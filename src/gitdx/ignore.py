"""Scoped Git ignore rules for the documented local working-tree profile."""

from dataclasses import dataclass
import fnmatch

from .errors import UnsupportedFormat


def _component(pattern: bytes) -> bytes:
    result = bytearray();i = 0
    while i < len(pattern):
        if pattern[i] == 92:
            i += 1
            if i == len(pattern):raise UnsupportedFormat("dangling escape in ignore pattern")
            c = pattern[i:i + 1]
            result.extend({b"*":b"[*]",b"?":b"[?]",b"[":b"[[]"}.get(c,c))
        else:result.append(pattern[i])
        i += 1
    return bytes(result)


def _match(pattern: tuple[bytes, ...], parts: tuple[bytes, ...]) -> bool:
    positions = {0}
    for n,component in enumerate(pattern):
        if component == b"**":
            start = min(positions) if positions else 0
            if n==len(pattern)-1 and n>0:start += 1
            positions = set(range(start, len(parts) + 1)) if positions else set()
        else:
            positions = {i + 1 for i in positions if i < len(parts) and fnmatch.fnmatchcase(parts[i], _component(component))}
    return len(parts) in positions


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    base: bytes
    pattern: tuple[bytes, ...]
    basename: bool
    directory: bool
    negate: bool

    def matches(self, path: bytes, directory: bool) -> bool:
        if self.directory and not directory:return False
        if self.base:
            if not path.startswith(self.base + b"/"):return False
            path = path[len(self.base) + 1:]
        parts = tuple(path.split(b"/"))
        return _match(self.pattern, parts[-1:] if self.basename else parts)


def parse_ignore(data: bytes, base: bytes = b"") -> tuple[IgnoreRule, ...]:
    rules = []
    for line in data.removeprefix(b'\xef\xbb\xbf').split(b"\n"):
        line = line.removesuffix(b"\r")
        while line.endswith(b" "):
            n = len(line) - 2
            while n >= 0 and line[n] == 92:n -= 1
            if (len(line) - 2 - n) % 2:break
            line = line[:-1]
        if not line or line.startswith(b"#"):continue
        if len(line) > 4096:raise UnsupportedFormat("ignore pattern exceeds the supported byte limit")
        negate = line.startswith(b"!")
        if negate:line = line[1:]
        directory = line.endswith(b"/")
        if directory:line = line[:-1]
        anchored = line.startswith(b"/")
        if anchored:line = line[1:]
        if not line:continue
        pattern = tuple(line.split(b"/"))
        if any(any(marker in part for marker in (b'[:',b'[.',b'[=',b'[^')) for part in pattern):
            raise UnsupportedFormat("unsupported ignore character-class syntax")
        for part in pattern:_component(part)
        rules.append(IgnoreRule(base, pattern, not anchored and len(pattern) == 1, directory, negate))
    return tuple(rules)
