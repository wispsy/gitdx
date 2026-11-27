"""Hosting-neutral, bounded receive with explicit policy and publication outcomes."""

from dataclasses import dataclass, field, replace
from contextlib import nullcontext
from enum import StrEnum
from itertools import islice

from .ancestry import is_ancestor
from .config import parse_config
from .confined_fs import ConfinedStorage, FilesystemLimits
from .errors import InvalidObject, UnsupportedFormat
from .filesystem import check_path, read_file, storage_exists
from .ingestion import IngestionLimits, IngestionProfile, ObjectValidator, check_ingestion_format, check_ingestion_names
from .names import InvalidRef, validate_refname
from .object_store import MissingObject, ObjectLimitExceeded
from .objects import ObjectType
from .pack_writer import PackWriteLimits
from .push_protocol import PushCommand
from .quarantine import ObjectQuarantine
from .refs import DirectRef, RefConflict, RefLocked, RefUpdateResult
from .ref_transaction import (RefChange, RefTransactionInterrupted, RefTransactionResult,
                              require_no_ref_transaction)
from .revisions import RevisionLimits
from .transfers import TransferControl, TransferError, TransferLimits


@dataclass(frozen=True, slots=True)
class ReceiveLimits:
    transfer: TransferLimits = field(default_factory=lambda:TransferLimits(max_refs=256))
    graph: PackWriteLimits = field(default_factory=PackWriteLimits)
    max_quarantine_bytes: int = 288 * 1024 * 1024
    ingestion: IngestionLimits = field(default_factory=IngestionLimits)
    filesystem: FilesystemLimits = field(default_factory=FilesystemLimits)

    def __post_init__(self):
        if (not isinstance(self.transfer,TransferLimits) or not isinstance(self.graph,PackWriteLimits)
                or not isinstance(self.ingestion,IngestionLimits) or not isinstance(self.filesystem,FilesystemLimits)):
            raise TypeError('select transfer and graph limits')
        if type(self.max_quarantine_bytes) is not int or self.max_quarantine_bytes<=0:
            raise ValueError('select a positive quarantine storage limit')


@dataclass(frozen=True, slots=True)
class ReceivePolicy:
    allow_deletes: bool = False
    allow_non_fast_forwards: bool = False
    allow_tag_replacement: bool = False
    object_check: object = field(default=None,repr=False)
    ref_check: object = field(default=None,repr=False)
    ingestion: IngestionProfile = IngestionProfile.TRUSTED_LOCAL
    publication_guard: object = field(default=None,repr=False)

    def __post_init__(self):
        object.__setattr__(self,'ingestion',IngestionProfile(self.ingestion))
        for value in (self.allow_deletes,self.allow_non_fast_forwards,self.allow_tag_replacement):
            if type(value) is not bool:raise TypeError('receive policy switches must be booleans')
        if any(value is not None and not callable(value) for value in (self.object_check,self.ref_check,self.publication_guard)):
            raise TypeError('policy checks must be trusted callables')


class ReceiveState(StrEnum):
    ACCEPTED='accepted'
    REJECTED='rejected'
    NOT_ATTEMPTED='not-attempted'
    UNKNOWN='unknown'


@dataclass(frozen=True, slots=True)
class ReceiveOutcome:
    command: PushCommand
    state: ReceiveState
    reason: bytes | None = None
    receipt: RefUpdateResult | None = None


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    outcomes: tuple[ReceiveOutcome,...]
    unpack_ok: bool
    received_objects: int = 0
    promoted_objects: int = 0
    cleanup_pending: bool = False
    cleanup_error: str | None = None
    atomic: bool = False
    coordinated: bool = False
    ref_transaction: RefTransactionResult | None = None
    storage_identity: tuple[int,int] | None = None

    @property
    def complete(self):return self.unpack_ok and all(o.state is ReceiveState.ACCEPTED for o in self.outcomes)


class ReceiveRejected(TransferError):
    def __init__(self,result):
        super().__init__('receive pack, graph or object policy rejected before ref publication')
        self.result=result


class ReceiveInterrupted(TransferError):
    def __init__(self,result):
        super().__init__('receive interrupted; inspect explicit ref outcomes and retained storage')
        self.result=result


