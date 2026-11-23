"""Bounded pack/index parsing and verified undeltified-object access."""

from bisect import bisect_left
from dataclasses import dataclass, fields
import hashlib
from pathlib import Path
import re
import struct
import zlib

from .errors import InvalidObject, UnsupportedFormat
from .filesystem import read_file
from .object_store import LooseObjectStore, MissingObject, ObjectLimitExceeded
from .objects import ObjectFormat, ObjectId, RawObject


class InvalidPack(InvalidObject):
    """Pack/index structure, checksums or object contents are inconsistent."""


@dataclass(frozen=True, slots=True)
class PackLimits:
    max_pack_bytes: int = 256 * 1024 * 1024
    max_index_bytes: int = 64 * 1024 * 1024
    max_objects: int = 250_000
    max_object_bytes: int = 64 * 1024 * 1024
    max_compressed_object_bytes: int = 64 * 1024 * 1024
    max_packs: int = 128
    max_scan_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0 for f in fields(self)):
            raise ValueError("pack limits must be positive integers")


@dataclass(frozen=True, slots=True)
class PackIndexEntry:
    oid: ObjectId
    offset: int
    crc32: int | None


@dataclass(frozen=True, slots=True)
class PackIndex:
    entries: tuple[PackIndexEntry, ...]
    format: ObjectFormat
    pack_checksum: bytes
    version: int

    def __post_init__(self):
        object.__setattr__(self, "format", ObjectFormat(self.format))
        object.__setattr__(self, "entries", tuple(self.entries))
        if self.version not in (1, 2):raise UnsupportedFormat("unsupported pack index version")
        if not isinstance(self.pack_checksum, bytes) or len(self.pack_checksum) != self.format.digest_size:raise InvalidPack("invalid pack-index trailer width")
        if any(e.oid.format != self.format or type(e.offset) is not int or e.offset < 12 for e in self.entries):raise InvalidPack("invalid pack-index entry format/offset")
        if any(a.oid.bytes >= b.oid.bytes for a, b in zip(self.entries, self.entries[1:])):raise InvalidPack("pack-index IDs must be sorted and unique")
        if len({e.offset for e in self.entries}) != len(self.entries):raise InvalidPack("duplicate packed object offset")
        if any((e.crc32 is not None if self.version == 1 else type(e.crc32) is not int or not 0 <= e.crc32 <= 0xffffffff) for e in self.entries):raise InvalidPack("invalid pack-index CRC table")

    def lookup(self, oid: ObjectId) -> PackIndexEntry | None:
        if oid.format != self.format:raise InvalidPack("pack index object format mismatch")
        position = bisect_left(self.entries, oid.bytes, key=lambda entry: entry.oid.bytes)
        if position < len(self.entries) and self.entries[position].oid == oid:return self.entries[position]
        return None

    @classmethod
    def from_bytes(cls, data: bytes, format=ObjectFormat.SHA1, *, limits: PackLimits | None = None) -> PackIndex:
        format, limits = ObjectFormat(format), limits or PackLimits()
        width = format.digest_size
        if len(data) > limits.max_index_bytes:raise ObjectLimitExceeded("pack index exceeds its byte limit")
        if len(data) < 1024 + width * 2:raise InvalidPack("truncated pack index")
        version, start = 1, 0
        if data[:4] == b"\xfftOc":
            version = struct.unpack_from(">I", data, 4)[0]
            if version != 2:raise UnsupportedFormat("only pack index versions 1 and 2 are supported")
            start = 8
        if len(data) < start + 1024 + width * 2:raise InvalidPack("truncated pack-index fanout")
        fanout = struct.unpack_from(">256I", data, start)
        if any(a > b for a, b in zip(fanout, fanout[1:])):raise InvalidPack("pack-index fanout is not monotone")
        count = fanout[-1]
        if count > limits.max_objects:raise ObjectLimitExceeded("pack object count exceeds its limit")
        if hashlib.new(format.value, memoryview(data)[:-width]).digest() != data[-width:]:raise InvalidPack("pack-index checksum mismatch")
        start += 1024
        end = len(data) - width * 2
        entries = []
        if version == 1:
            if end - start != count * (4 + width):raise InvalidPack("invalid v1 pack-index table length")
            for i in range(count):
                offset = start + i * (4 + width)
                entries.append(PackIndexEntry(ObjectId.from_bytes(data[offset + 4:offset + 4 + width], format), struct.unpack_from(">I", data, offset)[0], None))
        else:
            base_end = start + count * (width + 8)
            if base_end > end or (end - base_end) % 8:raise InvalidPack("invalid v2 pack-index table lengths")
            large_count = (end - base_end) // 8
            if large_count > count:raise InvalidPack("excessive pack-index large-offset entries")
            used_large = set()
            for i in range(count):
                oid = ObjectId.from_bytes(data[start + i * width:start + (i + 1) * width], format)
                crc = struct.unpack_from(">I", data, start + count * width + i * 4)[0]
                offset = struct.unpack_from(">I", data, start + count * (width + 4) + i * 4)[0]
                if offset & 0x80000000:
                    number = offset & 0x7fffffff
                    if number >= large_count or number in used_large:raise InvalidPack("invalid large-offset table reference")
                    used_large.add(number)
                    offset = struct.unpack_from(">Q", data, base_end + number * 8)[0]
                entries.append(PackIndexEntry(oid, offset, crc))
            if used_large != set(range(large_count)):raise InvalidPack("unreferenced pack-index large offsets")
        names = [entry.oid.bytes for entry in entries]
        if any(a >= b for a, b in zip(names, names[1:])):raise InvalidPack("pack-index IDs must be sorted and unique")
        offsets = [e.offset for e in entries]
        if len(set(offsets)) != count or any(o < 12 for o in offsets):raise InvalidPack("invalid pack offsets")
        if any(o >= limits.max_pack_bytes for o in offsets):raise ObjectLimitExceeded("pack offsets exceed the configured file budget")
        histogram = [0] * 256
        for name in names:histogram[name[0]] += 1
        total = 0
        for i, amount in enumerate(histogram):
            total += amount
            if fanout[i] != total:raise InvalidPack("pack-index fanout does not match its IDs")
        return cls(tuple(entries), format, data[-2 * width:-width], version)


