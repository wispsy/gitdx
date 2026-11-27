"""Bounded ref/capability discovery and same-format negotiation transcripts."""

from dataclasses import dataclass, field, replace
import re

from .objects import ObjectFormat, ObjectId
from .pktline import (DELIMITER, FLUSH, RESPONSE_END, Packet, PacketKind, ProtocolBudget,
                      ProtocolError, ProtocolLimitExceeded, ProtocolLimits, RemoteProtocolError, encode_packet)


class ObjectFormatMismatch(ProtocolError):
    """Negotiation cannot translate objects between hash algorithms."""


@dataclass(frozen=True, slots=True)
class Capability:
    name: bytes
    value: bytes | None = field(default=None,repr=False)

    def wire(self):return self.name+(b"="+self.value if self.value is not None else b"")


@dataclass(frozen=True, slots=True)
class AdvertisedRef:
    name: bytes
    oid: ObjectId | None
    peeled: ObjectId | None = None
    symref: bytes | None = None


@dataclass(frozen=True, slots=True)
class Advertisement:
    version: int
    format: ObjectFormat
    capabilities: tuple[Capability, ...]
    refs: tuple[AdvertisedRef, ...] = ()
    shallow: tuple[ObjectId, ...] = ()

    def features(self,name):
        cap=next((c for c in self.capabilities if c.name==name),None)
        return frozenset(cap.value.split()) if cap and cap.value else frozenset()


@dataclass(frozen=True, slots=True)
class Negotiated:
    version: int
    format: ObjectFormat
    capabilities: tuple[Capability, ...]
    command: bytes | None = None

    @property
    def sideband_packet_size(self):
        names={c.name for c in self.capabilities}
        if b"side-band-64k" in names or (self.version==2 and self.command==b"fetch"):return 65520
        if b"side-band" in names:return 1000
        return None


