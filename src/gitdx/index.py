"""Git index v2 snapshots and explicitly committed, exclusive transactions."""

from dataclasses import dataclass, fields
import hashlib
import os
from pathlib import Path
import struct

from .config import parse_config
from .repository_profile import check_repository_features
from .errors import GitdxError, UnsupportedFormat
from .filesystem import check_path, read_file
from .objects import ObjectFormat, ObjectId


class InvalidIndex(GitdxError, ValueError):
    """The index has invalid structure, ordering or checksum."""


class IndexLocked(GitdxError):
    """Another writer owns index.lock."""


@dataclass(frozen=True, slots=True)
class IndexLimits:
    max_bytes: int = 64 * 1024 * 1024
    max_entries: int = 250_000
    max_path_bytes: int = 4096
    max_extension_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0 for f in fields(self)):
            raise ValueError("index limits must be positive integers")


@dataclass(frozen=True, slots=True)
class IndexStat:
    ctime_seconds: int = 0
    ctime_nanoseconds: int = 0
    mtime_seconds: int = 0
    mtime_nanoseconds: int = 0
    device: int = 0
    inode: int = 0
    uid: int = 0
    gid: int = 0
    size: int = 0

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or not 0 <= getattr(self, f.name) <= 0xffffffff for f in fields(self)):
            raise InvalidIndex("stat fields must be unsigned 32-bit integers")
        if self.ctime_nanoseconds >= 1_000_000_000 or self.mtime_nanoseconds >= 1_000_000_000:
            raise InvalidIndex("stat nanoseconds must be below one second")


@dataclass(frozen=True, slots=True)
class IndexEntry:
    path: bytes
    oid: ObjectId
    mode: int = 0o100644
    stage: int = 0
    stat: IndexStat = IndexStat()
    assume_valid: bool = False

    def __post_init__(self):
        if not isinstance(self.path, bytes):
            raise TypeError("index paths must be immutable bytes")
        if not self.path or b"\0" in self.path or any(p in (b"", b".", b"..", b".git") for p in self.path.split(b"/")):
            raise InvalidIndex("index paths must be relative file paths without reserved components")
        if self.mode == 0o40000:
            raise UnsupportedFormat("sparse directory index entries are unsupported")
        if self.mode not in (0o100644, 0o100755, 0o120000, 0o160000):
            raise InvalidIndex("unsupported index file mode")
        if type(self.stage) is not int or not 0 <= self.stage <= 3:
            raise InvalidIndex("index stage must be 0 through 3")
        if not isinstance(self.oid, ObjectId) or not any(self.oid.bytes):
            raise InvalidIndex("index entry requires a non-null object ID")
        if not isinstance(self.stat, IndexStat) or type(self.assume_valid) is not bool:
            raise TypeError("index stat and assume-valid fields must be typed values")


@dataclass(frozen=True, slots=True)
class IndexExtension:
    signature: bytes
    data: bytes

    def __post_init__(self):
        if not isinstance(self.signature, bytes) or len(self.signature) != 4:
            raise InvalidIndex("index extension signatures are four bytes")
        if not 65 <= self.signature[0] <= 90:
            raise UnsupportedFormat("mandatory index extensions, including split/sparse indexes, are unsupported")
        if not isinstance(self.data, bytes):
            raise TypeError("extension data must be immutable bytes")


