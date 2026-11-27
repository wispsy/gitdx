"""Read-only stateless upload-pack sessions; authentication belongs to the host."""

from dataclasses import dataclass, field
from itertools import chain

from .errors import InvalidObject, UnsupportedFormat
from .history import HistoryLimits, list_refs
from .objects import ObjectType, Tag
from .pack_writer import PackWriteLimits, reachable_objects, write_pack
from .pktline import (FLUSH, Packet, PacketDecoder, ProtocolBudget, ProtocolError,
                      ProtocolLimitExceeded, ProtocolLimits, encode_packet, encode_sideband)
from .protocol import (Advertisement, AdvertisedRef, Capability, _oid, encode_advertisement,
                       parse_legacy_request, parse_v2_command)
from .refs import SymbolicRef


@dataclass(frozen=True, slots=True)
class UploadPackLimits:
    protocol: ProtocolLimits = field(default_factory=lambda:ProtocolLimits(max_bytes=1024*1024,
        max_packets=16_000,max_refs=10_000,max_capabilities=64,max_name_bytes=1024))
    graph: PackWriteLimits = field(default_factory=lambda:PackWriteLimits(max_object_bytes=16*1024*1024,
        max_bytes=128*1024*1024,max_pack_bytes=60*1024*1024))
    max_response_bytes: int = 64*1024*1024
    max_wants: int = 256
    max_haves: int = 4096
    max_prefixes: int = 128

    def __post_init__(self):
        if not isinstance(self.protocol,ProtocolLimits) or not isinstance(self.graph,PackWriteLimits):raise TypeError('select protocol and graph limits')
        if any(type(v) is not int or v<=0 for v in (self.max_response_bytes,self.max_wants,self.max_haves,self.max_prefixes)):
            raise ValueError('upload-pack limits must be positive integers')


