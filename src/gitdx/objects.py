"""Git object identities and codecs. Object payloads and path names are bytes."""

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import re
from typing import ClassVar, Iterable

from .errors import InvalidObject


class ObjectFormat(StrEnum):
    SHA1 = "sha1"
    SHA256 = "sha256"

    @property
    def digest_size(self) -> int:
        return 20 if self is ObjectFormat.SHA1 else 32

    @property
    def hex_size(self) -> int:
        return self.digest_size * 2


@dataclass(frozen=True, slots=True)
class ObjectId:
    hex: str
    format: ObjectFormat = ObjectFormat.SHA1

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", ObjectFormat(self.format))
        if not isinstance(self.hex, str) or not re.fullmatch(
            rf"[0-9a-f]{{{self.format.hex_size}}}", self.hex
        ):
            raise InvalidObject(f"expected a full lowercase {self.format.value} object ID")

    @classmethod
    def from_bytes(cls, value: bytes, format: ObjectFormat = ObjectFormat.SHA1) -> ObjectId:
        if not isinstance(value, bytes):
            raise TypeError("object ID bytes must be immutable bytes")
        return cls(value.hex(), format)

    @property
    def bytes(self) -> bytes:
        return bytes.fromhex(self.hex)

    def __str__(self) -> str:
        return self.hex


class ObjectType(StrEnum):
    BLOB = "blob"
    TREE = "tree"
    COMMIT = "commit"
    TAG = "tag"


def _bytes(value: bytes) -> None:
    if not isinstance(value, bytes):
        raise TypeError("Git payloads and names must be immutable bytes")


@dataclass(frozen=True, slots=True)
class RawObject:
    kind: ObjectType
    data: bytes

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", ObjectType(self.kind))
        except ValueError as error:
            raise InvalidObject("unsupported Git object type") from error
        _bytes(self.data)

    def encode(self) -> bytes:
        return f"{self.kind.value} {len(self.data)}".encode("ascii") + b"\0" + self.data

    def oid(self, format: ObjectFormat = ObjectFormat.SHA1) -> ObjectId:
        format = ObjectFormat(format)
        return ObjectId(hashlib.new(format.value, self.encode()).hexdigest(), format)


@dataclass(frozen=True, slots=True)
class Blob:
    data: bytes
    kind: ClassVar[ObjectType] = ObjectType.BLOB

    def __post_init__(self) -> None:
        _bytes(self.data)

    def to_raw(self) -> RawObject:
        return RawObject(self.kind, self.data)


TREE_MODES = frozenset((0o100644, 0o100755, 0o40000, 0o120000, 0o160000))
_MODE_BYTES = {f"{m:o}".encode("ascii") for m in TREE_MODES}


@dataclass(frozen=True, slots=True)
class TreeEntry:
    mode: int
    name: bytes
    oid: ObjectId

    def __post_init__(self) -> None:
        _bytes(self.name)
        if self.mode not in TREE_MODES:
            raise InvalidObject("unsupported tree entry mode")
        if not self.name or self.name in (b".", b"..") or b"\0" in self.name or b"/" in self.name:
            raise InvalidObject("tree entry names must be single nonempty path components")
        if not isinstance(self.oid, ObjectId):
            raise TypeError("tree entry requires an ObjectId")

    @property
    def sort_key(self) -> bytes:
        return self.name + (b"/" if self.mode == 0o40000 else b"")


@dataclass(frozen=True, slots=True)
class Tree:
    entries: tuple[TreeEntry, ...] = ()
    format: ObjectFormat = ObjectFormat.SHA1
    kind: ClassVar[ObjectType] = ObjectType.TREE

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", ObjectFormat(self.format))
        entries = tuple(self.entries)
        if any(e.oid.format != self.format for e in entries):
            raise InvalidObject("tree entries must use the tree's object format")
        if len({e.name for e in entries}) != len(entries):
            raise InvalidObject("duplicate tree entry name")
        object.__setattr__(self, "entries", tuple(sorted(entries, key=lambda e: e.sort_key)))

    def to_raw(self) -> RawObject:
        return RawObject(self.kind, b"".join(
            f"{e.mode:o} ".encode("ascii") + e.name + b"\0" + e.oid.bytes for e in self.entries
        ))

    @classmethod
    def from_bytes(cls, data: bytes, format: ObjectFormat = ObjectFormat.SHA1) -> Tree:
        _bytes(data)
        format = ObjectFormat(format)
        entries = []
        offset = 0
        while offset < len(data):
            space = data.find(b" ", offset, offset + 8)
            nul = data.find(b"\0", space + 1) if space >= 0 else -1
            end = nul + 1 + format.digest_size
            if space < 0 or nul < 0 or end > len(data):
                raise InvalidObject("truncated tree entry")
            mode = data[offset:space]
            if mode not in _MODE_BYTES:
                raise InvalidObject("invalid tree entry mode")
            entries.append(TreeEntry(int(mode, 8), data[space + 1:nul],
                                     ObjectId.from_bytes(data[nul + 1:end], format)))
            offset = end
        tree = cls(tuple(entries), format)
        if tree.to_raw().data != data:
            raise InvalidObject("tree entries are not in Git tree order")
        return tree


@dataclass(frozen=True, slots=True)
class Header:
    name: bytes
    value: bytes

    def __post_init__(self) -> None:
        _bytes(self.name)
        _bytes(self.value)
        if not re.fullmatch(rb"[!-~]+", self.name) or b"\0" in self.value:
            raise InvalidObject("invalid object header")
        if any(not line.startswith(b" ") for line in self.value.split(b"\n")[1:]):
            raise InvalidObject("header continuation lines must begin with a space")


