"""Bounded unindexed pack intake and private, graph-validated object staging."""

from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import struct
import tempfile
import zlib

from .delta import DeltaBudget, _apply
from .errors import UnsupportedFormat
from .filesystem import check_path
from .object_store import LooseObjectStore, MissingObject, ObjectLimitExceeded, StoreLimits
from .objects import ObjectFormat, RawObject, parse_object
from .pack import InvalidPack, PackLimits
from .pack_writer import PackWriteLimits, reachable_objects


@dataclass(frozen=True, slots=True)
class ReceivedObject:
    offset: int
    raw: RawObject
    depth: int = 0


def unpack_objects(data, format, *, limits=None, check=None):
    """Validate a complete non-thin pack, including forward REF/OFS deltas.

    No index, filesystem publication, subprocess or external delta base is used.
    Returned objects have verified derived identities; graph validation is a
    separate mandatory step before selecting ref targets.
    """
    limits = limits or PackLimits();format = ObjectFormat(format);check = check or (lambda: None)
    if not isinstance(data,bytes):raise TypeError('pack bytes must be immutable')
    width = format.digest_size;end = len(data)-width
    if len(data)>limits.max_pack_bytes:raise ObjectLimitExceeded('incoming pack byte limit exceeded')
    if len(data)<12+width or data[:4]!=b'PACK':raise InvalidPack('invalid incoming pack header')
    version,count = struct.unpack_from('>II',data,4)
    if version not in (2,3):raise UnsupportedFormat('unsupported incoming pack version')
    if count>limits.max_objects:raise ObjectLimitExceeded('incoming pack object count exceeded')
    check()
    if hashlib.new(format.value,memoryview(data)[:end]).digest()!=data[end:]:raise InvalidPack('incoming pack checksum mismatch')
    cursor = 12;records = {};ready = deque();waiting = defaultdict(list);budget = DeltaBudget(limits)
    for _ in range(count):
        check();start = cursor
        if cursor>=end:raise InvalidPack('truncated incoming object')
        byte=data[cursor];cursor+=1;kind=(byte>>4)&7;size=byte&15;shift=4;header=1
        while byte&128:
            if cursor>=end or header>=10:raise InvalidPack('truncated/excessive incoming object header')
            byte=data[cursor];cursor+=1;header+=1;size|=(byte&127)<<shift;shift+=7
        if kind not in (1,2,3,4,6,7):raise InvalidPack('invalid incoming object type')
        if size>limits.max_object_bytes:raise ObjectLimitExceeded('incoming object size exceeded')
        base=None
        if kind==6:
            if cursor>=end:raise InvalidPack('truncated incoming offset base')
            byte=data[cursor];cursor+=1;distance=byte&127;n=1
            while byte&128:
                if cursor>=end or n>=10:raise InvalidPack('invalid incoming offset base')
                byte=data[cursor];cursor+=1;n+=1;distance=((distance+1)<<7)|(byte&127)
            base=start-distance
            if not distance or base not in records:raise InvalidPack('incoming offset base is not an earlier object')
        elif kind==7:
            if cursor+width>end:raise InvalidPack('truncated incoming ref base')
            base=data[cursor:cursor+width];cursor+=width
            if not any(base):raise InvalidPack('null incoming ref base')
        compressed=cursor;decoder=zlib.decompressobj();payload=bytearray()
        while not decoder.eof:
            check()
            if cursor>=end:raise InvalidPack('truncated incoming compression')
            chunk=memoryview(data)[cursor:min(end,cursor+65536)]
            try:piece=decoder.decompress(chunk,size-len(payload)+1)
            except zlib.error as error:raise InvalidPack('invalid incoming compression') from error
            payload.extend(piece)
            if len(payload)>size or decoder.unconsumed_tail:raise InvalidPack('incoming inflation exceeds declared size')
            cursor+=len(chunk)-len(decoder.unused_data)
            if cursor-compressed>limits.max_compressed_object_bytes:raise ObjectLimitExceeded('incoming compressed object exceeded')
        if len(payload)!=size:raise InvalidPack('incoming inflated size mismatch')
        budget.consume(size);records[start]=(kind,bytes(payload),base)
        if kind not in (6,7):ready.append(ReceivedObject(start,RawObject({1:'commit',2:'tree',3:'blob',4:'tag'}[kind],bytes(payload))))
        else:waiting[base].append(start)
    if cursor!=end:raise InvalidPack('incoming object count or trailing bytes mismatch')
    result={};offsets={}
    while ready:
        check();resolved=ready.popleft();raw=resolved.raw;oid=raw.oid(format)
        if oid in result:raise InvalidPack('duplicate incoming object identity')
        parse_object(raw,format);result[oid]=raw;offsets[resolved.offset]=resolved
        for key in (resolved.offset,oid.bytes):
            for offset in waiting.pop(key,()):
                _,instructions,_=records[offset];depth=resolved.depth+1
                if depth>limits.max_delta_depth:raise ObjectLimitExceeded('incoming delta depth exceeded')
                ready.append(ReceivedObject(offset,RawObject(raw.kind,_apply(raw.data,instructions,budget)),depth))
    if len(result)!=count:raise InvalidPack('incoming thin, missing or cyclic delta base')
    return tuple(sorted(result.items(),key=lambda item:item[0].bytes))


class ObjectQuarantine:
    """Private incoming objects; validate first, then publish immutable objects."""

    def __init__(self, repo, *, limits=None, check=None):
        self.repo=repo;self.format=repo.object_format;self.limits=limits or PackLimits()
        self.check=check or (lambda:None);self.path=None;self.objects=None;self.received=();self.validated=None;self._received=False

    def __enter__(self):
        if self.path is not None:raise ValueError('quarantine cannot be reused')
        root=self.repo.objects.path;check_path(root,root)
        self.path=Path(tempfile.mkdtemp(prefix='incoming-',dir=root))
        self.objects=LooseObjectStore(self.path,self.format,limits=StoreLimits(
            max_object_bytes=self.limits.max_object_bytes,max_compressed_bytes=self.limits.max_compressed_object_bytes))
        return self

    def __exit__(self,*args):
        if self.path is not None:
            try:
                check_path(self.repo.objects.path,self.path);shutil.rmtree(self.path)
            except OSError:pass  # Interrupted cleanup leaves only an unreferenced private directory.

    def read(self,oid):
        try:return self.objects.read(oid)
        except MissingObject:return self.repo.objects.read(oid)

    def receive(self,data):
        if self._received:raise ValueError('quarantine already received a pack')
        self._received=True
        self.validated=None
        self.received=unpack_objects(data,self.format,limits=self.limits,check=self.check)
        for oid,raw in self.received:
            self.check();self.objects.write(raw)

    def validate(self,roots):
        self.validated=None
        self.check()
        limits=PackWriteLimits(max_objects=self.limits.max_objects,max_object_bytes=self.limits.max_object_bytes,
                              max_bytes=self.limits.max_delta_bytes)
        outer=self
        class CheckedStore:
            format=outer.format
            def read(self,oid):outer.check();return outer.read(oid)
        self.validated=reachable_objects(CheckedStore(),roots,limits=limits)
        self.check();return self.validated

    def promote(self):
        if self.validated is None:raise ValueError('validate quarantine roots before publication')
        for oid,raw in self.validated:
            self.check();self.repo.objects.write(raw)
        self.check()
