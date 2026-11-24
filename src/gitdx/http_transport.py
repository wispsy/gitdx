"""Direct smart HTTP with explicit credentials, verified TLS and no redirects."""

from dataclasses import dataclass, field, replace
import base64
import http.client
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urlsplit

from .errors import UnsupportedFormat
from .pktline import (DELIMITER, FLUSH, RESPONSE_END, Packet, PacketDecoder, PacketKind, ProtocolBudget,
                      ProtocolError, ProtocolLimits, RemoteProtocolError, decode_sideband)
from .protocol import (encode_legacy_request, encode_v2_command, negotiate, parse_ack,
                       parse_advertisement, parse_ls_refs)
from .transfers import TransferCancelled, TransferError, TransferTimeout


@dataclass(frozen=True, slots=True)
class HTTPAuthorization:
    value: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.value,str) or not 1<=len(self.value)<=8192 or any(ord(c)<32 or ord(c)>126 for c in self.value):
            raise ValueError('authorization must be a bounded ASCII header')
        if not self.value.startswith(('Basic ','Bearer ')):raise ValueError('select Basic or Bearer authorization')

    @classmethod
    def basic(cls,username,password):
        if ':' in username:raise ValueError('Basic username cannot contain a colon')
        return cls('Basic '+base64.b64encode((username+':'+password).encode()).decode('ascii'))

    @classmethod
    def bearer(cls,token):
        if not token or any(c.isspace() for c in token):raise ValueError('Bearer token cannot contain whitespace')
        return cls('Bearer '+token)


def _resolve(host,port,control):
    """Bound caller waiting for DNS; a cancelled lookup has no mutation callback."""
    result=queue.Queue(maxsize=1)
    def lookup():
        try:result.put((True,socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)))
        except OSError:result.put((False,None))
    threading.Thread(target=lookup,daemon=True).start()
    while True:
        control.check()
        try:ok,addresses=result.get(timeout=min(.05,max(.001,control.deadline-time.monotonic())))
        except queue.Empty:continue
        control.check()
        if not ok:raise TransferError('HTTP address lookup failed')
        if len(addresses)>32:raise TransferError('HTTP address count exceeded')
        return addresses


class _SocketScope:
    def __init__(self,control):self.control=control;self.sock=None;self.stop=threading.Event();self.guard=threading.Lock()

    def set(self,sock):
        with self.guard:self.sock=sock
        self.control.check()

    def watch(self):
        while not self.stop.wait(.025):
            try:self.control.check()
            except (TransferCancelled,TransferTimeout):
                with self.guard:
                    if self.sock is not None:
                        try:self.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                return

    def __enter__(self):
        self.thread=threading.Thread(target=self.watch,daemon=True);self.thread.start();return self

    def __exit__(self,*args):
        self.stop.set()
        with self.guard:
            if self.sock is not None:self.sock.close()
        self.thread.join(timeout=.2)