@dataclass(frozen=True, slots=True)
class Index:
    entries: tuple[IndexEntry, ...] = ()
    format: ObjectFormat = ObjectFormat.SHA1
    extensions: tuple[IndexExtension, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "format", ObjectFormat(self.format))
        entries = tuple(sorted(self.entries, key=lambda e: (e.path, e.stage)))
        previous = None
        stages: set[int] = set()
        for entry in entries:
            if entry.oid.format != self.format:
                raise InvalidIndex("index entry object format mismatch")
            if previous != entry.path:
                previous, stages = entry.path, set()
            if entry.stage in stages or (stages and (entry.stage == 0 or 0 in stages)):
                raise InvalidIndex("duplicate or mixed stage-0/conflict entries")
            stages.add(entry.stage)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "extensions", tuple(self.extensions))
        if len({e.signature for e in self.extensions}) != len(self.extensions):
            raise InvalidIndex("duplicate index extension")

    def with_entries(self, entries) -> Index:
        candidate = Index(tuple(entries), self.format)
        return self if candidate.entries == self.entries else candidate

    def encode(self, limits: IndexLimits | None = None) -> bytes:
        limits = limits or IndexLimits()
        if len(self.entries) > limits.max_entries:
            raise InvalidIndex("index entry count exceeds its limit")
        output = bytearray(struct.pack(">4sII", b"DIRC", 2, len(self.entries)))
        for entry in self.entries:
            if len(entry.path) > limits.max_path_bytes:
                raise InvalidIndex("index path exceeds its byte limit")
            s = entry.stat
            data = struct.pack(">10I", s.ctime_seconds, s.ctime_nanoseconds, s.mtime_seconds,
                               s.mtime_nanoseconds, s.device, s.inode, entry.mode, s.uid, s.gid, s.size)
            flags = (int(entry.assume_valid) << 15) | (entry.stage << 12) | min(len(entry.path), 0xfff)
            data += entry.oid.bytes + struct.pack(">H", flags) + entry.path
            data += b"\0" * (8 - len(data) % 8)
            if len(output) + len(data) + self.format.digest_size > limits.max_bytes:
                raise InvalidIndex("index exceeds its byte limit")
            output.extend(data)
        extension_bytes = 0
        for extension in self.extensions:
            extension_bytes += 8 + len(extension.data)
            if extension_bytes > limits.max_extension_bytes or len(output) + 8 + len(extension.data) + self.format.digest_size > limits.max_bytes:
                raise InvalidIndex("index extensions exceed their byte limit")
            output.extend(extension.signature + struct.pack(">I", len(extension.data)) + extension.data)
        output.extend(hashlib.new(self.format.value, output).digest())
        if len(output) > limits.max_bytes:
            raise InvalidIndex("index exceeds its byte limit")
        return bytes(output)

    @classmethod
    def from_bytes(cls, data: bytes, format: ObjectFormat = ObjectFormat.SHA1,
                   *, limits: IndexLimits | None = None) -> Index:
        limits, format = limits or IndexLimits(), ObjectFormat(format)
        width = format.digest_size
        if len(data) < 12 + width or len(data) > limits.max_bytes:
            raise InvalidIndex("truncated or oversized index")
        if data[:4] != b"DIRC":
            raise InvalidIndex("invalid index signature")
        version, count = struct.unpack_from(">II", data, 4)
        if version != 2:
            raise UnsupportedFormat("only Git index version 2 is supported")
        if hashlib.new(format.value, data[:-width]).digest() != data[-width:]:
            raise InvalidIndex("index checksum mismatch")
        end = len(data) - width
        fixed = 42 + width
        if count > limits.max_entries or count > (end - 12) // (fixed + 1):
            raise InvalidIndex("invalid or excessive index entry count")
        entries = []
        offset = 12
        for _ in range(count):
            start = offset
            if offset + fixed > end:
                raise InvalidIndex("truncated index entry")
            values = struct.unpack_from(">10I", data, offset)
            oid = ObjectId.from_bytes(data[offset + 40:offset + 40 + width], format)
            flags = struct.unpack_from(">H", data, offset + 40 + width)[0]
            if flags & 0x4000:
                raise UnsupportedFormat("extended index flags require an unsupported version")
            offset += fixed
            nul = data.find(b"\0", offset, min(end, offset + limits.max_path_bytes + 1))
            if nul < 0:
                raise InvalidIndex("missing or oversized index path")
            path = data[offset:nul]
            padding = 8 - (fixed + len(path)) % 8
            offset = nul + padding
            if offset > end or data[nul:offset] != b"\0" * padding or flags & 0xfff != min(len(path), 0xfff):
                raise InvalidIndex("invalid index padding or path length flags")
            stat = IndexStat(*values[:6], *values[7:])
            entries.append(IndexEntry(path, oid, values[6], (flags >> 12) & 3, stat, bool(flags & 0x8000)))
            if offset <= start:
                raise InvalidIndex("invalid index entry boundary")
        if entries != sorted(entries, key=lambda e: (e.path, e.stage)):
            raise InvalidIndex("index entries are not sorted")
        extensions = []
        extension_start = offset
        while offset < end:
            if end - offset < 8:
                raise InvalidIndex("truncated index extension")
            signature = data[offset:offset + 4]
            size = struct.unpack_from(">I", data, offset + 4)[0]
            offset += 8
            if size > end - offset or offset + size - extension_start > limits.max_extension_bytes:
                raise InvalidIndex("truncated or oversized index extension")
            extensions.append(IndexExtension(signature, data[offset:offset + size]))
            offset += size
        return cls(tuple(entries), format, tuple(extensions))


