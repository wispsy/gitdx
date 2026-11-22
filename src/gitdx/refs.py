"""Loose files refs with explicit expected-old updates and bounded symrefs."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Protocol

from .config import parse_config
from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .filesystem import check_path, read_file
from .names import InvalidRef, validate_refname
from .object_store import ObjectStore
from .objects import ObjectId, ObjectType, parse_object


class RefConflict(GitdxError):
    """The current value differs from the caller's expected value."""


class RefLocked(GitdxError):
    """Another writer owns the selected Git lockfile."""


@dataclass(frozen=True, slots=True)
class DirectRef:
    oid: ObjectId

    def __post_init__(self) -> None:
        if not isinstance(self.oid, ObjectId) or not any(self.oid.bytes):
            raise InvalidRef("direct refs require a non-null ObjectId")


@dataclass(frozen=True, slots=True)
class SymbolicRef:
    target: str

    def __post_init__(self) -> None:
        validate_refname(self.target)


type RefValue = DirectRef | SymbolicRef


@dataclass(frozen=True, slots=True)
class ResolvedRef:
    name: str
    oid: ObjectId | None
    symbolic_chain: tuple[str, ...]

    @property
    def unborn(self) -> bool:
        return bool(self.symbolic_chain and self.symbolic_chain[0] == "HEAD" and self.oid is None)

    @property
    def detached(self) -> bool:
        return self.name == "HEAD" and not self.symbolic_chain and self.oid is not None


class RefStore(Protocol):
    def read(self, name: str) -> RefValue | None: ...

    def resolve(self, name: str = "HEAD") -> ResolvedRef: ...

    def update(self, name: str, new: ObjectId, *, expected: ObjectId | None) -> None: ...


class FilesRefStore:
    """Single-ref transactions only. Packed refs and reflogs are separate work."""

    def __init__(self, git_dir: str | os.PathLike[str], objects: ObjectStore) -> None:
        self.path = Path(git_dir).absolute()
        self.objects = objects

    def _profile(self) -> None:
        config = parse_config(read_file(self.path, self.path / "config", 1024 * 1024))
        if config.object_format != self.objects.format:
            raise UnsupportedFormat("object format changed since the repository was opened")
        for name in ("packed-refs", "commondir"):
            target = self.path / name
            check_path(self.path, target)
            if target.exists():
                raise UnsupportedFormat(f"{name} is not supported by the loose-ref backend")

    def _path(self, name: str) -> Path:
        if name != "HEAD":
            validate_refname(name)
        path = self.path / name
        check_path(self.path, path)
        parent = self.path
        for part in path.relative_to(self.path).parts:
            if parent.is_dir():
                if any(p.name != part and p.name.casefold() == part.casefold() for p in parent.iterdir()):
                    raise InvalidRef("ref name collides with another name under portable case folding")
            parent /= part
        return path

    def read(self, name: str) -> RefValue | None:
        self._profile()
        path = self._path(name)
        try:
            data = read_file(self.path, path, 4096).removesuffix(b"\n")
        except FileNotFoundError:
            return None
        try:
            if data.startswith(b"ref: "):
                return SymbolicRef(data[5:].decode("utf-8"))
            return DirectRef(ObjectId(data.decode("ascii"), self.objects.format))
        except UnicodeError as error:
            raise InvalidRef("ref value must contain a valid name or full object ID") from error

    def resolve(self, name: str = "HEAD") -> ResolvedRef:
        chain: list[str] = []
        for _ in range(16):
            if name in chain:
                raise InvalidRef("symbolic ref cycle")
            value = self.read(name)
            if not isinstance(value, SymbolicRef):
                return ResolvedRef(name, value.oid if value else None, tuple(chain))
            chain.append(name)
            name = value.target
        raise InvalidRef("symbolic ref chain exceeds the supported depth")

    def _target(self, name: str, oid: ObjectId) -> None:
        if oid.format != self.objects.format:
            raise InvalidObject("ref and object store formats differ")
        raw = self.objects.read(oid)
        parse_object(raw, oid.format)
        if (name == "HEAD" or name.startswith("refs/heads/")) and raw.kind is not ObjectType.COMMIT:
            raise InvalidRef("branch and detached HEAD targets must be commits")

    def _write(self, name: str, value: RefValue, expected: RefValue | None) -> None:
        self._profile()
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        check_path(self.path, target)
        lock = target.with_name(target.name + ".lock")
        check_path(self.path, lock)
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RefLocked("ref lock already exists; its owner must resolve it") from error
        try:
            with os.fdopen(fd, "wb") as stream:
                if self.read(name) != expected:
                    raise RefConflict("ref changed since the expected value was read")
                if isinstance(value, SymbolicRef):
                    resolved = self.resolve(value.target)
                    if name == resolved.name or name in resolved.symbolic_chain:
                        raise InvalidRef("symbolic update would create a cycle")
                    if len(resolved.symbolic_chain) >= 15:
                        raise InvalidRef("symbolic update would exceed the supported depth")
                    if resolved.oid is not None:
                        self._target(name, resolved.oid)
                    data = b"ref: " + value.target.encode("utf-8") + b"\n"
                else:
                    data = value.oid.hex.encode("ascii") + b"\n"
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._profile()
            check_path(self.path, target)
            os.replace(lock, target)
        finally:
            lock.unlink(missing_ok=True)

    def update(self, name: str, new: ObjectId, *, expected: ObjectId | None) -> None:
        """Update a direct ref. None means create-only; symbolic refs never detach implicitly."""
        self._path(name)
        self._profile()
        self._target(name, new)
        if expected is not None and expected.format != self.objects.format:
            raise InvalidObject("expected ref ID uses a different object format")
        self._write(name, DirectRef(new), DirectRef(expected) if expected is not None else None)

    def set_symbolic(self, name: str, target: str, *, expected: RefValue | None) -> None:
        self._write(name, SymbolicRef(target), expected)

    def detach_head(self, new: ObjectId, *, expected: RefValue) -> None:
        self._profile()
        self._target("HEAD", new)
        self._write("HEAD", DirectRef(new), expected)