class SmartHTTPTransport:
    def __init__(self,url,*,authorization=None,allow_http=False,protocol_version=2,ssl_context=None,idle_timeout=10):
        if not isinstance(url,str) or len(url)>4096 or any(ord(c)<=32 or ord(c)==127 for c in url) or '\\' in url:
            raise ValueError('invalid HTTP repository URL')
        try:parts=urlsplit(url);port=parts.port
        except ValueError:raise ValueError('invalid HTTP repository URL') from None
        if parts.scheme not in ('http','https') or not parts.hostname or '@' in parts.netloc or parts.query or parts.fragment:
            raise ValueError('select an HTTP repository URL without userinfo/query/fragment')
        if parts.scheme=='http' and allow_http is not True:raise ValueError('cleartext HTTP requires allow_http=True')
        if '%' in parts.netloc or re.search(r'%(?![0-9a-fA-F]{2})',parts.path):raise ValueError('invalid HTTP URL encoding')
        try:decoded=unquote(parts.path,errors='strict')
        except UnicodeError as error:raise ValueError('invalid HTTP URL encoding') from error
        if any(p in ('.','..') for p in decoded.split('/')) or re.search(r'%(?:2f|5c|25)',parts.path,re.I) or any(ord(c)<32 or ord(c)==127 for c in decoded):
            raise ValueError('HTTP URL contains ambiguous or traversing path components')
        if any(ord(c)>127 for c in parts.path):raise ValueError('HTTP URL path must be percent-encoded')
        if authorization is not None and not isinstance(authorization,HTTPAuthorization):raise TypeError('select HTTPAuthorization explicitly')
        if type(protocol_version) is not int or protocol_version not in (0,1,2):raise ValueError('select protocol version 0, 1 or 2')
        if type(idle_timeout) not in (int,float) or not 0<idle_timeout<=300:raise ValueError('invalid HTTP idle timeout')
        if ssl_context is not None and not isinstance(ssl_context,ssl.SSLContext):raise TypeError('select an SSLContext explicitly')
        context=ssl_context or ssl.create_default_context()
        if not context.check_hostname or context.verify_mode!=ssl.CERT_REQUIRED:raise ValueError('HTTPS requires certificate and hostname verification')
        try:self.host=parts.hostname.encode('idna').decode('ascii')
        except UnicodeError:raise ValueError('invalid HTTP hostname') from None
        self.port=port if port is not None else (443 if parts.scheme=='https' else 80)
        if not 1<=self.port<=65535:raise ValueError('invalid HTTP port')
        self.secure=parts.scheme=='https';self.path=parts.path.rstrip('/');self.authorization=authorization
        self.protocol_version=protocol_version;self.context=context;self.idle_timeout=idle_timeout

    def __repr__(self):return f'SmartHTTPTransport(protocol_version={self.protocol_version})'

    def _limits(self,control):
        control.check()
        return ProtocolLimits(max_bytes=control.limits.max_wire_bytes,max_refs=control.limits.max_refs+1,
                              timeout=max(.001,control.deadline-time.monotonic()))

    def _packets(self,data,control):
        limits=self._limits(control);decoder=PacketDecoder(budget=ProtocolBudget(limits,cancelled=(control.cancel.is_set if control.cancel else None)))
        result=[]
        for start in range(0,len(data),65536):
            control.check();result.extend(decoder.feed(data[start:start+65536]))
        decoder.finish();control.check();return tuple(result)

    def _request(self,method,suffix,body,content_type,control,*,protocol_version=None):
        control.check()
        connection=http.client.HTTPConnection(self.host,self.port,timeout=min(self.idle_timeout,max(.001,control.deadline-time.monotonic())))
        with _SocketScope(control) as scope:
            def connect():
                addresses=_resolve(self.host,self.port,control)
                for family,kind,proto,_,address in addresses:
                    control.check();sock=socket.socket(family,kind,proto);scope.set(sock)
                    sock.settimeout(min(self.idle_timeout,max(.001,control.deadline-time.monotonic())))
                    try:sock.connect(address)
                    except OSError:
                        sock.close();control.check();continue
                    if self.secure:
                        sock=self.context.wrap_socket(sock,server_hostname=self.host,do_handshake_on_connect=False)
                        scope.set(sock);sock.do_handshake()
                    connection.sock=sock;return
                raise TransferError('HTTP connection failed')
            connection.connect=connect
            headers={'Accept':content_type,'Accept-Encoding':'identity','Connection':'close'}
            version=self.protocol_version if protocol_version is None else protocol_version
            if version:headers['Git-Protocol']='version='+str(version)
            if self.authorization is not None:headers['Authorization']=self.authorization.value
            if body is not None:headers['Content-Type']='application/x-git-upload-pack-request'
            response=None
            try:
                if body is not None:control.consume(len(body))
                connection.request(method,self.path+suffix,body=body,headers=headers)
                response=connection.getresponse();control.check()
                if response.status in (401,403):raise TransferError('HTTP authorization refused')
                if 300<=response.status<400:raise TransferError('HTTP redirects are unsupported')
                if response.status!=200:raise TransferError('HTTP server refused transfer')
                if response.getheader('Content-Type','').split(';',1)[0].strip().lower()!=content_type:
                    raise UnsupportedFormat('HTTP response is not the selected smart Git service')
                if response.getheader('Content-Encoding','identity').lower()!='identity':raise UnsupportedFormat('HTTP content encoding is unsupported')
                lengths=response.headers.get_all('Content-Length',[]);encoding=response.getheader('Transfer-Encoding')
                if len(lengths)>1 or (lengths and encoding):raise TransferError('ambiguous HTTP response framing')
                if encoding and encoding.lower()!='chunked':raise TransferError('unsupported HTTP transfer encoding')
                declared=None
                if lengths:
                    if not re.fullmatch(r'[0-9]{1,20}',lengths[0]):raise TransferError('invalid HTTP content length')
                    declared=int(lengths[0])
                    if declared>control.limits.max_wire_bytes-control.bytes:raise TransferError('HTTP response byte limit exceeded')
                data=bytearray()
                while True:
                    control.check();chunk=response.read1(65536)
                    if not chunk:break
                    control.consume(len(chunk));data.extend(chunk)
                control.check()
                if declared is not None and len(data)!=declared:raise TransferError('truncated HTTP response')
                return bytes(data)
            except (OSError,http.client.HTTPException) as error:
                control.check()
                if isinstance(error,TimeoutError):raise TransferTimeout('HTTP idle timeout exceeded') from None
                raise TransferError('HTTP connection or framing failed') from None
            finally:
                if response is not None:response.close()
                connection.close()

    def discover(self,control):
        data=self._request('GET','/info/refs?service=git-upload-pack',None,'application/x-git-upload-pack-advertisement',control)
        packets=self._packets(data,control)
        if packets and packets[0].kind is PacketKind.DATA and packets[0].data.rstrip(b'\n')==b'# service=git-upload-pack':
            if len(packets)<2 or packets[1]!=FLUSH:raise ProtocolError('invalid smart HTTP service boundary')
            packets=packets[2:]
        elif not packets or packets[0]!=Packet(data=b'version 2\n'):
            raise ProtocolError('missing smart HTTP service announcement')
        ad=parse_advertisement(packets,limits=self._limits(control))
        if ad.shallow:raise UnsupportedFormat('shallow HTTP sources are unsupported')
        if ad.version==2:
            arguments=(b'peel',b'symrefs')+((b'unborn',) if b'unborn' in ad.features(b'ls-refs') else ())
            request=encode_v2_command(negotiate(ad,ad.format,command=b'ls-refs'),arguments,limits=self._limits(control))
            reply=self._request('POST','/git-upload-pack',request,'application/x-git-upload-pack-result',control,protocol_version=2)
            refs=parse_ls_refs(self._packets(reply,control),ad.format,limits=self._limits(control))
            ad=replace(ad,refs=refs)
        return ad

    def _sidebands(self,packets,control):
        if packets and packets[-1]==RESPONSE_END:packets=packets[:-1]
        if not packets or packets[-1]!=FLUSH:raise ProtocolError('missing pack stream terminator')
        data=bytearray()
        for packet in packets[:-1]:
            control.check();side=decode_sideband(packet,limits=self._limits(control));side.raise_if_error()
            if side.channel==1:
                data.extend(side.data)
                if len(data)>control.limits.pack.max_pack_bytes:raise TransferError('HTTP pack byte limit exceeded')
        if not data.startswith(b'PACK'):raise ProtocolError('missing HTTP pack data')
        return bytes(data)

    def fetch_pack(self,ad,wants,control):
        if ad.shallow:raise UnsupportedFormat('shallow HTTP fetch is unsupported')
        limits=self._limits(control)
        if ad.version==2:
            request=encode_v2_command(negotiate(ad,ad.format,command=b'fetch'),
                                      (*[b'want '+str(oid).encode() for oid in wants],b'ofs-delta',b'done'),limits=limits)
            body=self._request('POST','/git-upload-pack',request,'application/x-git-upload-pack-result',control,protocol_version=2)
            packets=self._packets(body,control)
            if not packets or packets[0]!=Packet(data=b'packfile\n'):
                if any(p.kind is PacketKind.DATA and p.data.rstrip(b'\n')==b'shallow-info' for p in packets):
                    raise UnsupportedFormat('shallow HTTP response is unsupported')
                raise ProtocolError('unsupported v2 fetch response section')
            return self._sidebands(packets[1:],control)
        available={c.name for c in ad.capabilities}
        caps=tuple(c for c in (b'ofs-delta',b'side-band-64k' if b'side-band-64k' in available else b'side-band') if c in available)
        request=encode_legacy_request(ad,ad.format,wants,capabilities=caps,limits=limits)
        body=self._request('POST','/git-upload-pack',request,'application/x-git-upload-pack-result',control,protocol_version=ad.version)
        if len(body)<4 or not re.fullmatch(rb'[0-9a-fA-F]{4}',body[:4]):raise ProtocolError('missing HTTP negotiation response')
        size=int(body[:4],16)
        if not 4<=size<=min(len(body),65520):raise ProtocolError('invalid HTTP negotiation response')
        first=self._packets(body[:size],control)
        if len(first)!=1 or parse_ack(first[0],ad.format)!=(None,None):raise ProtocolError('unexpected full-pack acknowledgment')
        if any(c.startswith(b'side-band') for c in caps):return self._sidebands(self._packets(body[size:],control),control)
        pack=body[size:]
        if not pack.startswith(b'PACK') or len(pack)>control.limits.pack.max_pack_bytes:raise ProtocolError('invalid HTTP raw pack boundary')
        return pack
