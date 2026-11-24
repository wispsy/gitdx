"""Expected-old receive-pack commands and bounded report-status records."""

from dataclasses import dataclass, field, replace
from itertools import islice

from .objects import ObjectFormat, ObjectId
from .pktline import FLUSH, Packet, PacketDecoder, PacketKind, ProtocolError, ProtocolLimitExceeded, ProtocolLimits
from .protocol import (Negotiated, _caps, _format, _message, _name, _oid, _text, _wire, negotiate, ObjectFormatMismatch)

_PUSH_CAPS=frozenset((b'report-status',b'report-status-v2',b'ofs-delta',b'side-band-64k',b'atomic',b'object-format',b'delete-refs'))


@dataclass(frozen=True, slots=True)
class PushCommand:
    name: bytes
    old: ObjectId | None
    new: ObjectId | None

    def __post_init__(self):
        if not isinstance(self.name,bytes):raise TypeError('wire ref name must be bytes')
        _name(self.name,ProtocolLimits(),head=False)
        if self.old is None and self.new is None:raise ProtocolError('push command cannot have two null IDs')
        for oid in (self.old,self.new):
            if oid is not None and (not isinstance(oid,ObjectId) or not any(oid.bytes)):raise ProtocolError('use None for null push IDs')
        if self.old is not None and self.new is not None and self.old.format!=self.new.format:
            raise ObjectFormatMismatch('push command formats differ')


@dataclass(frozen=True, slots=True)
class PushRequest:
    commands: tuple[PushCommand,...]
    negotiated: Negotiated
    pack: bytes = field(repr=False)


def negotiate_push(ad,format,*,atomic=False,sideband=True,report_version=None,limits=None):
    format=ObjectFormat(format)
    if ad.version not in (0,1):raise ProtocolError('receive-pack uses legacy discovery, not protocol v2 commands')
    if ad.shallow:raise ProtocolError('shallow push is unsupported')
    if type(atomic) is not bool or type(sideband) is not bool:raise TypeError('push mode selection must be boolean')
    available={c.name for c in ad.capabilities}
    if report_version is None:report_version=2 if b'report-status-v2' in available else 1
    if type(report_version) is not int or report_version not in (1,2):raise ProtocolError('select report-status version 1 or 2')
    report=b'report-status-v2' if report_version==2 else b'report-status'
    if report not in available:raise ProtocolError('peer does not advertise the selected report-status')
    if atomic and b'atomic' not in available:raise ProtocolError('peer does not advertise atomic push')
    caps=[report]
    if b'ofs-delta' in available:caps.append(b'ofs-delta')
    if sideband and b'side-band-64k' in available:caps.append(b'side-band-64k')
    if atomic:caps.append(b'atomic')
    return negotiate(ad,format,capabilities=caps,limits=limits)


def _push_caps(ad,format,caps,limits):
    if ad.version not in (0,1) or ad.shallow:raise ProtocolError('unsupported receive-pack discovery profile')
    if any(c.name not in _PUSH_CAPS for c in caps):raise ProtocolError('unsupported push certificate/options/capability')
    names={c.name for c in caps}
    if len(names&{b'report-status',b'report-status-v2'})!=1:raise ProtocolError('select one report-status capability')
    if _format(caps)!=format:raise ObjectFormatMismatch('push request format differs from local storage')
    return negotiate(ad,format,capabilities=caps,limits=limits)


def _commands(commands,format,limits):
    values=tuple(islice(commands,limits.max_refs+1))
    if not values or len(values)>limits.max_refs:raise ProtocolLimitExceeded('push command count exceeded or empty')
    names=set()
    for command in values:
        if not isinstance(command,PushCommand):raise TypeError('select PushCommand values')
        _name(command.name,limits,head=False)
        if command.name in names:raise ProtocolError('duplicate push ref')
        names.add(command.name)
        if any(oid is not None and oid.format!=format for oid in (command.old,command.new)):
            raise ObjectFormatMismatch('push IDs differ from negotiated storage')
    return values


def encode_push_request(negotiated,commands,pack=b'',*,limits=None):
    limits=limits or ProtocolLimits();n=negotiated
    if n.version not in (0,1) or n.command is not None:raise ProtocolError('select a legacy receive-pack negotiation')
    caps=_caps(n.capabilities,limits)
    if any(c.name not in _PUSH_CAPS for c in caps):raise ProtocolError('unsupported push capability')
    if _format(caps)!=n.format:raise ObjectFormatMismatch('push capability format differs from selected storage')
    if len({c.name for c in caps}&{b'report-status',b'report-status-v2'})!=1:raise ProtocolError('select one report-status capability')
    if not isinstance(pack,bytes):raise TypeError('push pack must be immutable bytes')
    commands=_commands(commands,n.format,limits)
    creates=any(c.new is not None for c in commands)
    if (creates and not pack.startswith(b'PACK')) or (not creates and pack):raise ProtocolError('pack presence differs from push commands')
    zero=b'0'*n.format.hex_size;packets=[]
    for i,command in enumerate(commands):
        line=(str(command.old).encode() if command.old else zero)+b' '+(str(command.new).encode() if command.new else zero)+b' '+command.name
        if i==0:line+=b'\0'+b' '.join(c.wire() for c in caps)
        packets.append(Packet(data=line))
    wire=_wire((*packets,FLUSH),limits)
    if len(wire)+len(pack)>limits.max_bytes:raise ProtocolLimitExceeded('push wire byte limit exceeded')
    return wire+pack


