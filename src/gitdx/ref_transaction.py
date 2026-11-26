"""Coordinated files refs with all-value CAS and explicit journal recovery.

Native readers do not honor locks, so this backend never promises atomic
multi-ref visibility. Journal checks assume owner-controlled storage paths.
"""

import base64
from bisect import bisect_left
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .filesystem import check_path, read_file, replace_file
from .names import InvalidRef, validate_refname
from .objects import ObjectId
from .packed_refs import PackedRefs
from .refs import DirectRef, RefConflict, RefUpdateResult


STATE_NAME='ref-transaction-state'
MAX_REFS=256
MAX_PACKED=16*1024*1024
MAX_JOURNAL=48*1024*1024


class RefRecoveryRequired(GitdxError):
    """A pending ref transaction requires explicit owner-controlled recovery."""


class RefTransactionState(StrEnum):
    PREPARED='prepared'
    COMMITTED='committed'
    ROLLED_BACK='rolled-back'
    UNKNOWN='unknown'


@dataclass(frozen=True, slots=True)
class RefChange:
    name: str
    new: ObjectId | None
    expected: ObjectId | None

    def __post_init__(self):
        validate_refname(self.name)
        if len(self.name)>1024:raise InvalidRef('transaction ref name limit exceeded')
        if self.new is None and self.expected is None:raise InvalidRef('a ref change needs an old or new ID')
        for oid in (self.new,self.expected):
            if oid is not None and (not isinstance(oid,ObjectId) or not any(oid.bytes)):
                raise InvalidRef('ref changes require non-null ObjectId values')
        if self.new is not None and self.expected is not None and self.new.format!=self.expected.format:
            raise InvalidObject('ref change formats differ')


@dataclass(frozen=True, slots=True)
class RefTransactionResult:
    transaction_id: str
    changes: tuple[RefChange,...]
    state: RefTransactionState
    journal_pending: bool = False
    retired_path: Path | None = None
    atomic: bool = False

    @property
    def committed(self):return self.state is RefTransactionState.COMMITTED

    @property
    def updates(self):
        if not self.committed:return ()
        return tuple(RefUpdateResult(c.name,DirectRef(c.expected) if c.expected else None,
                                    DirectRef(c.new) if c.new else None) for c in self.changes)


class RefTransactionInterrupted(GitdxError):
    def __init__(self,result):
        super().__init__('ref transaction interrupted; inspect its receipt and recover before further mutation')
        self.result=result


def require_no_ref_transaction(root):
    path=root/STATE_NAME;check_path(root,path)
    if path.exists():raise RefRecoveryRequired('ref transaction recovery is pending')


def _guard(store,*,recovering=False):
    store._profile()
    for name in ('checkout-state','operation-state','merge-state'):
        path=store.path/name;check_path(store.path,path)
        if path.exists():raise UnsupportedFormat('finish the pending repository operation before a ref transaction')
    if not recovering:require_no_ref_transaction(store.path)


def _changes(values,format):
    changes=tuple(islice(values,MAX_REFS+1))
    if not changes or len(changes)>MAX_REFS:raise ValueError('select 1–256 ref changes')
    if any(not isinstance(c,RefChange) for c in changes):raise TypeError('select RefChange records')
    names={c.name.casefold() for c in changes}
    if len(names)!=len(changes):raise InvalidRef('transaction ref names alias')
    for name in names:
        parts=name.split('/')
        if any('/'.join(parts[:i]) in names for i in range(1,len(parts))):raise InvalidRef('transaction ref names overlap')
    if any(oid is not None and oid.format!=format for c in changes for oid in (c.new,c.expected)):
        raise InvalidObject('ref transaction format differs from storage')
    return tuple(sorted(changes,key=lambda c:c.name))


@contextmanager
def _locks(store,changes,*,recovering=False,check=lambda:None):
    _guard(store,recovering=recovering)
    with ExitStack() as stack:
        stack.enter_context(store._lock(store.path/'packed-refs'))
        check()
        _guard(store,recovering=recovering)
        packed=store.packed();packed_names=sorted(e.name.casefold() for e in packed.entries)
        exact={e.name.casefold():e.name for e in packed.entries}
        for change in changes:
            check();name=change.name.casefold();parts=name.split('/')
            pos=bisect_left(packed_names,name+'/')
            if ((name in exact and exact[name]!=change.name)
                    or any('/'.join(parts[:i]) in exact for i in range(1,len(parts)))
                    or (pos<len(packed_names) and packed_names[pos].startswith(name+'/'))):
                raise InvalidRef('transaction conflicts with the packed namespace')
        for change in changes:
            check();stack.enter_context(store._lock(store._path(change.name)))
        _guard(store,recovering=recovering)
        yield


def _read(store,name):
    try:return read_file(store.path,store._path(name) if name!='packed-refs' else store.path/name,
                         MAX_PACKED if name=='packed-refs' else 4096)
    except FileNotFoundError:return None


def _packed_after(data,changes,format):
    if data is None:return None
    packed=PackedRefs.from_bytes(data,format);names={c.name for c in changes}
    remaining=tuple(e for e in packed.entries if e.name not in names)
    return data if remaining==packed.entries else PackedRefs(remaining,format,packed.headers).encode()


