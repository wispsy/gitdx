"""Explicit, bounded same-format fetch and create-only clone operations."""

from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from itertools import islice
from typing import Protocol

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .filesystem import check_path
from .history import HistoryLimits, list_refs
from .names import validate_branch, validate_refname
from .objects import ObjectFormat, ObjectType
from .pack import PackLimits
from .pack_writer import PackWriteLimits, write_pack
from .protocol import Advertisement, AdvertisedRef, Capability, negotiate, parse_advertisement, encode_advertisement
from .pktline import PacketDecoder
from .quarantine import ObjectQuarantine
from .refs import DirectRef, RefConflict, RefLocked, SymbolicRef
from .repository import Repository


class TransferError(GitdxError):
    """A transfer failed without exposing peer text or credentials."""


class TransferCancelled(TransferError):pass
class TransferTimeout(TransferError):pass


class FetchInterrupted(TransferError):
    def __init__(self,updated):
        super().__init__('fetch publication interrupted; inspect completed ref outcomes')
        self.updated=tuple(updated)


class CloneIncomplete(TransferError):
    def __init__(self,path):
        super().__init__('clone incomplete; inspect the reserved destination and checkout recovery state')
        self.path=path


@dataclass(frozen=True, slots=True)
class TransferLimits:
    pack: PackLimits = field(default_factory=PackLimits)
    max_refs: int = 10_000
    max_seconds: float = 30
    max_wire_bytes: int = 288*1024*1024

    def __post_init__(self):
        if not isinstance(self.pack,PackLimits):raise TypeError('transfer pack limits must be PackLimits')
        if any(type(n) is not int or n<=0 for n in (self.max_refs,self.max_wire_bytes)):
            raise ValueError('transfer count/byte limits must be positive')
        if type(self.max_seconds) not in (int,float) or not 0<self.max_seconds<=86400:
            raise ValueError('transfer deadline must be finite and at most one day')


class TransferControl:
    def __init__(self,limits=None,cancel=None):
        self.limits=limits or TransferLimits();self.cancel=cancel
        self.deadline=time.monotonic()+self.limits.max_seconds;self.bytes=0

    def check(self):
        if self.cancel is not None and self.cancel.is_set():raise TransferCancelled('transfer cancelled')
        if time.monotonic()>=self.deadline:raise TransferTimeout('transfer deadline exceeded')

    def consume(self,size):
        self.check();self.bytes+=size
        if self.bytes>self.limits.max_wire_bytes:raise TransferError('transfer wire byte limit exceeded')


class FetchTransport(Protocol):
    def discover(self,control:TransferControl)->Advertisement: ...
    def fetch_pack(self,advertisement:Advertisement,wants,control:TransferControl)->bytes: ...


@dataclass(frozen=True, slots=True)
class LocalTransport:
    source: object = field(repr=False)

    def _repo(self):
        return Repository.open(self.source.git_dir if isinstance(self.source,Repository) else self.source)

    def discover_push(self,control):
        from .push import discover_local
        return discover_local(self,control)

    def send_pack(self,advertisement,request,control):
        from .push import receive_local
        return receive_local(self,advertisement,request,control)

    def discover(self,control):
        control.check();repo=self._repo();head=repo.refs.read('HEAD');resolved=repo.refs.resolve()
        entries=tuple(item for prefix in ('refs/heads/','refs/tags/') for item in list_refs(
            repo,prefix,limits=HistoryLimits(max_refs=control.limits.max_refs),check=control.check))
        if len(entries)>control.limits.max_refs:raise TransferError('remote ref count exceeded')
        refs=[AdvertisedRef(name.encode(),oid) for name,oid in entries if oid is not None]
        if resolved.oid is not None:refs.insert(0,AdvertisedRef(b'HEAD',resolved.oid,symref=head.target.encode() if isinstance(head,SymbolicRef) else None))
        caps=[Capability(b'object-format',repo.object_format.value.encode())]
        if isinstance(head,SymbolicRef):caps.append(Capability(b'symref',b'HEAD:'+head.target.encode()))
        if repo.refs.read('HEAD')!=head or repo.refs.resolve()!=resolved:raise RefConflict('source HEAD changed during discovery')
        ad=Advertisement(0,repo.object_format,tuple(caps),tuple(refs))
        # The same wire validators qualify local and HTTP advertisement inputs.
        decoder=PacketDecoder();packets=decoder.feed(encode_advertisement(ad));decoder.finish()
        control.check();return parse_advertisement(packets)

    def fetch_pack(self,advertisement,wants,control):
        repo=self._repo()
        if repo.object_format!=advertisement.format:raise TransferError('source format changed')
        outer=repo.objects
        class CheckedStore:
            format=outer.format
            def read(self,oid):control.check();return outer.read(oid)
        p=control.limits.pack
        limits=PackWriteLimits(max_objects=p.max_objects,max_object_bytes=p.max_object_bytes,max_bytes=p.max_delta_bytes,
                              max_pack_bytes=min(p.max_pack_bytes,0x7fffffff),max_index_bytes=p.max_index_bytes)
        pack=write_pack(CheckedStore(),wants,limits=limits).pack
        control.consume(len(pack));return pack


