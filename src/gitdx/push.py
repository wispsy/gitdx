"""Explicit push proposals, per-ref outcomes and a non-atomic local receiver."""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import islice
import time
from typing import Protocol

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .history import HistoryLimits
from .names import InvalidRef, validate_refname
from .objects import ObjectId, ObjectType
from .pack_writer import PackWriteLimits, write_pack
from .pktline import PacketDecoder, ProtocolBudget, ProtocolError, ProtocolLimits
from .protocol import Advertisement, Capability, ObjectFormatMismatch, encode_advertisement, parse_advertisement
from .push_protocol import (CommandStatus, PushCommand, ReportStatus, encode_push_request, encode_report_status,
                            negotiate_push, parse_push_request, parse_report_status)
from .receive import ReceiveLimits, ReceivePolicy, ReceiveRejected, ReceiveState, receive, receive_profile
from .transfers import TransferControl, TransferError


@dataclass(frozen=True, slots=True)
class PushUpdate:
    name: str
    new: ObjectId | None
    expected_old: ObjectId | None = field(kw_only=True)
    force: bool = field(default=False,kw_only=True)

    def __post_init__(self):
        validate_refname(self.name)
        if not self.name.startswith(('refs/heads/','refs/tags/')):raise UnsupportedFormat('push selects branch/tag refs explicitly')
        if type(self.force) is not bool:raise TypeError('force selection must be boolean')
        PushCommand(self.name.encode('ascii'),self.expected_old,self.new)

    def command(self):return PushCommand(self.name.encode('ascii'),self.expected_old,self.new)


class PushState(StrEnum):
    ACCEPTED='accepted'
    REJECTED='rejected'
    NOT_SENT='not_sent'
    UNKNOWN='unknown'


@dataclass(frozen=True, slots=True)
class PushOutcome:
    name: str
    old: ObjectId | None
    new: ObjectId | None
    state: PushState
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PushResult:
    outcomes: tuple[PushOutcome,...]
    atomic: bool
    peer_atomic: bool
    report_version: int
    atomic_requested: bool = False

    @property
    def complete(self):return all(o.state is PushState.ACCEPTED for o in self.outcomes)


class PushUncertain(TransferError):
    """Upload started but a complete, consistent remote outcome is unavailable."""
    def __init__(self,updates,*,atomic=False,peer_atomic=False,report_version=1):
        super().__init__('push outcome is unknown; inspect remote refs before retrying')
        self.result=PushResult(tuple(PushOutcome(u.name,u.expected_old,u.new,PushState.UNKNOWN,'report unavailable') for u in updates),
                               False,peer_atomic,report_version,atomic_requested=atomic)


class PushTransport(Protocol):
    def discover_push(self,control:TransferControl)->Advertisement: ...
    def send_pack(self,advertisement:Advertisement,request:bytes,control:TransferControl)->bytes: ...


def protocol_limits(control):
    control.check()
    return ProtocolLimits(max_bytes=control.limits.max_wire_bytes,max_refs=control.limits.max_refs,
                          timeout=max(.001,control.deadline-time.monotonic()))


def packets(data,control):
    decoder=PacketDecoder(budget=ProtocolBudget(protocol_limits(control),cancelled=control.cancel.is_set if control.cancel else None))
    result=[]
    for start in range(0,len(data),65536):
        control.check();result.extend(decoder.feed(data[start:start+65536]))
    decoder.finish();control.check();return tuple(result)


def ancestor(repo,old,new,control):
    for entry in repo.walk_history((new,),limits=HistoryLimits(max_commits=control.limits.pack.max_objects,
                                                             max_bytes=control.limits.pack.max_delta_bytes)):
        control.check()
        if entry.oid==old:return True
    return False


def check_names(names):
    values=[name.casefold() for name in names];unique=set(values)
    if len(unique)!=len(values):raise InvalidRef('push ref names alias')
    for name in unique:
        parts=name.split('/')
        if any('/'.join(parts[:i]) in unique for i in range(1,len(parts))):raise InvalidRef('push ref names overlap')


