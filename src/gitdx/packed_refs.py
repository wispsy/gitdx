"""Bounded, sorted packed-refs snapshots, including peeled object IDs."""

from dataclasses import dataclass, replace

from .names import InvalidRef, validate_refname
from .objects import ObjectFormat, ObjectId


@dataclass(frozen=True, slots=True)
class PackedRef:
    name: str
    oid: ObjectId
    peeled: ObjectId | None = None

    def __post_init__(self):
        validate_refname(self.name)
        if not any(self.oid.bytes) or (self.peeled is not None and
                (self.peeled.format != self.oid.format or not any(self.peeled.bytes))):
            raise InvalidRef("packed refs require non-null, format-consistent IDs")


@dataclass(frozen=True, slots=True)
class PackedRefs:
    entries: tuple[PackedRef, ...] = ()
    format: ObjectFormat = ObjectFormat.SHA1
    headers: tuple[bytes, ...] = (b"# pack-refs with: sorted",)

    def __post_init__(self):
        object.__setattr__(self, "format", ObjectFormat(self.format))
        entries = tuple(sorted(self.entries, key=lambda e: e.name))
        if len({e.name.casefold() for e in entries}) != len(entries):
            raise InvalidRef("duplicate or case-aliased packed ref")
        if any(e.oid.format != self.format for e in entries):
            raise InvalidRef("packed refs use a different object format")
        names = {e.name.casefold() for e in entries}
        for entry in entries:
            parts = entry.name.casefold().split("/")
            if any("/".join(parts[:i]) in names for i in range(1, len(parts))):
                raise InvalidRef("packed refs contain a directory/file namespace conflict")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "headers", tuple(self.headers))
        if any(not h.startswith(b"#") or any(c < 32 or c > 126 for c in h) for h in self.headers):
            raise InvalidRef("invalid packed-ref header")

    def encode(self) -> bytes:
        lines = list(self.headers)
        for entry in self.entries:
            lines.append(entry.oid.hex.encode("ascii") + b" " + entry.name.encode("ascii"))
            if entry.peeled is not None:lines.append(b"^" + entry.peeled.hex.encode("ascii"))
        return b"\n".join(lines) + (b"\n" if lines else b"")

    @classmethod
    def from_bytes(cls, data: bytes, format=ObjectFormat.SHA1, *, max_entries=100_000) -> PackedRefs:
        format = ObjectFormat(format)
        if data and not data.endswith(b"\n"):
            raise InvalidRef("packed refs require complete newline-terminated records")
        entries, headers = [], []
        for line in data.split(b"\n")[:-1]:
            if line.startswith(b"#") and not entries:
                headers.append(line);continue
            try:
                if line.startswith(b"^"):
                    if not entries or entries[-1].peeled is not None:raise InvalidRef("orphan or duplicate peeled ref")
                    entries[-1] = replace(entries[-1], peeled=ObjectId(line[1:].decode("ascii"), format))
                    continue
                oid, space, name = line.partition(b" ")
                if not space:raise InvalidRef("invalid packed-ref record")
                entry = PackedRef(name.decode("ascii"), ObjectId(oid.decode("ascii"), format))
            except UnicodeError as error:
                raise InvalidRef("packed refs require supported ASCII names and IDs") from error
            if entries and entries[-1].name >= entry.name:raise InvalidRef("packed refs must be sorted and unique")
            if len(entries) >= max_entries:raise InvalidRef("packed-ref count exceeds its limit")
            entries.append(entry)
        return cls(tuple(entries), format, tuple(headers))
