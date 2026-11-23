"""Explicit reflog metadata and recoverable post-commit append receipts."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import os
import re

from .filesystem import check_path, read_file, replace_file
from .names import InvalidRef, validate_refname
from .objects import ObjectId


@dataclass(frozen=True, slots=True)
class ReflogUpdate:
    identity: bytes
    message: bytes = b""

    def __post_init__(self):
        if not isinstance(self.identity, bytes) or not re.fullmatch(rb"[^<>\x00-\x1f\x7f]+ <[^<>\x00-\x20\x7f]+> [0-9]+ [+-][0-9]{4}", self.identity):
            raise InvalidRef("reflog identity must include name, email, timestamp and timezone")
        if not isinstance(self.message, bytes) or any(c in self.message for c in (b"\0", b"\r", b"\n")):
            raise InvalidRef("reflog message must be one line of bytes")


@dataclass(frozen=True, slots=True)
class ReflogReceipt:
    name: str
    old_oid: ObjectId
    new_oid: ObjectId
    line: bytes
    offset: int
    prefix_digest: bytes


class ReflogStore:
    def __init__(self, root: Path, *, max_bytes=8 * 1024 * 1024):
        self.root, self.max_bytes = root, max_bytes

    def path(self, name):
        if name != "HEAD":validate_refname(name)
        path = self.root / "logs" / name
        check_path(self.root, path)
        return path

    def _read(self, path):
        try:return read_file(self.root, path, self.max_bytes)
        except FileNotFoundError:return b""

    def prepare(self, name, old_oid, new_oid, update):
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        check_path(self.root, path)
        prior = self._read(path)
        if prior and not prior.endswith(b"\n"):raise InvalidRef("existing reflog has an incomplete record")
        line = f"{old_oid} {new_oid} ".encode("ascii") + update.identity + b"\t" + update.message + b"\n"
        if len(prior) + len(line) > self.max_bytes:raise InvalidRef("reflog exceeds its byte limit")
        return ReflogReceipt(name, old_oid, new_oid, line, len(prior), hashlib.sha256(prior).digest())

    def append(self, receipt):
        path = self.path(receipt.name)
        with path.open("ab") as stream:
            stream.write(receipt.line)
            stream.flush()
            os.fsync(stream.fileno())

    def repair(self, receipt):
        path = self.path(receipt.name)
        current = self._read(path)
        if receipt.offset < 0 or receipt.offset + len(receipt.line) > self.max_bytes:
            raise InvalidRef("reflog receipt exceeds its byte limit")
        prefix, tail = current[:receipt.offset], current[receipt.offset:]
        if len(prefix) != receipt.offset or hashlib.sha256(prefix).digest() != receipt.prefix_digest or not receipt.line.startswith(tail):
            raise InvalidRef("reflog changed since the failed append; manual reconciliation required")
        # Full lines, absent lines and partial writes all converge to one copy.
        replace_file(self.root, path, prefix + receipt.line)
