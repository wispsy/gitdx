"""Files refs, packed snapshots and explicit expected-old mutation outcomes."""

from dataclasses import dataclass
from contextlib import contextmanager
import os
from pathlib import Path
from typing import Protocol

from .config import parse_config
from .repository_profile import check_repository_features
from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .filesystem import check_path, read_file, replace_file
from .names import InvalidRef, validate_refname
from .object_store import ObjectStore
from .objects import ObjectId, ObjectType, parse_object
from .packed_refs import PackedRefs
from .reflog import ReflogReceipt, ReflogStore, ReflogUpdate


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
class RefUpdateResult:
    name: str
    old_value: RefValue | None
    new_value: RefValue | None
    reflog_receipt: ReflogReceipt | None = None
    reflog_error: str | None = None

    @property
    def committed(self) -> bool:
        return True


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

    def update(self, name: str, new: ObjectId, *, expected: ObjectId | None, reflog: ReflogUpdate | None = None) -> RefUpdateResult: ...

    def delete(self, name: str, *, expected: ObjectId, reflog: ReflogUpdate | None = None) -> RefUpdateResult: ...


class FilesRefStore:
    """Single-ref transactions; optional reflog failure never hides a committed ref."""

    def __init__(self, git_dir: str | os.PathLike[str], objects: ObjectStore) -> None:
        self.path = Path(git_dir).absolute()
        self.objects = objects

    def _profile(self) -> None:
        config = parse_config(read_file(self.path, self.path / "config", 1024 * 1024))
        check_repository_features(self.path,config)
        if config.object_format != self.objects.format:
            raise UnsupportedFormat("object format changed since the repository was opened")

    def packed(self) -> PackedRefs:
        self._profile()
        try:data = read_file(self.path, self.path / "packed-refs", 16 * 1024 * 1024)
        except FileNotFoundError:return PackedRefs(format=self.objects.format)
        return PackedRefs.from_bytes(data, self.objects.format)

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
        packed = self.packed()
        for entry in packed.entries:
            if entry.name != name and entry.name.casefold() == name.casefold():
                raise InvalidRef("ref name aliases a packed name under portable case folding")
        try:
            data = read_file(self.path, path, 4096).removesuffix(b"\n")
        except FileNotFoundError:
            return next((DirectRef(e.oid) for e in packed.entries if e.name == name), None)
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

    @contextmanager
    def _lock(self, target):
        check_path(self.path, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock = target.with_name(target.name + ".lock")
        check_path(self.path, lock)
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RefLocked("ref or packed-refs lock already exists; its owner must resolve it") from error
        os.close(fd)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _log_result(self, name, old, new, old_oid, new_oid, update):
        if update is None:return RefUpdateResult(name, old, new)
        logs, receipt = ReflogStore(self.path), None
        zero = ObjectId("0" * self.objects.format.hex_size, self.objects.format)
        try:
            receipt = logs.prepare(name, old_oid or zero, new_oid or zero, update)
            logs.append(receipt)
        except (OSError, GitdxError, ValueError) as error:
            return RefUpdateResult(name, old, new, receipt, type(error).__name__)
        return RefUpdateResult(name, old, new, receipt)

    @contextmanager
    def _mutation_locks(self, name):
        target = self._path(name)
        with self._lock(self.path / "packed-refs"):
            self._profile()
            packed = self.packed()
            for entry in packed.entries:
                a, b = entry.name.casefold(), name.casefold()
                if entry.name != name and (a == b or a.startswith(b + "/") or b.startswith(a + "/")):
                    raise InvalidRef("ref conflicts with the packed namespace")
            # Validate packed namespace before creating any loose directories.
            with self._lock(target):
                yield target, packed

    def _write(self, name: str, value: RefValue | None, expected: RefValue | None, reflog=None) -> RefUpdateResult:
        from .ref_transaction import require_no_ref_transaction
        require_no_ref_transaction(self.path)
        operation = self.path/"operation-state";check_path(self.path, operation)
        if operation.exists():raise UnsupportedFormat("worktree operation recovery is pending")
        pending=self.path/"checkout-state";check_path(self.path,pending)
        if pending.exists():raise UnsupportedFormat("checkout recovery is pending")
        if reflog is not None and not isinstance(reflog, ReflogUpdate):raise TypeError("reflog must be a ReflogUpdate")
        self._profile()
        with self._mutation_locks(name) as (target, packed):
            require_no_ref_transaction(self.path)
            check_path(self.path, operation)
            if operation.exists():raise UnsupportedFormat("worktree operation recovery is pending")
            check_path(self.path,pending)
            if pending.exists():raise UnsupportedFormat("checkout recovery is pending")
            old = self.read(name)
            if old != expected:raise RefConflict("ref changed since the expected value was read")
            old_oid = self.resolve(name).oid
            if isinstance(value, SymbolicRef):
                resolved = self.resolve(value.target)
                if name == resolved.name or name in resolved.symbolic_chain:raise InvalidRef("symbolic update would create a cycle")
                if len(resolved.symbolic_chain) >= 15:raise InvalidRef("symbolic update would exceed the supported depth")
                if resolved.oid is not None:self._target(name, resolved.oid)
                data = b"ref: " + value.target.encode("utf-8") + b"\n"
                new_oid = resolved.oid
            elif value is not None:
                data, new_oid = value.oid.hex.encode("ascii") + b"\n", value.oid
            else:
                new_oid = None
            if value is None:
                loose_present = target.exists()
                remaining = tuple(e for e in packed.entries if e.name != name)
                if len(remaining) != len(packed.entries):
                    replacement = PackedRefs(remaining, packed.format, packed.headers).encode()
                    # Keep packed-refs.lock present across publication AND loose
                    # deletion, preventing native pack-refs from restoring old data.
                    replace_file(self.path, self.path / "packed-refs", replacement)
                check_path(self.path, target)
                if loose_present:target.unlink()
            else:
                self._profile()
                replace_file(self.path, target, data)
            return self._log_result(name, old, value, old_oid, new_oid, reflog)

    def update(self, name: str, new: ObjectId, *, expected: ObjectId | None, reflog: ReflogUpdate | None = None) -> RefUpdateResult:
        """Update a direct ref. None means create-only; symbolic refs never detach implicitly."""
        self._path(name)
        self._profile()
        self._target(name, new)
        if expected is not None and expected.format != self.objects.format:
            raise InvalidObject("expected ref ID uses a different object format")
        return self._write(name, DirectRef(new), DirectRef(expected) if expected is not None else None, reflog)

    def set_symbolic(self, name: str, target: str, *, expected: RefValue | None, reflog: ReflogUpdate | None = None) -> RefUpdateResult:
        return self._write(name, SymbolicRef(target), expected, reflog)

    def detach_head(self, new: ObjectId, *, expected: RefValue, reflog: ReflogUpdate | None = None) -> RefUpdateResult:
        self._profile()
        self._target("HEAD", new)
        return self._write("HEAD", DirectRef(new), expected, reflog)

    def delete(self, name: str, *, expected: ObjectId, reflog: ReflogUpdate | None = None) -> RefUpdateResult:
        if name == "HEAD":raise InvalidRef("HEAD cannot be deleted")
        if expected.format != self.objects.format:raise InvalidObject("expected ref ID uses a different object format")
        return self._write(name, None, DirectRef(expected), reflog)

    def repair_reflog(self, result: RefUpdateResult) -> None:
        from .ref_transaction import require_no_ref_transaction
        require_no_ref_transaction(self.path)
        receipt = result.reflog_receipt
        if receipt is None:raise InvalidRef("no safe append receipt; inspect the reflog manually")
        if receipt.name != result.name or receipt.new_oid.format != self.objects.format:
            raise InvalidRef("reflog receipt does not match the ref result")
        with self._mutation_locks(result.name):
            require_no_ref_transaction(self.path)
            current = self.read(result.name)
            resolved = self.resolve(result.name).oid
            wanted = receipt.new_oid if any(receipt.new_oid.bytes) else None
            if current != result.new_value or resolved != wanted:raise RefConflict("ref moved since the append receipt")
            ReflogStore(self.path).repair(receipt)

    def transaction(self, changes, *, atomic=False, check=None, before_publish=None):
        from .ref_transaction import transaction
        return transaction(self,changes,atomic=atomic,check=check,before_publish=before_publish)

    def recover_transaction(self, *, check=None):
        from .ref_transaction import recover_transaction
        return recover_transaction(self,check=check)
