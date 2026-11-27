"""Application-selected trust profiles and bounded incoming-object validation."""

from dataclasses import dataclass, fields
from enum import StrEnum

from .errors import InvalidObject, UnsupportedFormat
from .history import validate_identity
from .object_store import ObjectLimitExceeded
from .objects import Commit, ObjectFormat, ObjectType, Tag, Tree, parse_object


class IngestionProfile(StrEnum):
    TRUSTED_LOCAL='trusted-local'
    UNTRUSTED='untrusted'


@dataclass(frozen=True, slots=True)
class IngestionLimits:
    max_headers: int = 2048
    max_header_bytes: int = 256 * 1024
    max_tree_entries: int = 100_000
    max_ref_components: int = 32
    max_ref_component_bytes: int = 255

    def __post_init__(self):
        if any(type(getattr(self,f.name)) is not int or getattr(self,f.name)<=0 for f in fields(self)):
            raise ValueError('ingestion bounds must be positive integers')


def check_ingestion_format(profile,format):
    """Run before filesystem lookup, quarantine, inflation or policy callbacks."""
    profile=IngestionProfile(profile)
    if profile is IngestionProfile.UNTRUSTED and ObjectFormat(format) is not ObjectFormat.SHA256:
        raise UnsupportedFormat('untrusted ingestion requires native SHA-256; SHA-1 is trusted-local only')
    return profile


class ObjectValidator:
    """Bound parser allocations before decoding, then enforce the selected fsck subset.

    The ordinary byte codecs preserve historical bytes. Untrusted intake adds
    canonical identities, bounded headers/entries and unsafe tree-name refusal.
    This is a documented subset, not every configurable native fsck diagnostic.
    """

    def __init__(self,format,*,limits=None,check=None):
        self.format=ObjectFormat(format);self.limits=limits or IngestionLimits()
        self.check=check if check is not None else (lambda:None)

    def __call__(self,raw):
        self.check();limits=self.limits
        if raw.kind in (ObjectType.COMMIT,ObjectType.TAG):
            end=raw.data.find(b'\n\n',0,limits.max_header_bytes+2)
            if end<0:
                if len(raw.data)>limits.max_header_bytes:raise ObjectLimitExceeded('incoming object header byte limit exceeded')
                raise InvalidObject('incoming object headers require a message separator')
            if end>limits.max_header_bytes:raise ObjectLimitExceeded('incoming object header byte limit exceeded')
            cursor=count=0
            while cursor<end:
                self.check();newline=raw.data.find(b'\n',cursor,end)
                count+=1
                if count>limits.max_headers:raise ObjectLimitExceeded('incoming object header count exceeded')
                if newline<0:break
                cursor=newline+1
        elif raw.kind is ObjectType.TREE:
            cursor=count=0
            while cursor<len(raw.data):
                self.check();count+=1
                if count>limits.max_tree_entries:raise ObjectLimitExceeded('incoming tree entry count exceeded')
                nul=raw.data.find(b'\0',cursor)
                if nul<0 or nul+self.format.digest_size+1>len(raw.data):raise InvalidObject('truncated incoming tree entry')
                cursor=nul+1+self.format.digest_size
        value=parse_object(raw,self.format)
        if isinstance(value,Commit):
            for name in (b'author',b'committer'):
                validate_identity(next(h.value for h in value.headers if h.name==name))
        elif isinstance(value,Tag):
            if not next(h.value for h in value.headers if h.name==b'tag'):
                raise InvalidObject('incoming annotated tag must have a name')
            for header in value.headers:
                if header.name==b'tagger':validate_identity(header.value)
        elif isinstance(value,Tree):
            for entry in value.entries:
                self.check()
                if entry.name in (b'.',b'..') or entry.name.lower()==b'.git':
                    raise InvalidObject('unsafe incoming tree path component')
                if entry.name.lower()==b'.gitmodules' and entry.mode==0o120000:
                    raise InvalidObject('incoming .gitmodules cannot be a symlink')
        self.check()


def check_ingestion_names(commands,limits):
    for command in commands:
        parts=command.name.split(b'/')
        if len(parts)>limits.max_ref_components or any(len(p)>limits.max_ref_component_bytes for p in parts):
            raise ObjectLimitExceeded('incoming ref component/depth limit exceeded')