class IndexFile:
    def __init__(self, git_dir, format: ObjectFormat = ObjectFormat.SHA1, *, limits: IndexLimits | None = None):
        self.git_dir = Path(git_dir).absolute()
        self.path = self.git_dir / "index"
        self.format, self.limits = ObjectFormat(format), limits or IndexLimits()

    def _profile(self):
        config = parse_config(read_file(self.git_dir, self.git_dir / "config", 1024 * 1024))
        check_repository_features(self.git_dir,config)
        if config.object_format != self.format or config.bare:
            raise UnsupportedFormat("index transactions require the selected non-bare repository format")

    def read(self) -> Index:
        self._profile()
        try:
            data = read_file(self.git_dir, self.path, self.limits.max_bytes, limit_error=InvalidIndex)
        except FileNotFoundError:
            return Index(format=self.format)
        return Index.from_bytes(data, self.format, limits=self.limits)

    def transaction(self) -> IndexTransaction:
        return IndexTransaction(self)


class IndexTransaction:
    def __init__(self, file: IndexFile):
        self.file = file
        self.lock = file.path.with_name("index.lock")
        self._stream = None
        self._used = False
        self._owns_lock = False
        self._index: Index | None = None

    @property
    def index(self) -> Index:
        if self._index is None:
            raise RuntimeError("index transaction has not been entered")
        return self._index

    def __enter__(self):
        if self._used:
            raise RuntimeError("index transactions cannot be reused")
        self._used = True
        from .ref_transaction import require_no_ref_transaction
        require_no_ref_transaction(self.file.git_dir)
        pending = self.file.git_dir/"checkout-state"
        check_path(self.file.git_dir,pending)
        if pending.exists():raise UnsupportedFormat("checkout recovery is pending")
        self.file._profile()
        check_path(self.file.git_dir, self.lock)
        try:
            fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise IndexLocked("index.lock already exists; its owner must resolve it") from error
        self._owns_lock = True
        self._stream = os.fdopen(fd, "wb")
        try:
            require_no_ref_transaction(self.file.git_dir)
            check_path(self.file.git_dir,pending)
            if pending.exists():raise UnsupportedFormat("checkout recovery is pending")
            self._index = self.file.read()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def replace(self, entries):
        if self._stream is None:
            raise RuntimeError("index transaction is not active")
        self._index = self.index.with_entries(entries)

    def commit(self):
        if self._stream is None:
            raise RuntimeError("index transaction is not active")
        data = self.index.encode(self.file.limits)
        stream = self._stream
        # An attempted publication consumes the stream even when an I/O error
        # is caught inside the context. Retrying cannot append a second index.
        self._stream = None
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.file._profile()
        from .ref_transaction import require_no_ref_transaction
        require_no_ref_transaction(self.file.git_dir)
        check_path(self.file.git_dir, self.file.path)
        os.replace(self.lock, self.file.path)
        self._owns_lock = False

    def __exit__(self, *exc):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._owns_lock:
            self.lock.unlink(missing_ok=True)
            self._owns_lock = False