class UploadPack:
    """One authorized request, with a shared deadline and cancellation budget.

    Wants are limited to advertised tips/peeled tags. Negotiation acknowledges
    common objects but sends complete non-thin closures, without exclusion or
    shallow/filter/reachable-object-want features. No repository mutation occurs.
    """

    def __init__(self,repository,*,limits=None,cancel=None):
        self.repo=repository;self.limits=limits or UploadPackLimits()
        if not isinstance(self.limits,UploadPackLimits):raise TypeError('select UploadPackLimits')
        self.budget=ProtocolBudget(self.limits.protocol,cancelled=cancel.is_set if cancel is not None else None)

    def _refs(self):
        self.budget.check();repo=self.repo;head=repo.refs.read('HEAD');resolved=repo.refs.resolve()
        limits=HistoryLimits(max_refs=self.limits.protocol.max_refs)
        values=tuple(item for prefix in ('refs/heads/','refs/tags/') for item in list_refs(repo,prefix,limits=limits,check=self.budget.check))
        if len(values)+1>self.limits.protocol.max_refs:raise ProtocolLimitExceeded('upload-pack ref count exceeded')
        records=[]
        if resolved.oid is not None or isinstance(head,SymbolicRef):
            records.append(AdvertisedRef(b'HEAD',resolved.oid,symref=head.target.encode() if isinstance(head,SymbolicRef) else None))
        for name,oid in values:
            self.budget.check()
            if oid is None:continue
            peeled=None;current=oid;seen=set()
            for _ in range(32):
                self.budget.check()
                if current in seen:raise InvalidObject('annotated tag cycle')
                seen.add(current);raw=repo.objects.read(current)
                if len(raw.data)>self.limits.graph.max_object_bytes:raise ProtocolLimitExceeded('advertised target object exceeds limit')
                if raw.kind is not ObjectType.TAG:break
                current=Tag.from_bytes(raw.data,repo.object_format).target;peeled=current
            else:raise ProtocolLimitExceeded('annotated tag depth exceeded')
            records.append(AdvertisedRef(name.encode(),oid,peeled))
        if repo.refs.read('HEAD')!=head or repo.refs.resolve()!=resolved:raise ProtocolError('HEAD changed during upload-pack discovery')
        return tuple(records)

    def _advertisement(self,version,records):
        if type(version) is not int or version not in (0,1,2):raise ProtocolError('unsupported upload-pack protocol version')
        caps=[Capability(b'agent',b'gitdx'),Capability(b'object-format',self.repo.object_format.value.encode())]
        if version==2:caps.extend((Capability(b'ls-refs',b'unborn'),Capability(b'fetch',b'wait-for-done')))
        else:
            caps.extend((Capability(b'multi_ack_detailed'),Capability(b'side-band-64k'),Capability(b'no-progress'),Capability(b'include-tag')))
            for record in records:
                if record.name==b'HEAD' and record.symref is not None:caps.append(Capability(b'symref',b'HEAD:'+record.symref))
        return Advertisement(version,self.repo.object_format,tuple(caps),() if version==2 else tuple(r for r in records if r.oid is not None))

    def _output(self,chunks):
        result=bytearray()
        for chunk in chunks:
            self.budget.check()
            if len(result)+len(chunk)>self.limits.max_response_bytes:raise ProtocolLimitExceeded('upload-pack response bytes exceeded')
            result.extend(chunk)
        return bytes(result)

    def advertise(self,version=0,*,http=False):
        self.budget.check();records=() if version==2 else self._refs()
        ad=self._advertisement(version,records)
        prefix=encode_packet(Packet(data=b'# service=git-upload-pack\n'))+encode_packet(FLUSH) if http and version!=2 else b''
        return self._output((prefix,encode_advertisement(ad,limits=self.limits.protocol)))

    def respond(self,data,version=0):
        self.budget.check()
        if not isinstance(data,bytes):raise TypeError('upload-pack request must be immutable bytes')
        decoder=PacketDecoder(budget=self.budget);packets=decoder.feed(data);decoder.finish()
        records=self._refs();ad=self._advertisement(version,records)
        if version==2:
            command=parse_v2_command(packets,ad,self.repo.object_format,limits=self.limits.protocol)
            if command is None:return b''
            if command.name==b'ls-refs':return self._ls_refs(command.arguments,records)
            wants=[];haves=[];flags=set()
            for arg in command.arguments:
                self.budget.check()
                if arg.startswith(b'want '):wants.append(_oid(arg[5:],self.repo.object_format))
                elif arg.startswith(b'have '):haves.append(_oid(arg[5:],self.repo.object_format))
                elif arg in (b'thin-pack',b'ofs-delta',b'no-progress',b'include-tag',b'done',b'wait-for-done'):
                    if arg in flags:raise ProtocolError('duplicate fetch argument')
                    flags.add(arg)
                else:raise UnsupportedFormat('unsupported upload-pack fetch argument')
            done=b'done' in flags;include_tags=b'include-tag' in flags;sideband=True;detailed=True
        else:
            request=parse_legacy_request(packets,ad,self.repo.object_format,limits=self.limits.protocol)
            wants=request.wants;haves=request.haves;done=request.done;flags={c.name for c in request.capabilities}
            include_tags=b'include-tag' in flags;sideband=b'side-band-64k' in flags;detailed=b'multi_ack_detailed' in flags
        if not wants:
            if version!=2 and len(packets)==1 and packets[0]==FLUSH:return b''
            raise ProtocolError('fetch requires at least one advertised want')
        if len(wants)>self.limits.max_wants or len(haves)>self.limits.max_haves:raise ProtocolLimitExceeded('upload-pack negotiation ID count exceeded')
        known={r.oid for r in records if r.oid}|{r.peeled for r in records if r.peeled}
        if any(oid not in known for oid in wants):raise ProtocolError('want does not name an advertised tip')
        outer=self.repo.objects;check=self.budget.check
        class CheckedStore:
            format=outer.format
            def read(self,oid):check();return outer.read(oid)
        store=CheckedStore();closure=reachable_objects(store,wants,limits=self.limits.graph,check=check)
        available={oid for oid,_ in closure};common=tuple(dict.fromkeys(oid for oid in haves if oid in available))
        frame=lambda line:encode_packet(Packet(data=line+b'\n'),limits=self.limits.protocol)
        if not done and (version==2 or detailed or not common):
            if version==2:
                lines=[frame(b'acknowledgments')]
                lines.extend(frame(b'ACK '+str(oid).encode()) for oid in common)
                if not common:lines.append(frame(b'NAK'))
                lines.append(encode_packet(FLUSH))
            else:lines=[*(frame(b'ACK '+str(oid).encode()+b' common') for oid in common),frame(b'NAK')]
            return self._output(lines)
        roots=list(dict.fromkeys(wants))
        if include_tags:roots.extend(r.oid for r in records if r.peeled in available and r.oid not in available)
        # Reuse the checked closure without repeating repository reads.
        cached=dict(closure)
        class CachedStore:
            format=outer.format
            def read(self,oid):check();return cached.get(oid) or outer.read(oid)
        pack=write_pack(CachedStore(),roots,limits=self.limits.graph,check=check).pack
        prefix=frame(b'packfile') if version==2 else frame(b'ACK '+str(common[-1]).encode() if common else b'NAK')
        if sideband:
            chunks=(encode_sideband(1,pack[p:p+65515]) for p in range(0,len(pack),65515))
            return self._output(chain((prefix,),chunks,(encode_packet(FLUSH),)))
        return self._output((prefix,pack))

    def _ls_refs(self,arguments,records):
        flags=set();prefixes=[]
        for arg in arguments:
            self.budget.check()
            if arg in (b'peel',b'symrefs',b'unborn'):
                if arg in flags:raise ProtocolError('duplicate ls-refs argument')
                flags.add(arg)
            elif arg.startswith(b'ref-prefix '):
                prefix=arg[11:]
                if not prefix or len(prefix)>self.limits.protocol.max_name_bytes:raise ProtocolError('invalid ref prefix')
                prefixes.append(prefix)
                if len(prefixes)>self.limits.max_prefixes:raise ProtocolLimitExceeded('ref prefix count exceeded')
            else:raise UnsupportedFormat('unsupported ls-refs argument')
        lines=[]
        for record in records:
            self.budget.check()
            if prefixes and not any(record.name.startswith(p) for p in prefixes):continue
            if record.oid is None and b'unborn' not in flags:continue
            line=(str(record.oid).encode() if record.oid else b'unborn')+b' '+record.name
            if record.peeled and b'peel' in flags:line+=b' peeled:'+str(record.peeled).encode()
            if record.symref and b'symrefs' in flags:line+=b' symref-target:'+record.symref
            lines.append(encode_packet(Packet(data=line+b'\n'),limits=self.limits.protocol))
        return self._output((*lines,encode_packet(FLUSH)))

    def chunks(self,data,*,size=65536):
        if type(size) is not int or not 1<=size<=1024*1024:raise ValueError('select bounded response chunks')
        for position in range(0,len(data),size):
            self.budget.check();yield data[position:position+size]