def _identity(root):
    info=root.stat();return [info.st_dev,info.st_ino]


def _canonical(data):return json.dumps(data,sort_keys=True,separators=(',',':')).encode('ascii')


def _save(root,journal):
    data=dict(journal,checksum=hashlib.sha256(_canonical(journal)).hexdigest());encoded=_canonical(data)
    if len(encoded)>MAX_JOURNAL:raise RefRecoveryRequired('ref journal byte limit exceeded')
    replace_file(root,root/'state.json',encoded)


def _encode(value):return None if value is None else base64.b64encode(value).decode('ascii')


def _decode(value,limit):
    if value is None:return None
    if not isinstance(value,str) or len(value)>4*((limit+2)//3):raise ValueError('invalid journal snapshot')
    data=base64.b64decode(value,validate=True)
    if len(data)>limit:raise ValueError('journal snapshot byte limit exceeded')
    return data


def _load(store,root=None):
    root=root or store.path/STATE_NAME;check_path(store.path,root)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate journal field')
            result[key]=value
        return result
    try:
        data=json.loads(read_file(root,root/'state.json',MAX_JOURNAL),object_pairs_hook=unique)
        if not isinstance(data,dict) or set(data)!={'version','id','format','repository','phase','changes','snapshots','checksum'}:
            raise ValueError('invalid journal fields')
        checksum=data.pop('checksum')
        if not isinstance(checksum,str) or hashlib.sha256(_canonical(data)).hexdigest()!=checksum:raise ValueError('ref journal checksum mismatch')
        if type(data['version']) is not int or data['version']!=1 or data['format']!=store.objects.format.value:
            raise ValueError('unsupported ref journal version/format')
        if data['repository']!=_identity(store.path) or not isinstance(data['id'],str) or not re.fullmatch('[0-9a-f]{32}',data['id']):
            raise ValueError('ref journal repository/ownership mismatch')
        if data['phase'] not in ('prepared','committed'):raise ValueError('invalid ref transaction phase')
        records=data['changes']
        if not isinstance(records,list) or not 1<=len(records)<=MAX_REFS:raise ValueError('invalid ref journal changes')
        if any(not isinstance(r,list) or len(r)!=3 for r in records):raise ValueError('invalid ref change record')
        changes=_changes((RefChange(name,ObjectId(new,store.objects.format) if new is not None else None,
                                   ObjectId(old,store.objects.format) if old is not None else None) for name,old,new in records),store.objects.format)
        if records!=[[c.name,str(c.expected) if c.expected else None,str(c.new) if c.new else None] for c in changes]:
            raise ValueError('ref journal changes are not canonical')
        encoded=data['snapshots']
        if not isinstance(encoded,dict) or set(encoded)!={'packed-refs',*(c.name for c in changes)}:
            raise ValueError('ref journal snapshot paths differ')
        if any(not isinstance(v,list) or len(v)!=2 for v in encoded.values()):raise ValueError('invalid ref snapshots')
        snapshots={name:tuple(_decode(v,MAX_PACKED if name=='packed-refs' else 4096) for v in values) for name,values in encoded.items()}
        before,after=snapshots['packed-refs']
        packed=PackedRefs.from_bytes(before,store.objects.format) if before is not None else PackedRefs(format=store.objects.format)
        if after!=_packed_after(before,changes,store.objects.format):raise ValueError('ref journal packed snapshots differ')
        packed_values={e.name:e.oid for e in packed.entries}
        for change in changes:
            old,new=snapshots[change.name]
            old_oid=ObjectId(old.removesuffix(b'\n').decode('ascii'),store.objects.format) if old is not None else packed_values.get(change.name)
            wanted=str(change.new).encode('ascii')+b'\n' if change.new else None
            if old_oid!=change.expected or new!=wanted:raise ValueError('ref journal logical and physical values differ')
        with os.scandir(root) as entries:
            entries=tuple(islice(entries,5))
        if len(entries)>4:raise ValueError('unexpected ref journal file count')
        for entry in entries:
            path=root/entry.name;check_path(root,path)
            if not stat.S_ISREG(path.stat().st_mode) or (entry.name!='state.json' and not entry.name.startswith('tmp_meta_')):
                raise ValueError('unexpected ref journal file')
        return data,changes,snapshots
    except (OSError,ValueError,TypeError,KeyError,GitdxError) as error:
        raise RefRecoveryRequired('ref journal is invalid or belongs to different storage; inspect it before recovery') from error


def _check_physical(store,snapshots,*,side=None):
    for name,values in snapshots.items():
        if _read(store,name) not in (values if side is None else (values[side],)):
            raise RefConflict('ref storage changed outside the pending transaction')


def _put(store,name,data):
    if _read(store,name)==data:return
    path=store.path/name;check_path(store.path,path)
    if data is None:path.unlink(missing_ok=True)
    else:replace_file(store.path,path,data)


def _install(store,changes,snapshots,side,check):
    # Updates first, then packed removals, then loose removals. Native readers
    # may see each complete intermediate ref; the set is never atomically visible.
    for change in changes:
        data=snapshots[change.name][side]
        if data is not None:check();_put(store,change.name,data)
    check();_put(store,'packed-refs',snapshots['packed-refs'][side])
    for change in changes:
        if snapshots[change.name][side] is None:check();_put(store,change.name,None)
    check()


def _retire(store,journal):
    state=store.path/STATE_NAME;retired=store.path/('tmp_refs_done_'+journal['id'])
    check_path(store.path,retired)
    if retired.exists():raise RefConflict('retired ref journal path already exists')
    os.rename(state,retired)
    try:shutil.rmtree(retired)
    except OSError:return retired
    return None


def transaction(store,values,*,atomic=False,check=None,before_publish=None):
    if type(atomic) is not bool:raise TypeError('atomic selection must be boolean')
    if atomic:raise UnsupportedFormat('files refs cannot provide atomic multi-ref visibility')
    check=(lambda:None) if check is None else check
    if not callable(check) or (before_publish is not None and not callable(before_publish)):raise TypeError('select trusted checks')
    check();changes=_changes(values,store.objects.format);_guard(store)
    with _locks(store,changes,check=check):
        for change in changes:
            check()
            if store.read(change.name)!=(DirectRef(change.expected) if change.expected else None):raise RefConflict('ref transaction expected-old value changed')
            if change.new is not None:store._target(change.name,change.new)
        if before_publish is not None:before_publish()
        packed=_read(store,'packed-refs')
        snapshots={'packed-refs':(packed,_packed_after(packed,changes,store.objects.format))}
        snapshots.update((c.name,(_read(store,c.name),str(c.new).encode('ascii')+b'\n' if c.new else None)) for c in changes)
        journal={'version':1,'id':uuid.uuid4().hex,'format':store.objects.format.value,'repository':_identity(store.path),'phase':'prepared',
                 'changes':[[c.name,str(c.expected) if c.expected else None,str(c.new) if c.new else None] for c in changes],
                 'snapshots':{name:[_encode(v) for v in values] for name,values in snapshots.items()}}
        state=store.path/STATE_NAME;temporary=Path(tempfile.mkdtemp(prefix='tmp_refs_',dir=store.path))
        temporary_identity=_identity(temporary);retired=None;prepared=False;committed=False
        try:
            _save(temporary,journal);check();require_no_ref_transaction(store.path)
            os.rename(temporary,state)
            prepared=True
            if before_publish is not None:before_publish()
            _guard(store,recovering=True);_check_physical(store,snapshots,side=0)
            for change in changes:
                check()
                if store.read(change.name)!=(DirectRef(change.expected) if change.expected else None):raise RefConflict('ref changed before publication')
                if change.new is not None:store._target(change.name,change.new)
            _install(store,changes,snapshots,1,check)
            journal['phase']='committed';_save(state,journal)
            committed=True
            retired=_retire(store,journal)
        except BaseException as error:
            if not state.exists() and not prepared:raise
            phase=RefTransactionState.UNKNOWN
            try:
                current,_,_=_load(store)
                if current['id']==journal['id']:phase=RefTransactionState(current['phase'])
            except RefRecoveryRequired:
                if not state.exists() and committed:phase=RefTransactionState.COMMITTED
            retired_path=store.path/('tmp_refs_done_'+journal['id'])
            result=RefTransactionResult(journal['id'],changes,phase,state.exists(),retired_path if retired_path.exists() else None)
            raise RefTransactionInterrupted(result) from error
        finally:
            if temporary.exists():
                check_path(store.path,temporary)
                if _identity(temporary)==temporary_identity:shutil.rmtree(temporary)
        return RefTransactionResult(journal['id'],changes,RefTransactionState.COMMITTED,False,retired)


def recover_transaction(store,*,check=None):
    check=(lambda:None) if check is None else check
    if not callable(check):raise TypeError('select a trusted check')
    check()
    state=store.path/STATE_NAME;check_path(store.path,state)
    if not state.exists():raise RefRecoveryRequired('there is no pending ref transaction')
    journal,changes,snapshots=_load(store)
    with _locks(store,changes,recovering=True,check=check):
        current,_,_=_load(store)
        if current!=journal:raise RefConflict('ref journal changed before recovery acquired its locks')
        _check_physical(store,snapshots)
        committed=journal['phase']=='committed'
        if committed:
            for change in changes:
                if change.new is not None:check();store._target(change.name,change.new)
        try:
            _install(store,changes,snapshots,int(committed),check)
            retired=_retire(store,journal)
        except BaseException as error:
            retired_path=store.path/('tmp_refs_done_'+journal['id'])
            result=RefTransactionResult(journal['id'],changes,RefTransactionState(journal['phase']),state.exists(),
                                        retired_path if retired_path.exists() else None)
            raise RefTransactionInterrupted(result) from error
        return RefTransactionResult(journal['id'],changes,RefTransactionState.COMMITTED if committed else RefTransactionState.ROLLED_BACK,False,retired)
