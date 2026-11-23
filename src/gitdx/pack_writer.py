"""Complete, bounded object closures and deterministic non-delta pack bundles."""

from dataclasses import dataclass, fields
import hashlib
import struct
import zlib

from .errors import InvalidObject
from .object_store import ObjectLimitExceeded
from .objects import Commit, ObjectFormat, ObjectId, ObjectType, Tag, Tree


@dataclass(frozen=True, slots=True)
class PackWriteLimits:
    max_objects: int = 100_000
    max_edges: int = 1_000_000
    max_object_bytes: int = 64 * 1024 * 1024
    max_bytes: int = 256 * 1024 * 1024
    max_pack_bytes: int = 256 * 1024 * 1024
    max_index_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0 for f in fields(self)):
            raise ValueError("pack writing limits must be positive integers")
        if self.max_objects > 0xffffffff or self.max_pack_bytes >= 0x80000000:
            raise ValueError("pack writer supports bounded 32-bit offset indexes")


@dataclass(frozen=True, slots=True)
class PackBundle:
    pack: bytes
    index: bytes
    format: ObjectFormat
    object_ids: tuple[ObjectId, ...]
    checksum: bytes


def reachable_objects(store, roots, *, limits=None):
    """Validate every selected dependency before returning any complete closure."""
    limits = limits or PackWriteLimits();pending = [];objects = {};total = edges = 0
    for oid in roots:
        if len(pending) >= limits.max_objects:raise ObjectLimitExceeded("too many pack roots")
        pending.append((oid, None))
    while pending:
        oid, expected = pending.pop()
        if not isinstance(oid, ObjectId) or oid.format != store.format:raise InvalidObject("pack root/dependency format mismatch")
        raw = objects.get(oid)
        if raw is not None:
            if expected is not None and raw.kind is not expected:raise InvalidObject("reachable dependency type mismatch")
            continue
        if len(objects) >= limits.max_objects:raise ObjectLimitExceeded("pack object count exceeded")
        raw = store.read(oid)
        if raw.oid(store.format) != oid:raise InvalidObject("reachable object hash mismatch")
        if expected is not None and raw.kind is not expected:raise InvalidObject("reachable dependency type mismatch")
        total += len(raw.data)
        if len(raw.data) > limits.max_object_bytes or total > limits.max_bytes:raise ObjectLimitExceeded("reachable object bytes exceeded")
        dependencies = []
        if raw.kind is ObjectType.COMMIT:
            commit = Commit.from_bytes(raw.data, store.format)
            dependencies = [(commit.tree, ObjectType.TREE), *((p, ObjectType.COMMIT) for p in commit.parents)]
        elif raw.kind is ObjectType.TREE:
            tree = Tree.from_bytes(raw.data, store.format)
            # Gitlinks name another repository's commits, not local dependencies.
            dependencies = [(e.oid, ObjectType.TREE if e.mode == 0o40000 else ObjectType.BLOB) for e in tree.entries if e.mode != 0o160000]
        elif raw.kind is ObjectType.TAG:
            tag = Tag.from_bytes(raw.data, store.format)
            kind = ObjectType(next(h.value for h in tag.headers if h.name == b"type").decode("ascii"))
            dependencies = [(tag.target, kind)]
        edges += len(dependencies)
        if edges > limits.max_edges:raise ObjectLimitExceeded("reachable edge limit exceeded")
        objects[oid] = raw;pending.extend(dependencies)
    return tuple(sorted(objects.items(), key=lambda pair: pair[0].bytes))


def _header(kind, size):
    byte = (kind << 4) | (size & 15);size >>= 4;result = bytearray()
    while size:
        result.append(byte | 128);byte = size & 127;size >>= 7
    result.append(byte)
    return result


def write_pack(store, roots, *, limits=None):
    limits = limits or PackWriteLimits();format = ObjectFormat(store.format)
    objects = reachable_objects(store, roots, limits=limits)
    count = len(objects);width = format.digest_size
    if 8+1024+count*(width+8)+2*width > limits.max_index_bytes:raise ObjectLimitExceeded("pack index byte limit exceeded")
    pack = bytearray(b"PACK"+struct.pack(">II", 2, count));offsets = [];crcs = [];fanout = [0]*256
    for oid, raw in objects:
        encoded = _header({ObjectType.COMMIT:1, ObjectType.TREE:2, ObjectType.BLOB:3, ObjectType.TAG:4}[raw.kind], len(raw.data))
        encoded.extend(zlib.compress(raw.data))
        if len(pack)+len(encoded)+width > limits.max_pack_bytes:raise ObjectLimitExceeded("pack output byte limit exceeded")
        offsets.append(len(pack));crcs.append(zlib.crc32(encoded));fanout[oid.bytes[0]] += 1;pack.extend(encoded)
    if len(pack)+width > limits.max_pack_bytes:raise ObjectLimitExceeded("pack output byte limit exceeded")
    checksum = hashlib.new(format.value, pack).digest();pack.extend(checksum)
    for i in range(1, 256):fanout[i] += fanout[i-1]
    index = bytearray(b"\xfftOc"+struct.pack(">I256I", 2, *fanout))
    index.extend(b"".join(oid.bytes for oid, _ in objects))
    for table in (crcs, offsets):
        for value in table:index.extend(struct.pack(">I", value))
    index.extend(checksum);index.extend(hashlib.new(format.value, index).digest())
    return PackBundle(bytes(pack), bytes(index), format, tuple(oid for oid, _ in objects), checksum)
