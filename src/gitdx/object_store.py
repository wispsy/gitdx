"""Bounded loose-object reading and publish-if-absent atomic writes."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol
import zlib

from .errors import GitdxError, InvalidObject
from .filesystem import check_path, read_file, storage_is_dir
from .confined_fs import active
from .objects import ObjectFormat, ObjectId, RawObject


class MissingObject(GitdxError, LookupError):
    """An object is absent from the selected object store."""


class ObjectLimitExceeded(GitdxError, ValueError):
    """An object exceeds a configured storage budget."""


@dataclass(frozen=True, slots=True)
class StoreLimits:
    max_object_bytes: int = 64 * 1024 * 1024
    max_compressed_bytes: int = 64 * 1024 * 1024
    max_header_bytes: int = 64

    def __post_init__(self) -> None:
        for limit in (self.max_object_bytes, self.max_compressed_bytes, self.max_header_bytes):
            if type(limit) is not int or limit <= 0:
                raise ValueError("object limits must be positive integers")


class ObjectStore(Protocol):
    format: ObjectFormat

    def read(self, oid: ObjectId) -> RawObject: ...

    def write(self, obj: RawObject) -> ObjectId: ...


class LooseObjectStore:
    """Loose objects under an existing, trusted `objects` directory.

    fsync precedes publication. A same-directory hard link publishes a complete
    file without overwriting a competing writer. Full power-loss durability and
    protection from concurrent hostile directory replacement are not promised.
    """

    def __init__(self, path: str | os.PathLike[str], format: ObjectFormat = ObjectFormat.SHA1,
                 *, limits: StoreLimits | None = None) -> None:
        self.path = Path(path).absolute()
        self.format = ObjectFormat(format)
        self.limits = limits or StoreLimits()
        check_path(self.path, self.path)
        if not storage_is_dir(self.path):
            raise NotADirectoryError("object store must be an existing directory")

    def object_path(self, oid: ObjectId) -> Path:
        if oid.format != self.format:
            raise InvalidObject("object ID format does not match the object store")
        path = self.path / oid.hex[:2] / oid.hex[2:]
        check_path(self.path, path)
        return path

    def read(self, oid: ObjectId) -> RawObject:
        path = self.object_path(oid)
        try:
            # Read at most the compressed budget before allocating expanded data.
            compressed = read_file(self.path, path, self.limits.max_compressed_bytes,
                                   limit_error=ObjectLimitExceeded)
        except FileNotFoundError as error:
            raise MissingObject(f"object {oid} is not present in loose storage; packed objects are not supported yet") from error
        decoder = zlib.decompressobj()
        maximum = self.limits.max_object_bytes + self.limits.max_header_bytes
        try:
            data = decoder.decompress(compressed, maximum + 1)
        except zlib.error as error:
            raise InvalidObject("invalid loose-object compression") from error
        if len(data) > maximum or decoder.unconsumed_tail:
            raise ObjectLimitExceeded("expanded object exceeds its configured byte limit")
        if not decoder.eof or decoder.unused_data:
            raise InvalidObject("truncated or trailing loose-object compression data")
        nul = data.find(b"\0", 0, self.limits.max_header_bytes)
        if nul < 0:
            raise InvalidObject("missing or oversized loose-object header")
        match = re.fullmatch(rb"(blob|tree|commit|tag) (0|[1-9][0-9]*)", data[:nul])
        if not match:
            raise InvalidObject("invalid loose-object type or size header")
        payload = data[nul + 1:]
        declared = int(match[2])
        if declared > self.limits.max_object_bytes or len(payload) > self.limits.max_object_bytes:
            raise ObjectLimitExceeded("object payload exceeds its configured byte limit")
        if declared != len(payload):
            raise InvalidObject("loose-object payload size does not match its header")
        obj = RawObject(match[1].decode("ascii"), payload)
        if obj.oid(self.format) != oid:
            raise InvalidObject("loose-object contents do not match the requested object ID")
        return obj

    def write(self, obj: RawObject, *, reserve=None) -> ObjectId:
        if len(obj.data) > self.limits.max_object_bytes:
            raise ObjectLimitExceeded("object payload exceeds its configured byte limit")
        encoded = obj.encode()
        if len(encoded) - len(obj.data) > self.limits.max_header_bytes:
            raise ObjectLimitExceeded("object header exceeds its configured byte limit")
        compressed = zlib.compress(encoded)
        if len(compressed) > self.limits.max_compressed_bytes:
            raise ObjectLimitExceeded("compressed object exceeds its configured byte limit")
        oid = obj.oid(self.format)
        target = self.object_path(oid)
        scope=active()
        if scope is not None:
            try:scope.write(target,compressed,immutable=True,reserve=reserve)
            except FileExistsError:
                if self.read(oid)!=obj:raise InvalidObject('existing object conflicts with the new object')
            return oid
        if reserve is not None:reserve(len(compressed))
        target.parent.mkdir(exist_ok=True)
        check_path(self.path, target)
        fd, name = tempfile.mkstemp(prefix="tmp_obj_", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(compressed)
                stream.flush()
                os.fsync(stream.fileno())
            check_path(self.path, target)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if self.read(oid) != obj:
                    raise InvalidObject("existing object conflicts with the new object")
        finally:
            temporary.unlink(missing_ok=True)
        return oid