class ReceiveView:
    """Read-only policy helpers over quarantined and existing native objects."""

    def __init__(self,repo,incoming,limits):
        self._repo=replace(repo,objects=incoming);self._incoming=incoming;self._limits=limits

    def read(self,oid):return self._incoming.read(oid)

    def is_ancestor(self,old,new):
        graph=self._limits.graph
        return is_ancestor(self._repo,old,new,limits=RevisionLimits(max_objects=graph.max_objects,
            max_bytes=graph.max_bytes,max_steps=graph.max_edges,max_parents=graph.max_edges))


def receive_profile(repo,control):
    control.check()
    if not repo.bare:raise UnsupportedFormat('receive requires an explicitly selected bare destination')
    config=parse_config(read_file(repo.git_dir,repo.git_dir/'config',1024*1024));values=config.values
    repo.refs.packed()
    if config.object_format!=repo.object_format or not config.bare:raise UnsupportedFormat('receive profile changed')
    if values.get_all('core','hookspath') or values.get_all('receive','procreceiverefs'):
        raise UnsupportedFormat('receive does not execute configured hooks')
    if values.boolean('receive','fsckobjects') or values.boolean('transfer','fsckobjects'):
        raise UnsupportedFormat('native fsck policy requires a separately qualified receiver')
    for name in ('pre-receive','update','post-receive','post-update','proc-receive','push-to-checkout'):
        path=repo.git_dir/'hooks'/name;check_path(repo.git_dir,path)
        if storage_exists(path):raise UnsupportedFormat('receive refuses active hooks')
    return values.boolean('receive','denydeletes'),values.boolean('receive','denynonfastforwards')


def _commands(commands,repo,limit):
    commands=tuple(islice(commands,limit+1))
    if not commands or len(commands)>limit:raise ValueError('select a bounded nonempty receive batch')
    names=set()
    for command in commands:
        if not isinstance(command,PushCommand):raise TypeError('select PushCommand records')
        try:name=command.name.decode('ascii')
        except UnicodeError as error:raise UnsupportedFormat('receive selects portable ref names') from error
        validate_refname(name)
        if not name.startswith(('refs/heads/','refs/tags/')) or len(command.name)>1024:
            raise UnsupportedFormat('receive selects bounded branch/tag refs')
        key=name.casefold()
        if key in names:raise InvalidRef('receive ref names alias')
        names.add(key)
        if any(oid is not None and oid.format!=repo.object_format for oid in (command.old,command.new)):
            raise InvalidObject('receive object format differs from storage')
    for name in names:
        parts=name.split('/')
        if any('/'.join(parts[:i]) in names for i in range(1,len(parts))):raise InvalidRef('receive ref names overlap')
    return commands


def _ref_reason(repo,view,command,policy,control):
    deny_deletes,deny_rewinds=receive_profile(repo,control)
    name=command.name.decode('ascii');actual=repo.refs.read(name)
    if actual!=(DirectRef(command.old) if command.old else None):return b'stale reference'
    if command.new is None:
        if deny_deletes or not policy.allow_deletes:return b'deletion prohibited'
    elif command.old is not None and command.old!=command.new:
        if name.startswith('refs/tags/'):
            if not policy.allow_tag_replacement:return b'tag replacement prohibited'
        elif (deny_rewinds or not policy.allow_non_fast_forwards) and not view.is_ancestor(command.old,command.new):
            return b'non-fast-forward'
    if policy.ref_check is not None:
        reason=policy.ref_check(view,command)
        if reason is not None and (not isinstance(reason,bytes) or not 1<=len(reason)<=128 or any(c<32 or c>126 for c in reason)):
            raise ValueError('ref policy must return None or a bounded printable ASCII reason')
        return reason
    return None