@dataclass(frozen=True, slots=True)
class FetchFailure:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class FetchResult:
    advertisement: Advertisement
    updated: tuple
    rejected: tuple[FetchFailure,...]
    received_objects: int

    @property
    def complete(self):return not self.rejected


def _selected(ad,remote,branches,tags,prefix,limit,check):
    validate_refname('refs/remotes/'+remote+'/probe')
    if '/' in remote or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',remote):raise ValueError('select a portable remote name')
    if isinstance(branches,(str,bytes)):raise TypeError('branches must be a sequence of branch names')
    selected=None if branches is None else tuple(islice(branches,limit+1))
    if selected is not None and len(selected)>limit:raise TransferError('selected branch count exceeded')
    if selected is not None:selected=set(validate_branch(name) for name in selected)
    if len(ad.refs)>limit+1:raise TransferError('advertised ref count exceeded')
    result=[];found=set();aliases=[]
    for entry in ad.refs:
        check()
        if entry.oid is None:continue
        if entry.name.startswith(b'refs/heads/'):
            try:branch=entry.name[len(b'refs/heads/'):].decode('ascii')
            except UnicodeError as error:raise UnsupportedFormat('remote ref outside portable mutation profile') from error
            if selected is not None and branch not in selected:continue
            name=prefix+branch;found.add(branch);kind='branch'
        elif tags and entry.name.startswith(b'refs/tags/'):
            try:name=entry.name.decode('ascii')
            except UnicodeError as error:raise UnsupportedFormat('remote tag outside portable mutation profile') from error
            kind='tag'
        else:continue
        validate_refname(name);key=name.casefold()
        aliases.append(key);result.append((name,entry.oid,kind))
        if len(result)>limit:raise TransferError('selected ref count exceeded')
    if selected is not None and selected!=found:raise TransferError('selected branch was not advertised')
    aliases.sort()
    if any(a==b or b.startswith(a+'/') for a,b in zip(aliases,aliases[1:])):raise TransferError('remote ref namespace aliases')
    return tuple(sorted(result))