@dataclass(frozen=True, slots=True)
class PackedEntry:
    kind: int
    size: int
    compressed_start: int
    end: int


class PackFile:
    def __init__(self, data: bytes, index: PackIndex, *, limits: PackLimits | None = None):
        if not isinstance(data, bytes):raise TypeError("pack data must be immutable bytes")
        self.data, self.index, self.limits = data, index, limits or PackLimits()
        width = index.format.digest_size
        if len(data) > self.limits.max_pack_bytes:raise ObjectLimitExceeded("pack exceeds its byte limit")
        if len(data) < 12 + width or data[:4] != b"PACK":raise InvalidPack("invalid pack signature or length")
        version, count = struct.unpack_from(">II", data, 4)
        if version not in (2, 3):raise UnsupportedFormat("only pack versions 2 and 3 are supported")
        if count != len(index.entries):raise InvalidPack("pack and index object counts differ")
        if count > self.limits.max_objects:raise ObjectLimitExceeded("pack object count exceeds its limit")
        checksum = hashlib.new(index.format.value, memoryview(data)[:-width]).digest()
        if data[-width:] != checksum or index.pack_checksum != checksum:raise InvalidPack("pack checksum or index pairing mismatch")
        physical = sorted(index.entries, key=lambda e: e.offset)
        if (physical and physical[0].offset != 12) or (not physical and len(data) != 12 + width):raise InvalidPack("unexpected bytes before pack objects")
        offsets = {e.offset for e in physical}
        self._entries = {}
        for i, entry in enumerate(physical):
            start = entry.offset
            end = physical[i + 1].offset if i + 1 < count else len(data) - width
            if not 12 <= start < end <= len(data) - width:raise InvalidPack("pack offset outside object region")
            if entry.crc32 is not None and zlib.crc32(memoryview(data)[start:end]) != entry.crc32:raise InvalidPack("packed object CRC32 mismatch")
            cursor, byte = start + 1, data[start]
            kind, size, shift, header_bytes = (byte >> 4) & 7, byte & 15, 4, 1
            while byte & 0x80:
                if cursor >= end or header_bytes >= 10:raise InvalidPack("truncated or excessive packed object header")
                byte = data[cursor];cursor += 1;header_bytes += 1
                size |= (byte & 0x7f) << shift;shift += 7
            if kind not in (1, 2, 3, 4, 6, 7):raise InvalidPack("invalid packed object type")
            if size > self.limits.max_object_bytes:raise ObjectLimitExceeded("packed object size exceeds its limit")
            if kind == 7:
                if cursor + width > end:raise InvalidPack("truncated REF_DELTA base")
                if not any(data[cursor:cursor + width]):raise InvalidPack("null REF_DELTA base")
                cursor += width
            elif kind == 6:
                if cursor >= end:raise InvalidPack("truncated OFS_DELTA base")
                byte = data[cursor];cursor += 1
                distance, size_bytes = byte & 0x7f, 1
                while byte & 0x80:
                    if cursor >= end or size_bytes >= 10:raise InvalidPack("truncated or excessive OFS_DELTA base")
                    byte = data[cursor];cursor += 1;size_bytes += 1
                    distance = ((distance + 1) << 7) | (byte & 0x7f)
                if distance == 0 or start - distance not in offsets or start - distance >= start:raise InvalidPack("OFS_DELTA base is not an earlier object boundary")
            if cursor >= end:raise InvalidPack("missing packed object compression frame")
            if end - cursor > self.limits.max_compressed_object_bytes:raise ObjectLimitExceeded("compressed packed object exceeds its limit")
            self._entries[start] = PackedEntry(kind, size, cursor, end)

    def read(self, oid: ObjectId) -> RawObject:
        found = self.index.lookup(oid)
        if found is None:raise MissingObject(f"object {oid} is not in the pack index")
        entry = self._entries[found.offset]
        if entry.kind in (6, 7):raise UnsupportedFormat("packed delta resolution is not implemented yet (DX-12)")
        decoder = zlib.decompressobj()
        try:
            payload = decoder.decompress(memoryview(self.data)[entry.compressed_start:entry.end], entry.size + 1)
        except zlib.error as error:
            raise InvalidPack("invalid packed object compression") from error
        if len(payload) > entry.size or decoder.unconsumed_tail:raise InvalidPack("packed inflation exceeds its declared size")
        if not decoder.eof or decoder.unused_data or len(payload) != entry.size:raise InvalidPack("packed object size/framing mismatch")
        obj = RawObject({1: "commit", 2: "tree", 3: "blob", 4: "tag"}[entry.kind], payload)
        if obj.oid(self.index.format) != oid:raise InvalidPack("packed object hash does not match its index ID")
        return obj