def push(repo,transport,updates,*,atomic=False,limits=None,cancel=None):
    control=TransferControl(limits,cancel);control.check();repo.refs.snapshot(max_refs=control.limits.max_refs,check=control.check)
    updates=tuple(islice(updates,control.limits.max_refs+1))
    if not updates or len(updates)>control.limits.max_refs:raise ValueError('select a bounded, nonempty push batch')
    if any(not isinstance(u,PushUpdate) for u in updates):raise TypeError('select PushUpdate proposals')
    check_names(u.name for u in updates)
    if any(oid is not None and oid.format!=repo.object_format for u in updates for oid in (u.expected_old,u.new)):
        raise ObjectFormatMismatch('push proposals differ from source object format')
    commands=tuple(u.command() for u in updates)
    ad=transport.discover_push(control);n=negotiate_push(ad,repo.object_format,atomic=atomic,limits=protocol_limits(control))
    peer_atomic=any(c.name==b'atomic' for c in ad.capabilities)
    report_version=2 if any(c.name==b'report-status-v2' for c in n.capabilities) else 1
    if len(ad.refs)>control.limits.max_refs+1:raise TransferError('push discovery ref count exceeded')
    advertised={r.name:r for r in ad.refs};rejections={}
    for update,command in zip(updates,commands):
        control.check();entry=advertised.get(command.name)
        if entry is not None and entry.symref is not None:raise UnsupportedFormat('push does not select symbolic ref destinations')
        old=entry.oid if entry else None
        if old!=update.expected_old:rejections[update.name]='stale expected-old value';continue
        if update.new is None:
            if not any(c.name==b'delete-refs' for c in ad.capabilities):raise UnsupportedFormat('peer does not advertise deletion')
            continue
        if update.new.format!=repo.object_format:raise InvalidObject('push source object format differs')
        raw=repo.objects.read(update.new)
        if update.name.startswith('refs/heads/') and raw.kind is not ObjectType.COMMIT:
            raise InvalidObject('pushed branch must name a commit')
        if old is not None and old!=update.new and not update.force:
            if update.name.startswith('refs/tags/') or not ancestor(repo,old,update.new,control):
                rejections[update.name]='tag replacement' if update.name.startswith('refs/tags/') else 'non-fast-forward'
    if rejections:
        return PushResult(tuple(PushOutcome(u.name,u.expected_old,u.new,PushState.REJECTED if u.name in rejections else PushState.NOT_SENT,
                                             rejections.get(u.name,'batch failed preflight')) for u in updates),False,peer_atomic,report_version,atomic_requested=atomic)
    roots=tuple(dict.fromkeys(u.new for u in updates if u.new is not None));pack=b''
    if roots:
        p=control.limits.pack;source=repo.objects
        class CheckedStore:
            format=source.format
            def read(self,oid):control.check();return source.read(oid)
        pack=write_pack(CheckedStore(),roots,limits=PackWriteLimits(max_objects=p.max_objects,max_object_bytes=p.max_object_bytes,
            max_bytes=p.max_delta_bytes,max_pack_bytes=min(p.max_pack_bytes,0x7fffffff),max_index_bytes=p.max_index_bytes)).pack
    request=encode_push_request(n,commands,pack,limits=protocol_limits(control));control.check()
    try:
        response=transport.send_pack(ad,request,control)
        report=parse_report_status(packets(response,control),commands,repo.object_format,version=report_version,limits=protocol_limits(control))
        byname={c.name:c for c in commands}
        for status in report.commands:
            original=byname[status.name]
            for option in status.options:
                if option.name==b'refname' and option.value!=original.name:raise ProtocolError('rewritten push ref is outside selected profile')
                if option.name in (b'old-oid',b'new-oid'):
                    expected=original.old if option.name==b'old-oid' else original.new
                    value=(str(expected) if expected else '0'*repo.object_format.hex_size).encode()
                    if option.value.lower()!=value:raise ProtocolError('rewritten push ID is outside selected profile')
        if atomic and len({s.ok for s in report.commands})>1:raise ProtocolError('peer violated atomic report consistency')
        control.check()
    except (OSError,GitdxError,ValueError,TypeError,KeyboardInterrupt) as error:
        raise PushUncertain(updates,atomic=atomic,peer_atomic=peer_atomic,report_version=report_version) from error
    reported={s.name:s for s in report.commands}
    return PushResult(tuple(PushOutcome(u.name,u.expected_old,u.new,PushState.ACCEPTED if reported[u.name.encode()].ok else PushState.REJECTED,
                                       None if reported[u.name.encode()].ok else 'remote rejected' if report.unpack_ok else 'unpack rejected') for u in updates),
                      atomic,peer_atomic,report_version,atomic_requested=atomic)


def receiver_policy(repo,control):
    return receive_profile(repo,control)


def discover_local(transport,control):
    repo=transport._repo();receiver_policy(repo,control);ad=transport.discover(control)
    caps=(Capability(b'object-format',ad.format.value.encode()),Capability(b'report-status'),Capability(b'delete-refs'),Capability(b'ofs-delta'))
    limits=replace(protocol_limits(control),max_refs=control.limits.max_refs+1)
    wire=encode_advertisement(Advertisement(0,ad.format,caps,ad.refs),limits=limits);control.consume(len(wire))
    return parse_advertisement(packets(wire,control),limits=limits)


def receive_local(transport,ad,request,control):
    repo=transport._repo();receiver_policy(repo,control)
    if repo.object_format!=ad.format:raise UnsupportedFormat('local receive object format changed')
    actual=discover_local(transport,control)
    parsed=parse_push_request(request,actual,repo.object_format,limits=protocol_limits(control));commands=parsed.commands
    control.consume(len(request)-len(parsed.pack))
    p=control.limits.pack
    limits=ReceiveLimits(transfer=control.limits,graph=PackWriteLimits(max_objects=p.max_objects,
        max_object_bytes=p.max_object_bytes,max_bytes=p.max_delta_bytes))
    policy=ReceivePolicy(allow_deletes=True,allow_non_fast_forwards=True,allow_tag_replacement=True)
    try:result=receive(repo,commands,parsed.pack,policy=policy,limits=limits,_control=control)
    except ReceiveRejected as error:result=error.result
    statuses=tuple(CommandStatus(o.command.name,o.state is ReceiveState.ACCEPTED,
        None if o.state is ReceiveState.ACCEPTED else o.reason or b'reference rejected') for o in result.outcomes)
    report=ReportStatus(result.unpack_ok,statuses,None if result.unpack_ok else b'invalid pack or graph')
    wire=encode_report_status(report,commands,repo.object_format,limits=protocol_limits(control));control.consume(len(wire));return wire