def _fetch(repo,transport,control,*,remote='origin',branches=None,tags=False,allow_rewind=False,ad=None,prefix=None,include_head=False):
    control.check()
    pending=repo.git_dir/'checkout-state';check_path(repo.git_dir,pending)
    if pending.exists():raise UnsupportedFormat('checkout recovery is pending')
    ad=ad or transport.discover(control);negotiate(ad,repo.object_format)
    if ad.shallow:raise UnsupportedFormat('shallow sources are unsupported')
    mappings=_selected(ad,remote,branches,tags,prefix or 'refs/remotes/'+remote+'/',control.limits.max_refs,control.check)
    expected={}
    for name,_,_ in mappings:
        control.check();expected[name]=repo.refs.read(name)
    for value in expected.values():
        if value is not None and not isinstance(value,DirectRef):raise RefConflict('fetch destination is symbolic')
    roots=list(dict.fromkeys(oid for _,oid,_ in mappings))
    if include_head:
        roots.extend(r.oid for r in ad.refs if r.name==b'HEAD' and r.oid is not None and r.oid not in roots)
    control.check()
    if not roots:return FetchResult(ad,(),(),0)
    data=transport.fetch_pack(ad,tuple(roots),control)
    with ObjectQuarantine(repo,limits=control.limits.pack,check=control.check) as incoming:
        incoming.receive(data);incoming.validate(roots)
        for name,oid,kind in mappings:
            if kind=='branch' and incoming.read(oid).kind is not ObjectType.COMMIT:
                raise InvalidObject('advertised branch does not name a commit')
        incoming.promote();received=len(incoming.received)
    updated=[];rejected=[]
    try:
        for name,oid,kind in mappings:
            control.check();old=expected[name]
            if old is not None and old.oid!=oid and (kind=='tag' or not allow_rewind):
                fast_forward=False
                if kind=='branch':
                    for row in repo.walk_history((oid,),limits=HistoryLimits(max_commits=control.limits.pack.max_objects)):
                        control.check()
                        if row.oid==old.oid:fast_forward=True;break
                if not fast_forward:
                    rejected.append(FetchFailure(name,'tag replacement' if kind=='tag' else 'non-fast-forward'));continue
            try:updated.append(repo.refs.update(name,oid,expected=old.oid if old else None))
            except (RefConflict,RefLocked) as error:rejected.append(FetchFailure(name,type(error).__name__))
    except (OSError,GitdxError,ValueError) as error:
        raise FetchInterrupted(updated) from error
    return FetchResult(ad,tuple(updated),tuple(rejected),received)


def fetch(repo,transport,*,remote='origin',branches=None,tags=False,allow_rewind=False,limits=None,cancel=None):
    """Fetch into tracking refs; no pruning, local branch movement or checkout."""
    return _fetch(repo,transport,TransferControl(limits,cancel),remote=remote,branches=branches,tags=tags,allow_rewind=allow_rewind)


def _default(ad,branch):
    heads={r.name[len(b'refs/heads/'):].decode('ascii'):r.oid for r in ad.refs if r.name.startswith(b'refs/heads/')}
    if branch is not None:
        validate_branch(branch)
        if branch not in heads:raise TransferError('selected clone branch was not advertised')
        return branch,heads[branch]
    head=next((r for r in ad.refs if r.name==b'HEAD'),None)
    target=head.symref if head else None
    if target is None:
        target=next((c.value[5:] for c in ad.capabilities if c.name==b'symref' and c.value and c.value.startswith(b'HEAD:')),None)
    if target and target.startswith(b'refs/heads/'):
        try:name=target[len(b'refs/heads/'):].decode('ascii')
        except UnicodeError as error:raise UnsupportedFormat('remote HEAD outside portable profile') from error
        validate_branch(name)
        if name in heads:return name,heads[name]
        if not heads and (head is None or head.oid is None):return name,None
        return None,None  # Remote HEAD names a missing branch; do not guess.
    return None,head.oid if head else None


def clone(transport,path,*,object_format=None,bare=False,branch=None,checkout=True,tags=True,limits=None,cancel=None):
    """Reserve a new destination; retain incomplete state for explicit inspection."""
    destination=Path(path).absolute();check_path(destination.parent,destination)
    if destination.exists():raise FileExistsError('clone destination already exists')
    control=TransferControl(limits,cancel);ad=transport.discover(control)
    format=ad.format if object_format is None else ObjectFormat(object_format);negotiate(ad,format)
    selected,oid=_default(ad,branch)
    control.check();destination.mkdir()
    try:
        repo=Repository.init(destination,bare=bare,object_format=format,initial_branch=selected or 'main')
        result=_fetch(repo,transport,control,ad=ad,tags=tags,prefix='refs/heads/' if bare else None,include_head=True)
        if not result.complete:raise TransferError('clone refs could not be published completely')
        if oid is not None:
            if repo.objects.read(oid).kind is not ObjectType.COMMIT:raise InvalidObject('clone HEAD does not name a commit')
            if not bare and checkout:
                repo.apply_checkout(repo.plan_checkout(oid))
            if selected is not None:
                if not bare:repo.refs.update('refs/heads/'+selected,oid,expected=None)
                repo.refs.set_symbolic('HEAD','refs/heads/'+selected,expected=repo.refs.read('HEAD'))
            else:repo.refs.detach_head(oid,expected=repo.refs.read('HEAD'))
        control.check();return repo
    except (OSError,GitdxError,ValueError) as error:
        raise CloneIncomplete(destination) from error