class RepositoryObjectStore(LooseObjectStore):
    """Loose-first storage with independently indexed pack fallback."""

    def __init__(self, *args, pack_limits: PackLimits | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pack_limits = pack_limits or PackLimits(max_object_bytes=self.limits.max_object_bytes,
                                                    max_compressed_object_bytes=self.limits.max_compressed_bytes)

    def read(self, oid: ObjectId) -> RawObject:
        try:return super().read(oid)
        except MissingObject:pass
        directory = self.path / "pack"
        from .filesystem import check_path
        check_path(self.path, directory)
        if not directory.exists():raise MissingObject(f"object {oid} is absent from loose and indexed pack storage")
        paths = []
        for path in directory.iterdir():
            if path.name.startswith("pack-") and path.suffix == ".idx":
                if not re.fullmatch(rf"pack-[0-9a-f]{{{self.format.hex_size}}}\.idx", path.name):raise InvalidPack("malformed pack-index filename")
                paths.append(path)
                if len(paths) > self.pack_limits.max_packs:raise ObjectLimitExceeded("pack count exceeds its lookup limit")
        scanned = 0
        for path in sorted(paths):
            raw_index = read_file(self.path, path, self.pack_limits.max_index_bytes, limit_error=ObjectLimitExceeded)
            scanned += len(raw_index)
            if scanned > self.pack_limits.max_scan_bytes:raise ObjectLimitExceeded("pack lookup exceeds its aggregate scan budget")
            index = PackIndex.from_bytes(raw_index, self.format, limits=self.pack_limits)
            if path.stem[5:] != index.pack_checksum.hex():raise InvalidPack("pack filename does not match its checksum")
            if index.lookup(oid) is None:continue
            pack_path = path.with_suffix(".pack")
            remaining = min(self.pack_limits.max_pack_bytes, self.pack_limits.max_scan_bytes - scanned)
            data = read_file(self.path, pack_path, remaining, limit_error=ObjectLimitExceeded)
            return PackFile(data, index, limits=self.pack_limits).read(oid)
        raise MissingObject(f"object {oid} is absent from loose and indexed pack storage")