def receive(repo,commands,pack=b'',*,policy,limits=None,cancel=None,coordinated=False,atomic=False,_control=None):
    if not isinstance(policy,ReceivePolicy):raise TypeError('select an explicit ReceivePolicy')
    profile=check_ingestion_format(policy.ingestion,repo.object_format)
    if profile is IngestionProfile.TRUSTED_LOCAL:
        return _receive(repo,commands,pack,policy=policy,limits=limits,cancel=cancel,coordinated=coordinated,atomic=atomic,_control=_control)
    limits=limits or ReceiveLimits()
    if not isinstance(limits,ReceiveLimits):raise TypeError('select ReceiveLimits')
    if limits.max_quarantine_bytes<8192:raise ObjectLimitExceeded('hosting quarantine allocation requires at least 8192 bytes')
    control=_control or TransferControl(limits.transfer,cancel)
    with ConfinedStorage(repo.git_dir,repo.storage_identity,limits=limits.filesystem,check=control.check,
                         limit_error=ObjectLimitExceeded) as storage:
        result=_receive(repo,commands,pack,policy=policy,limits=limits,cancel=cancel,coordinated=coordinated,atomic=atomic,_control=control)
        try:storage.unchanged()
        except BaseException as error:raise ReceiveInterrupted(result) from error
        return result