def parse_push_request(data,ad,format,*,limits=None):
    limits=limits or ProtocolLimits();format=ObjectFormat(format)
    if not isinstance(data,bytes):raise TypeError('push wire must be immutable bytes')
    if len(data)>limits.max_bytes:raise ProtocolLimitExceeded('push wire byte limit exceeded')
    decoder=PacketDecoder(limits=limits);cursor=0;packets=[]
    while cursor<len(data):
        chunk=data[cursor:cursor+decoder.needed];cursor+=len(chunk)
        frames=decoder.feed(chunk)
        if not frames:continue
        packet=frames[0]
        if packet==FLUSH:break
        if packet.kind is not PacketKind.DATA:raise ProtocolError('unexpected push command boundary')
        packets.append(packet)
        if len(packets)>limits.max_refs:raise ProtocolLimitExceeded('push ref count exceeded')
    else:raise ProtocolError('missing push command flush')
    decoder.finish()
    if not packets:raise ProtocolError('empty push request is not an update batch')
    first,separator,rawcaps=_text(packets[0],limits,nul=True).partition(b'\0')
    if not separator or b'\0' in rawcaps:raise ProtocolError('push capabilities require first command NUL')
    caps=_caps(rawcaps.split(b' ') if rawcaps else (),limits)
    negotiated=_push_caps(ad,format,caps,limits);commands=[]
    for i,packet in enumerate(packets):
        line=first if i==0 else _text(packet,limits)
        parts=line.split(b' ')
        if len(parts)!=3:raise ProtocolError('invalid push command')
        old,new=(_oid(part,format,zero=True) for part in parts[:2])
        commands.append(PushCommand(parts[2],old if any(old.bytes) else None,new if any(new.bytes) else None))
    commands=_commands(commands,format,limits);pack=data[cursor:]
    if any(c.new is None for c in commands) and not any(c.name==b'delete-refs' for c in ad.capabilities):
        raise ProtocolError('peer did not advertise ref deletion')
    creates=any(c.new is not None for c in commands)
    if (creates and not pack.startswith(b'PACK')) or (not creates and pack):raise ProtocolError('push pack boundary differs from commands')
    return PushRequest(commands,negotiated,pack)


@dataclass(frozen=True, slots=True)
class ReportOption:
    name: bytes
    value: bytes | None = field(default=None,repr=False)


@dataclass(frozen=True, slots=True)
class CommandStatus:
    name: bytes
    ok: bool
    error: bytes = field(default=b'',repr=False)
    options: tuple[ReportOption,...] = ()


@dataclass(frozen=True, slots=True)
class ReportStatus:
    unpack_ok: bool
    commands: tuple[CommandStatus,...]
    unpack_error: bytes = field(default=b'',repr=False)


def parse_report_status(packets,commands,format,*,version=1,limits=None):
    limits=limits or ProtocolLimits();format=ObjectFormat(format);expected={c.name for c in _commands(commands,format,limits)}
    if type(version) is not int or version not in (1,2):raise ProtocolError('unsupported report-status version')
    lines=_message(packets,limits)
    if not lines:raise ProtocolError('missing unpack report')
    first=_text(lines[0],limits)
    if not first.startswith(b'unpack ') or not first[7:]:raise ProtocolError('invalid unpack report')
    unpack_ok=first[7:]==b'ok';unpack_error=b'' if unpack_ok else first[7:]
    if len(unpack_error)>limits.max_error_bytes:raise ProtocolLimitExceeded('unpack error exceeds its byte limit')
    statuses=[];seen=set()
    for packet in lines[1:]:
        line=_text(packet,limits)
        if line.startswith(b'option '):
            if version!=2 or not statuses or not statuses[-1].ok:raise ProtocolError('unexpected report-status option')
            name,space,value=line[7:].partition(b' ')
            if name in {o.name for o in statuses[-1].options}:raise ProtocolError('duplicate report-status option')
            if name==b'refname' and space:_name(value,limits,head=False)
            elif name in (b'old-oid',b'new-oid') and space:_oid(value,format,zero=True)
            elif name==b'forced-update' and not space:pass
            else:raise ProtocolError('unsupported report-status option')
            statuses[-1]=replace(statuses[-1],options=(*statuses[-1].options,ReportOption(name,value if space else None)))
            continue
        status,space,rest=line.partition(b' ')
        if not space or status not in (b'ok',b'ng'):raise ProtocolError('invalid command report')
        name,space,error=rest.partition(b' ')
        _name(name,limits,head=False)
        if name not in expected or name in seen:raise ProtocolError('unknown or duplicate command report')
        if (status==b'ok' and space) or (status==b'ng' and (not space or not error)):raise ProtocolError('invalid command report error')
        if len(error)>limits.max_error_bytes:raise ProtocolLimitExceeded('command error exceeds its byte limit')
        seen.add(name);statuses.append(CommandStatus(name,status==b'ok',error))
    if seen!=expected:raise ProtocolError('missing command reports')
    if not unpack_ok and any(s.ok for s in statuses):raise ProtocolError('successful ref after unpack failure')
    return ReportStatus(unpack_ok,tuple(statuses),unpack_error)


def encode_report_status(report,commands,format,*,version=1,limits=None):
    limits=limits or ProtocolLimits()
    packets=[Packet(data=b'unpack '+(b'ok' if report.unpack_ok else report.unpack_error)+b'\n')]
    for status in report.commands:
        line=(b'ok ' if status.ok else b'ng ')+status.name+(b'' if status.ok else b' '+status.error)
        packets.append(Packet(data=line+b'\n'))
        for option in status.options:packets.append(Packet(data=b'option '+option.name+(b' '+option.value if option.value is not None else b'')+b'\n'))
    packets.append(FLUSH);parse_report_status(packets,commands,format,version=version,limits=limits)
    return _wire(packets,limits)