@dataclass(frozen=True, slots=True)
class V2Command:
    name: bytes
    capabilities: tuple[Capability, ...]
    arguments: tuple[bytes, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class LegacyRequest:
    wants: tuple[ObjectId, ...]
    haves: tuple[ObjectId, ...]
    capabilities: tuple[Capability, ...]
    done: bool


def _frames(packets,limits):
    budget=ProtocolBudget(limits);result=[]
    for packet in packets:
        if not isinstance(packet,Packet):raise ProtocolError("expected packet values")
        budget.consume_packet();budget.consume_bytes(len(encode_packet(packet,limits=limits)));result.append(packet)
    return result


def _message(packets,limits,*,response_end=False):
    result=_frames(packets,limits)
    if response_end and result and result[-1]==RESPONSE_END:result.pop()
    if not result or result[-1]!=FLUSH:raise ProtocolError("message requires a final flush")
    result.pop()
    if FLUSH in result or RESPONSE_END in result:raise ProtocolError("unexpected message boundary")
    return result


def _text(packet,limits,*,nul=False):
    if packet.kind is not PacketKind.DATA:raise ProtocolError("expected a data line")
    data=packet.data.removesuffix(b"\n")
    if data.startswith(b"ERR "):
        if len(data)-4>limits.max_error_bytes:raise ProtocolLimitExceeded("remote error exceeds its byte limit")
        raise RemoteProtocolError("remote protocol error")
    if b"\r" in data or b"\n" in data or (not nul and b"\0" in data):raise ProtocolError("invalid protocol text line")
    return data


def _capability(data,limits):
    if not isinstance(data,bytes) or len(data)>limits.max_name_bytes:raise ProtocolLimitExceeded("capability size limit exceeded")
    name,equals,value=data.partition(b"=")
    if not re.fullmatch(rb"[A-Za-z0-9_-]+",name) or (equals and (not value or any(c<32 or c==127 or (c>126 and name!=b"symref") for c in value))):raise ProtocolError("invalid capability")
    return Capability(name,value if equals else None)


def _caps(items,limits,*,repeat=()):
    result=[];names=set()
    for item in items:
        if len(result)>=limits.max_capabilities:raise ProtocolLimitExceeded("capability count exceeded")
        cap=_capability(item.wire() if isinstance(item,Capability) else item,limits)
        if cap.name in names and cap.name not in repeat:raise ProtocolError("duplicate capability")
        names.add(cap.name);result.append(cap)
    return tuple(result)


def _format(caps):
    values=[c.value for c in caps if c.name==b"object-format"]
    if not values:return ObjectFormat.SHA1
    if len(values)!=1 or values[0] not in (b"sha1",b"sha256"):raise ProtocolError("unsupported object-format capability")
    return ObjectFormat(values[0].decode("ascii"))


def _oid(data,format,*,zero=False):
    if len(data)!=format.hex_size or not re.fullmatch(rb"[0-9A-Fa-f]+",data):raise ProtocolError("object ID does not match the negotiated format")
    oid=ObjectId(data.decode("ascii").lower(),format)
    if not zero and not any(oid.bytes):raise ProtocolError("null object ID is not valid here")
    return oid


def _name(name,limits,*,head=True):
    if len(name)>limits.max_name_bytes:raise ProtocolLimitExceeded("ref name exceeds its byte limit")
    if head and name==b"HEAD":return name
    if (not name.startswith(b"refs/") or any(c<=32 or c==127 or c in b"~^:?*[\\" for c in name)
            or b".." in name or b"@{" in name or name.endswith((b"/",b"."))
            or any(not part or part.startswith(b".") or part.endswith(b".lock") for part in name.split(b"/"))):
        raise ProtocolError("invalid wire ref name")
    return name


def parse_advertisement(packets,*,limits=None):
    limits=limits or ProtocolLimits();lines=_message(packets,limits);version=0
    if lines and lines[0].kind is PacketKind.DATA and _text(lines[0],limits,nul=True).startswith(b"version "):
        first=_text(lines.pop(0),limits)
        if first not in (b"version 1",b"version 2"):raise ProtocolError("unsupported protocol version")
        version=int(first[-1:])
    if version==2:
        caps=_caps((_text(p,limits) for p in lines),limits)
        return Advertisement(version,_format(caps),caps)
    if not lines:return Advertisement(version,ObjectFormat.SHA1,())
    first,separator,cap_data=_text(lines[0],limits,nul=True).partition(b"\0")
    if not separator or b"\0" in cap_data:raise ProtocolError("legacy advertisement requires first-line capabilities")
    caps=_caps(cap_data.split(b" ") if cap_data else (),limits,repeat=(b"symref",));format=_format(caps)
    records=[];names=set();shallow=[];in_shallow=False
    for i,packet in enumerate(lines):
        line=first if i==0 else _text(packet,limits)
        if line.startswith(b"shallow "):
            shallow.append(_oid(line[8:],format));in_shallow=True
            if len(shallow)>limits.max_refs:raise ProtocolLimitExceeded("shallow count exceeded")
            continue
        if in_shallow:raise ProtocolError("ref after shallow boundary")
        oid_data,space,name=line.partition(b" ")
        if not space:raise ProtocolError("invalid legacy ref record")
        oid=_oid(oid_data,format,zero=i==0)
        if name==b"capabilities^{}" and i==0:
            if any(oid.bytes) or len(lines)!=1:raise ProtocolError("invalid empty advertisement")
            continue
        if not any(oid.bytes):raise ProtocolError("null advertised ref")
        if name.endswith(b"^{}"):
            if not records or records[-1].name!=name[:-3] or records[-1].peeled is not None:raise ProtocolError("orphan peeled ref")
            records[-1]=replace(records[-1],peeled=oid);continue
        _name(name,limits)
        if name in names:raise ProtocolError("duplicate advertised ref")
        if len(records)>=limits.max_refs:raise ProtocolLimitExceeded("advertised ref count exceeded")
        records.append(AdvertisedRef(name,oid));names.add(name)
    symrefs={}
    for cap in caps:
        if cap.name==b"symref":
            source,colon,target=(cap.value or b"").partition(b":")
            if not colon or source in symrefs:raise ProtocolError("invalid symref capability")
            _name(source,limits);_name(target,limits,head=False);symrefs[source]=target
    return Advertisement(version,format,caps,tuple(replace(r,symref=symrefs.get(r.name)) for r in records),tuple(shallow))


def _wire(packets,limits):
    return b"".join(encode_packet(p,limits=limits) for p in _frames(packets,limits))


def encode_advertisement(advertisement,*,limits=None):
    limits=limits or ProtocolLimits();ad=advertisement
    if ad.version not in (0,1,2):raise ProtocolError("unsupported protocol version")
    caps=_caps(ad.capabilities,limits,repeat=(b"symref",))
    if _format(caps)!=ad.format:raise ObjectFormatMismatch("object-format advertisement cannot translate objects")
    packets=[Packet(data=f"version {ad.version}\n".encode())] if ad.version else []
    if ad.version==2:
        if ad.refs or ad.shallow:raise ProtocolError("v2 refs require a separate ls-refs response")
        packets.extend(Packet(data=c.wire()+b"\n") for c in caps)
    else:
        extra=[]
        for i,record in enumerate(ad.refs):
            if i>=limits.max_refs:raise ProtocolLimitExceeded("advertised ref count exceeded")
            if record.symref is not None:
                _name(record.symref,limits,head=False)
                value=record.name+b":"+record.symref
                if not any(c.name==b"symref" and c.value==value for c in caps):extra.append(Capability(b"symref",value))
            if len(extra)>limits.max_capabilities:raise ProtocolLimitExceeded("symref capability count exceeded")
        caps=_caps((*caps,*extra),limits,repeat=(b"symref",))
        capabilities=b" ".join(c.wire() for c in caps)
        if not ad.refs:packets.append(Packet(data=b"0"*ad.format.hex_size+b" capabilities^{}\0"+capabilities+b"\n"))
        for i,record in enumerate(ad.refs):
            if i>=limits.max_refs:raise ProtocolLimitExceeded("advertised ref count exceeded")
            _name(record.name,limits)
            if record.oid is None or record.oid.format!=ad.format:raise ProtocolError("legacy ref requires a format-consistent ID")
            packets.append(Packet(data=str(record.oid).encode()+b" "+record.name+(b"\0"+capabilities if i==0 else b"")+b"\n"))
            if record.peeled is not None:
                if record.peeled.format!=ad.format:raise ObjectFormatMismatch("peeled ID format mismatch")
                packets.append(Packet(data=str(record.peeled).encode()+b" "+record.name+b"^{}\n"))
        for i,oid in enumerate(ad.shallow):
            if i>=limits.max_refs:raise ProtocolLimitExceeded("shallow count exceeded")
            if oid.format!=ad.format:raise ObjectFormatMismatch("shallow ID format mismatch")
            packets.append(Packet(data=b"shallow "+str(oid).encode()+b"\n"))
    packets.append(FLUSH);wire=_wire(packets,limits)
    # Validate emission with the same boundary/type rules as reception.
    parse_advertisement(packets,limits=limits)
    return wire


def negotiate(advertisement,local_format,*,capabilities=(),command=None,limits=None):
    limits=limits or ProtocolLimits();format=ObjectFormat(local_format);ad=advertisement
    if ad.format!=format:raise ObjectFormatMismatch("peer and local object formats differ; conversion is not negotiated")
    selected=list(_caps(capabilities,limits,repeat=(b"server-option",)))
    available={c.name:c for c in ad.capabilities}
    if ad.version==2 and command is not None and (command not in (b"fetch",b"ls-refs") or command not in available):raise ProtocolError("unsupported or unadvertised v2 command")
    if ad.version!=2 and command is not None:raise ProtocolError("legacy negotiation has no v2 command")
    for cap in selected:
        if cap.name not in available or cap.name==b"symref":raise ProtocolError("unadvertised or informational capability selected")
        if cap.name in (b"agent",b"object-format",b"server-option") and cap.value is None:raise ProtocolError("selected capability requires a value")
        if cap.value is not None and available[cap.name].value is None and cap.name!=b"server-option":raise ProtocolError("flag capability does not accept a value")
        if ad.version==2 and cap.name not in (b"agent",b"object-format",b"server-option"):raise ProtocolError("unsupported v2 request capability")
        if cap.name==b"object-format" and cap.value!=format.value.encode():raise ObjectFormatMismatch("selected object-format does not match local storage")
    if b"object-format" in available and not any(c.name==b"object-format" for c in selected):selected.append(Capability(b"object-format",format.value.encode()))
    names={c.name for c in selected}
    if {b"side-band",b"side-band-64k"}<=names or {b"multi_ack",b"multi_ack_detailed"}<=names:raise ProtocolError("mutually exclusive capabilities selected")
    if len(selected)>limits.max_capabilities:raise ProtocolLimitExceeded("capability count exceeded")
    return Negotiated(ad.version,format,tuple(selected),command)


def _arguments(arguments,format,limits,*,fetch=False):
    result=[]
    for arg in arguments:
        if len(result)>=limits.max_packets:raise ProtocolLimitExceeded("argument count exceeded")
        line=_text(Packet(data=arg),limits)
        if not line:raise ProtocolError("empty command argument")
        if line.startswith(b"command="):raise ProtocolError("a command cannot appear in its argument section")
        if fetch and (line.startswith(b"want ") or line.startswith(b"have ")):_oid(line[5:],format)
        result.append(line)
    return tuple(result)


def encode_v2_command(negotiated,arguments=(),*,limits=None):
    limits=limits or ProtocolLimits();n=negotiated
    if n.version!=2 or n.command not in (b"fetch",b"ls-refs"):raise ProtocolError("select a negotiated v2 command")
    args=_arguments(arguments,n.format,limits,fetch=n.command==b"fetch")
    packets=[Packet(data=b"command="+n.command+b"\n"),*(Packet(data=c.wire()+b"\n") for c in n.capabilities),DELIMITER,
             *(Packet(data=a+b"\n") for a in args),FLUSH]
    return _wire(packets,limits)


def parse_v2_command(packets,advertisement,local_format,*,limits=None):
    limits=limits or ProtocolLimits();lines=_message(packets,limits)
    if advertisement.version!=2:raise ProtocolError("v2 command requires v2 discovery")
    if not lines:return None
    command=_text(lines[0],limits)
    if not command.startswith(b"command=") or lines.count(DELIMITER)!=1:raise ProtocolError("invalid v2 command boundary")
    split=lines.index(DELIMITER)
    if split==0:raise ProtocolError("missing v2 command")
    caps=_caps((_text(p,limits) for p in lines[1:split]),limits,repeat=(b"server-option",))
    if _format(caps)!=ObjectFormat(local_format):raise ObjectFormatMismatch("request object-format does not match local storage")
    selected=negotiate(advertisement,local_format,capabilities=caps,command=command[8:],limits=limits)
    args=_arguments((_text(p,limits) for p in lines[split+1:]),selected.format,limits,fetch=selected.command==b"fetch")
    return V2Command(selected.command,caps,args)


def parse_ls_refs(packets,format,*,limits=None):
    limits=limits or ProtocolLimits();format=ObjectFormat(format);result=[];names=set()
    for packet in _message(packets,limits,response_end=True):
        parts=_text(packet,limits).split(b" ")
        if len(parts)<2:raise ProtocolError("invalid ls-refs record")
        oid=None if parts[0]==b"unborn" else _oid(parts[0],format);name=_name(parts[1],limits)
        if name in names:raise ProtocolError("duplicate ls-refs record")
        if len(result)>=limits.max_refs:raise ProtocolLimitExceeded("ls-refs count exceeded")
        peeled=symref=None;attributes=set()
        for attribute in parts[2:]:
            key,colon,value=attribute.partition(b":")
            if not colon or not key or not value or key in attributes:raise ProtocolError("invalid ref attribute")
            attributes.add(key)
            if key==b"peeled":peeled=_oid(value,format)
            elif key==b"symref-target":symref=_name(value,limits,head=False)
        if oid is None and (symref is None or peeled is not None):raise ProtocolError("unborn ref requires a symbolic target")
        names.add(name);result.append(AdvertisedRef(name,oid,peeled,symref))
    return tuple(result)


def encode_legacy_request(advertisement,local_format,wants,*,haves=(),capabilities=(),done=True,limits=None):
    if type(done) is not bool:raise TypeError("done must be an explicit boolean")
    limits=limits or ProtocolLimits();n=negotiate(advertisement,local_format,capabilities=capabilities,limits=limits)
    if n.version not in (0,1):raise ProtocolError("legacy request requires legacy discovery")
    wanted=[];known={r.oid for r in advertisement.refs}|{r.peeled for r in advertisement.refs if r.peeled}
    for oid in wants:
        if len(wanted)>=limits.max_refs:raise ProtocolLimitExceeded("want count exceeded")
        if not isinstance(oid,ObjectId) or oid.format!=n.format or oid not in known:raise ProtocolError("legacy want was not advertised")
        wanted.append(oid)
    if not wanted:return encode_packet(FLUSH)
    caps=b" ".join(c.wire() for c in n.capabilities)
    packets=[Packet(data=b"want "+str(oid).encode()+(b" "+caps if i==0 and caps else b"")+b"\n") for i,oid in enumerate(wanted)]+[FLUSH]
    for oid in haves:
        if len(packets)>=limits.max_packets:raise ProtocolLimitExceeded("have count exceeded")
        if not isinstance(oid,ObjectId) or oid.format!=n.format or not any(oid.bytes):raise ProtocolError("invalid have ID")
        packets.append(Packet(data=b"have "+str(oid).encode()+b"\n"))
    packets.append(Packet(data=b"done\n") if done else FLUSH)
    return _wire(packets,limits)


def parse_legacy_request(packets,advertisement,local_format,*,limits=None):
    limits=limits or ProtocolLimits();frames=_frames(packets,limits)
    if advertisement.version not in (0,1) or not frames:raise ProtocolError("legacy request requires legacy discovery")
    if frames==[FLUSH]:return LegacyRequest((),(),(),False)
    try:split=frames.index(FLUSH)
    except ValueError:raise ProtocolError("want phase requires a flush") from None
    if split==0:raise ProtocolError("missing want phase")
    first=_text(frames[0],limits).split(b" ")
    if len(first)<2 or first[0]!=b"want":raise ProtocolError("invalid first want")
    caps=_caps(first[2:],limits)
    if any(c.name==b'object-format' for c in caps) and _format(caps)!=ObjectFormat(local_format):
        raise ObjectFormatMismatch("request object-format does not match local storage")
    # Native legacy clients can infer the advertised format without echoing
    # object-format. Every want/have must still use that exact digest width.
    n=negotiate(advertisement,local_format,capabilities=caps,limits=limits)
    known={r.oid for r in advertisement.refs}|{r.peeled for r in advertisement.refs if r.peeled};wants=[];haves=[];done=False
    for i,p in enumerate(frames[:split]):
        parts=first[:2] if i==0 else _text(p,limits).split(b" ")
        if len(parts)!=2 or parts[0]!=b"want":raise ProtocolError("unsupported legacy want argument")
        oid=_oid(parts[1],n.format)
        if oid not in known:raise ProtocolError("legacy want was not advertised")
        wants.append(oid)
    tail=frames[split+1:]
    if not tail:raise ProtocolError("missing legacy negotiation boundary")
    for i,p in enumerate(tail):
        if p==FLUSH:
            if i!=len(tail)-1:raise ProtocolError("trailing data after have boundary")
            continue
        line=_text(p,limits)
        if line==b"done":
            if i!=len(tail)-1:raise ProtocolError("trailing data after done")
            done=True
        elif line.startswith(b"have "):haves.append(_oid(line[5:],n.format))
        else:raise ProtocolError("unsupported legacy negotiation argument")
    if not done and tail[-1]!=FLUSH:raise ProtocolError("missing have boundary")
    if len(wants)>limits.max_refs or len(haves)>limits.max_refs:raise ProtocolLimitExceeded("negotiation ID count exceeded")
    return LegacyRequest(tuple(wants),tuple(haves),caps,done)


def parse_ack(packet,format,*,limits=None):
    limits=limits or ProtocolLimits();line=_text(packet,limits)
    if line==b"NAK":return None,None
    parts=line.split(b" ")
    if len(parts) not in (2,3) or parts[0]!=b"ACK" or (len(parts)==3 and parts[2] not in (b"continue",b"common",b"ready")):raise ProtocolError("invalid ACK/NAK record")
    return _oid(parts[1],ObjectFormat(format)),parts[2] if len(parts)==3 else None