def _receive(repo,commands,pack=b'',*,policy,limits=None,cancel=None,coordinated=False,atomic=False,_control=None):
    """Publish verified objects then explicit per-ref CAS updates; never atomic.

    Authentication belongs to the caller. Trusted policy callables must not
    mutate storage. Interrupted publication can be unknown; it is never hidden
    behind a whole-batch rejection. No configured external hook is executed.
    """
    if not isinstance(policy,ReceivePolicy):raise TypeError('select an explicit ReceivePolicy')
    ingestion=check_ingestion_format(policy.ingestion,repo.object_format)
    if type(coordinated) is not bool or type(atomic) is not bool:raise TypeError('receive mode switches must be booleans')
    if atomic:raise UnsupportedFormat('files refs cannot provide atomic receive visibility')
    limits=limits or ReceiveLimits()
    if not isinstance(limits,ReceiveLimits):raise TypeError('select ReceiveLimits')
    control=_control or TransferControl(limits.transfer,cancel)
    require_no_ref_transaction(repo.git_dir)
    receive_profile(repo,control);commands=_commands(commands,repo,min(limits.transfer.max_refs,256) if coordinated else limits.transfer.max_refs)
    if ingestion is IngestionProfile.UNTRUSTED:check_ingestion_names(commands,limits.ingestion)
    if not isinstance(pack,bytes):raise TypeError('receive pack must be immutable bytes')
    roots=tuple(c.new for c in commands if c.new is not None)
    if bool(roots)!=bool(pack):raise InvalidObject('pack presence differs from receive targets')
    control.consume(len(pack))
    outcomes=[ReceiveOutcome(c,ReceiveState.NOT_ATTEMPTED) for c in commands]
    validator=ObjectValidator(repo.object_format,limits=limits.ingestion,check=control.check) if ingestion is IngestionProfile.UNTRUSTED else None
    incoming=ObjectQuarantine(repo,limits=limits.transfer.pack,check=control.check,validator=validator)
    unpack_ok=False;error=None;rejected=False;ref_transaction=None
    try:
        with incoming:
            if pack:
                try:
                    incoming.receive(pack,max_storage_bytes=limits.max_quarantine_bytes)
                    # Validate even unreferenced supplied objects so policy and
                    # connectivity checks cannot be bypassed by hiding roots.
                    all_roots=tuple(dict.fromkeys((*roots,*(oid for oid,_ in incoming.received))))
                    closure=incoming.validate(all_roots,limits=limits.graph)
                    for command in commands:
                        if command.new is not None and command.name.startswith(b'refs/heads/') and incoming.read(command.new).kind is not ObjectType.COMMIT:
                            raise InvalidObject('received branch must name a commit')
                    for oid,raw in closure:
                        control.check()
                        if policy.object_check is not None:
                            allowed=policy.object_check(oid,raw)
                            if type(allowed) is not bool:raise TypeError('object policy must return a boolean')
                            if not allowed:raise InvalidObject('object policy rejected')
                except (InvalidObject,MissingObject,ObjectLimitExceeded):
                    rejected=True;raise
            unpack_ok=True;view=ReceiveView(repo,incoming,limits)
            allowed=[]
            for i,command in enumerate(commands):
                control.check();reason=_ref_reason(repo,view,command,policy,control)
                if reason is None:allowed.append(i)
                else:outcomes[i]=ReceiveOutcome(command,ReceiveState.REJECTED,reason)
            if coordinated and len(allowed)!=len(commands):
                for i in allowed:outcomes[i]=ReceiveOutcome(commands[i],ReceiveState.REJECTED,b'coordinated batch refused')
                allowed=[]
            if allowed and pack:incoming.promote()
            # A trusted host can serialize catalog policy/revocation with the
            # complete publication boundary. Acquire it before ref locks, then
            # re-run ref_check under the coordinated backend's own locks.
            publication = policy.publication_guard(view,commands) if allowed and policy.publication_guard is not None else nullcontext()
            with publication:
                control.check()
                if coordinated and allowed:
                    def guard():
                        for command in commands:
                            if _ref_reason(repo,view,command,policy,control) is not None:raise RefConflict('receive ref policy changed')
                    changes=tuple(RefChange(c.name.decode('ascii'),c.new,c.old) for c in commands)
                    try:ref_transaction=repo.refs.transaction(changes,check=control.check,before_publish=guard)
                    except RefTransactionInterrupted as interrupted:
                        ref_transaction=interrupted.result
                        state=ReceiveState.ACCEPTED if ref_transaction.committed else ReceiveState.UNKNOWN
                        receipts={r.name:r for r in ref_transaction.updates}
                        outcomes=[ReceiveOutcome(c,state, None if ref_transaction.committed else b'ref recovery required',receipts.get(c.name.decode('ascii'))) for c in commands]
                        raise
                    except (RefConflict,RefLocked,InvalidRef,UnsupportedFormat):
                        outcomes=[ReceiveOutcome(c,ReceiveState.REJECTED,b'coordinated ref transaction rejected') for c in commands]
                    else:
                        receipts={r.name:r for r in ref_transaction.updates}
                        outcomes=[ReceiveOutcome(c,ReceiveState.ACCEPTED,receipt=receipts[c.name.decode('ascii')]) for c in commands]
                for i in (() if coordinated else allowed):
                    command=commands[i];control.check()
                    reason=_ref_reason(repo,view,command,policy,control)
                    if reason is not None:
                        outcomes[i]=ReceiveOutcome(command,ReceiveState.REJECTED,reason);continue
                    outcomes[i]=ReceiveOutcome(command,ReceiveState.UNKNOWN,b'publication interrupted')
                    try:
                        name=command.name.decode('ascii')
                        receipt=repo.refs.delete(name,expected=command.old) if command.new is None else repo.refs.update(name,command.new,expected=command.old)
                    except (RefConflict,RefLocked,InvalidRef,UnsupportedFormat):
                        outcomes[i]=ReceiveOutcome(command,ReceiveState.REJECTED,b'reference rejected')
                    else:outcomes[i]=ReceiveOutcome(command,ReceiveState.ACCEPTED,receipt=receipt)
                    control.check()
    except BaseException as cause:error=cause
    result=ReceiveResult(tuple(outcomes),unpack_ok,len(incoming.received),incoming.promoted,
                         incoming.cleanup_pending,incoming.cleanup_error,coordinated=coordinated,
                         ref_transaction=ref_transaction,storage_identity=repo.storage_identity)
    if error is not None:
        if rejected:
            result=replace(result,outcomes=tuple(ReceiveOutcome(c,ReceiveState.REJECTED,b'invalid pack, graph or object policy') for c in commands))
            raise ReceiveRejected(result) from error
        raise ReceiveInterrupted(result) from error
    return result


def recover_receive(repo,*,ingestion=IngestionProfile.TRUSTED_LOCAL,limits=None,cancel=None):
    """Owner-selected recovery of a coordinated receive's ref journal."""
    profile=check_ingestion_format(ingestion,repo.object_format);limits=limits or ReceiveLimits()
    if not isinstance(limits,ReceiveLimits):raise TypeError('select ReceiveLimits')
    control=TransferControl(limits.transfer,cancel)
    if profile is IngestionProfile.TRUSTED_LOCAL:return repo.refs.recover_transaction(check=control.check)
    with ConfinedStorage(repo.git_dir,repo.storage_identity,limits=limits.filesystem,check=control.check,
                         limit_error=ObjectLimitExceeded) as storage:
        result=repo.refs.recover_transaction(check=control.check);storage.unchanged()
        return result
