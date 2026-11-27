"""Bounded unindexed pack intake and private, graph-validated object staging."""

from collections import defaultdict, deque
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import struct
import tempfile
import uuid
import zlib

from .delta import DeltaBudget, _apply
from .errors import GitdxError, UnsupportedFormat
from .filesystem import (check_path, read_file, storage_mkdtemp, storage_remove_tree,
                         storage_stat, storage_write_exclusive)
from .confined_fs import active
from .object_store import LooseObjectStore, MissingObject, ObjectLimitExceeded, StoreLimits
from .objects import ObjectFormat, RawObject, parse_object
from .pack import InvalidPack, PackLimits
from .pack_writer import PackWriteLimits, reachable_objects


@dataclass(frozen=True, slots=True)
class ReceivedObject:
    offset: int
    raw: RawObject
    depth: int = 0


def unpack_objects(data, format, *, limits=None, check=None, validator=None):
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
    cursor = 12;records = {};ready = deque();waiting = defaultdict(list);budget = DeltaBudget(limits,check=check)
    for _ in range(count):
        check();start = cursor
        if cursor>=end:raise InvalidPack('truncated incoming object')
        byte=data[cursor];cursor+=1;kind=(byte>>4)&7;size=byte&15;shift=4;header=1
        while byte&128:
            if cursor>=end or header>=10:raise InvalidPack('truncated/excessive incoming object header')
            byte=data[cursor];cursor+=1;header+=1;size|=(byte&127)<<shift;shift+=7
        if kind not in (1,2,3,4,6,7):raise InvalidPack('invalid incoming object type')
        if size>limits.max_object_bytes:raise ObjectLimitExceeded('incoming object size exceeded')
        budget.consume(size)  # Reserve declared inflation before allocating output.
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
        records[start]=(kind,bytes(payload),base)
        if kind not in (6,7):ready.append(ReceivedObject(start,RawObject({1:'commit',2:'tree',3:'blob',4:'tag'}[kind],bytes(payload))))
        else:waiting[base].append(start)
    if cursor!=end:raise InvalidPack('incoming object count or trailing bytes mismatch')
    result={};offsets={}
    while ready:
        check();resolved=ready.popleft();raw=resolved.raw;oid=raw.oid(format)
        if oid in result:raise InvalidPack('duplicate incoming object identity')
        if validator is not None:validator(raw)
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

    def __init__(self, repo, *, limits=None, check=None, validator=None):
        self.repo=repo;self.format=repo.object_format;self.limits=limits or PackLimits()
        self.check=check or (lambda:None);self._path=None;self._root=repo.objects.path
        self.objects=None;self._received_objects=();self._validated=None;self._received=False
        self._closed=False;self._identity=None;self._root_identity=None;self._receipt=uuid.uuid4().bytes
        self.cleanup_error=None;self.cleanup_pending=False;self.promoted=0;self.staged_bytes=0;self._cleaned=False
        self.validator=validator

    @property
    def path(self):return self._path

    @property
    def received(self):return self._received_objects

    @property
    def validated(self):return self._validated

    def _owned(self):
        if self.path is None or self.repo.objects.path!=self._root or self.objects.path!=self.path:
            raise QuarantineOwnershipError('quarantine storage identity changed')
        check_path(self._root,self.path)
        root_info=storage_stat(self._root)
        if (root_info.st_dev,root_info.st_ino)!=self._root_identity:
            raise QuarantineOwnershipError('quarantine root identity changed')
        info=storage_stat(self.path)
        if (info.st_dev,info.st_ino)!=self._identity or read_file(self.path,self.path/'owner',32)!=self._receipt:
            raise QuarantineOwnershipError('quarantine ownership receipt changed')

    def _active(self):
        if self._closed:raise ValueError('quarantine is closed')
        self._owned();self.check()

    def __enter__(self):
        if self.path is not None:raise ValueError('quarantine cannot be reused')
        if self._closed:raise ValueError('quarantine is closed')
        root=self._root;check_path(root,root)
        info=storage_stat(root);self._root_identity=(info.st_dev,info.st_ino)
        self._path=storage_mkdtemp(root,'incoming-')
        try:
            scope=active()
            with scope.cleanup() if scope is not None else nullcontext():
                info=storage_stat(self.path);self._identity=(info.st_dev,info.st_ino)
                self.objects=LooseObjectStore(self.path,self.format,limits=StoreLimits(
                    max_object_bytes=self.limits.max_object_bytes,max_compressed_bytes=self.limits.max_compressed_object_bytes))
            storage_write_exclusive(self.path/'owner',self._receipt)
            self.staged_bytes=len(self._receipt)
        except BaseException:
            self.cleanup_pending=True;self._closed=True
            if active() is not None and self._identity is not None:
                try:
                    storage_remove_tree(self.path,identity=self._identity)
                    self._cleaned=True;self.cleanup_pending=False
                except (OSError,GitdxError) as error:self.cleanup_error=type(error).__name__
            raise
        return self

    def __exit__(self,*args):
        self.cleanup()

    def cleanup(self):
        """Remove only this instance's owned directory; report retained storage."""
        self._closed=True
        if self.path is None or self._cleaned:return True
        try:
            scope=active()
            with scope.cleanup() if scope is not None else nullcontext():
                self._owned();storage_remove_tree(self.path,identity=self._identity)
        except (OSError,GitdxError) as error:
            self.cleanup_error=type(error).__name__;self.cleanup_pending=True
            return False
        self.cleanup_error=None;self.cleanup_pending=False;self._cleaned=True
        return True

    def read(self,oid):
        self._active()
        try:raw=self.objects.read(oid)
        except MissingObject:raw=self.repo.objects.read(oid)
        if self.validator is not None:self.validator(raw)
        return raw

    def receive(self,data,*,max_storage_bytes=None):
        self._active()
        if max_storage_bytes is not None and (type(max_storage_bytes) is not int or max_storage_bytes<=0):
            raise ValueError('select a positive quarantine storage byte limit')
        if self._received:raise ValueError('quarantine already received a pack')
        self._received=True
        self._validated=None
        self._received_objects=unpack_objects(data,self.format,limits=self.limits,check=self.check,validator=self.validator)
        reserve=None
        if active() is not None and max_storage_bytes is not None:
            allocated=8192;prefixes=set()
            def reserve(size):
                nonlocal allocated
                cost=((size+4095)//4096)*4096
                if oid.hex[:2] not in prefixes:cost+=4096
                if allocated+cost>max_storage_bytes:raise ObjectLimitExceeded('quarantine allocation reservation exceeded')
                allocated+=cost;prefixes.add(oid.hex[:2])
            if allocated>max_storage_bytes:raise ObjectLimitExceeded('quarantine receipt allocation exceeds budget')
        for oid,raw in self.received:
            self._active()
            if reserve is None:self.objects.write(raw)
            else:self.objects.write(raw,reserve=reserve)
            self.staged_bytes+=storage_stat(self.path/oid.hex[:2]/oid.hex[2:]).st_size
            if max_storage_bytes is not None and self.staged_bytes>max_storage_bytes:
                raise ObjectLimitExceeded('quarantine storage byte limit exceeded')

    def validate(self,roots,*,limits=None):
        self._validated=None
        self._active()
        if not self._received:raise ValueError('receive a pack before validating quarantine')
        limits=limits or PackWriteLimits(max_objects=self.limits.max_objects,max_object_bytes=self.limits.max_object_bytes,
                                        max_bytes=self.limits.max_delta_bytes)
        outer=self
        class CheckedStore:
            format=outer.format
            def read(self,oid):outer.check();return outer.read(oid)
        self._validated=reachable_objects(CheckedStore(),roots,limits=limits)
        self.check();return self.validated

    def promote(self):
        self._active()
        if self.validated is None:raise ValueError('validate quarantine roots before publication')
        for oid,raw in self.validated:
            self._active();self.repo.objects.write(raw);self.promoted+=1
        self.check()


class QuarantineOwnershipError(GitdxError):
    """A private staging directory no longer belongs to this intake instance."""