def _parse_headers(data: bytes) -> tuple[tuple[Header, ...], bytes]:
    _bytes(data)
    block, separator, message = data.partition(b"\n\n")
    if not separator:
        raise InvalidObject("object headers require an empty line before the message")
    headers: list[Header] = []
    for line in block.split(b"\n"):
        if line.startswith(b" "):
            if not headers:
                raise InvalidObject("orphan header continuation")
            previous = headers.pop()
            headers.append(Header(previous.name, previous.value + b"\n" + line))
        else:
            name, space, value = line.partition(b" ")
            if not space:
                raise InvalidObject("invalid object header line")
            headers.append(Header(name, value))
    return tuple(headers), message


def _payload(headers: tuple[Header, ...], message: bytes) -> bytes:
    return b"\n".join(h.name + b" " + h.value for h in headers) + b"\n\n" + message


def _one(headers: tuple[Header, ...], name: bytes) -> bytes:
    values = [h.value for h in headers if h.name == name]
    if len(values) != 1 or not values[0]:
        raise InvalidObject(f"expected exactly one nonempty {name.decode('ascii')} header")
    return values[0]


def _header_oid(value: bytes, format: ObjectFormat) -> ObjectId:
    try:
        return ObjectId(value.decode("ascii"), format)
    except UnicodeError as error:
        raise InvalidObject("object header ID is not ASCII") from error


@dataclass(frozen=True, slots=True)
class Commit:
    headers: tuple[Header, ...]
    message: bytes
    format: ObjectFormat = ObjectFormat.SHA1
    kind: ClassVar[ObjectType] = ObjectType.COMMIT

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", ObjectFormat(self.format))
        object.__setattr__(self, "headers", tuple(self.headers))
        _bytes(self.message)
        if not self.headers or self.headers[0].name != b"tree":
            raise InvalidObject("commit must start with a tree header")
        _header_oid(_one(self.headers, b"tree"), self.format)
        for name in (b"author", b"committer"):
            if b"\n" in _one(self.headers, name):
                raise InvalidObject("identity headers cannot span multiple lines")
        for header in self.headers:
            if header.name == b"parent":
                _header_oid(header.value, self.format)

    @property
    def tree(self) -> ObjectId:
        return _header_oid(_one(self.headers, b"tree"), self.format)

    @property
    def parents(self) -> tuple[ObjectId, ...]:
        return tuple(_header_oid(h.value, self.format) for h in self.headers if h.name == b"parent")

    def to_raw(self) -> RawObject:
        return RawObject(self.kind, _payload(self.headers, self.message))

    @classmethod
    def from_bytes(cls, data: bytes, format: ObjectFormat = ObjectFormat.SHA1) -> Commit:
        return cls(*_parse_headers(data), format)

    @classmethod
    def create(cls, tree: ObjectId, *, author: bytes, committer: bytes, message: bytes,
               parents: Iterable[ObjectId] = (), extra_headers: Iterable[Header] = ()) -> Commit:
        parents = tuple(parents)
        if any(p.format != tree.format for p in parents):
            raise InvalidObject("parent object format mismatch")
        headers = (Header(b"tree", tree.hex.encode("ascii")),)
        headers += tuple(Header(b"parent", p.hex.encode("ascii")) for p in parents)
        headers += (Header(b"author", author), Header(b"committer", committer))
        return cls(headers + tuple(extra_headers), message, tree.format)


@dataclass(frozen=True, slots=True)
class Tag:
    headers: tuple[Header, ...]
    message: bytes
    format: ObjectFormat = ObjectFormat.SHA1
    kind: ClassVar[ObjectType] = ObjectType.TAG

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", ObjectFormat(self.format))
        object.__setattr__(self, "headers", tuple(self.headers))
        _bytes(self.message)
        if tuple(h.name for h in self.headers[:3]) != (b"object", b"type", b"tag"):
            raise InvalidObject("tag must start with object, type and tag headers")
        _header_oid(_one(self.headers, b"object"), self.format)
        try:
            ObjectType(_one(self.headers, b"type").decode("ascii"))
        except (ValueError, UnicodeError) as error:
            raise InvalidObject("invalid tag target type") from error
        if b"\n" in _one(self.headers, b"tag"):
            raise InvalidObject("tag name must be one line")
        if any(h.name == b"tagger" for h in self.headers):
            if b"\n" in _one(self.headers, b"tagger"):
                raise InvalidObject("tagger must be one line")

    @property
    def target(self) -> ObjectId:
        return _header_oid(_one(self.headers, b"object"), self.format)

    def to_raw(self) -> RawObject:
        return RawObject(self.kind, _payload(self.headers, self.message))

    @classmethod
    def from_bytes(cls, data: bytes, format: ObjectFormat = ObjectFormat.SHA1) -> Tag:
        return cls(*_parse_headers(data), format)

    @classmethod
    def create(cls, target: ObjectId, *, target_type: ObjectType, name: bytes,
               message: bytes, tagger: bytes | None = None) -> Tag:
        headers = (Header(b"object", target.hex.encode("ascii")),
                   Header(b"type", ObjectType(target_type).value.encode("ascii")), Header(b"tag", name))
        if tagger is not None:
            headers += (Header(b"tagger", tagger),)
        return cls(headers, message, target.format)


def parse_object(raw: RawObject, format: ObjectFormat = ObjectFormat.SHA1) -> Blob | Tree | Commit | Tag:
    if raw.kind is ObjectType.BLOB:
        return Blob(raw.data)
    return {ObjectType.TREE: Tree, ObjectType.COMMIT: Commit, ObjectType.TAG: Tag}[raw.kind].from_bytes(raw.data, format)
