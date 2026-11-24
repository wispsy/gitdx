"""Deterministic, bounded diffs of byte strings and explicit tree snapshots."""

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from .errors import InvalidObject
from .object_store import ObjectLimitExceeded
from .objects import ObjectId, ObjectType, Tree


@dataclass(frozen=True, slots=True)
class DiffLimits:
    max_paths: int = 100_000
    max_depth: int = 128
    max_bytes: int = 64 * 1024 * 1024
    max_lines: int = 100_000
    max_work: int = 4_000_000
    max_rename_pairs: int = 10_000
    max_hunks: int = 10_000
    max_output: int = 16 * 1024 * 1024

    def __post_init__(self):
        if any(type(getattr(self, k)) is not int or getattr(self, k) <= 0 for k in self.__slots__):
            raise ValueError('diff limits must be positive integers')


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: str
    data: bytes


@dataclass(frozen=True, slots=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True, slots=True)
class BlobDiff:
    binary: bool
    hunks: tuple[DiffHunk, ...]


@dataclass(frozen=True, slots=True)
class FileDiff:
    status: str
    old_path: bytes | None
    new_path: bytes | None
    old_mode: int | None
    new_mode: int | None
    old_oid: ObjectId | None
    new_oid: ObjectId | None
    old_data: bytes | None
    new_data: bytes | None
    binary: bool
    hunks: tuple[DiffHunk, ...]
    similarity: int | None = None

    def patch(self, *, limits=None):
        """Return raw patch bytes; use render_patch for terminal presentation."""
        limits = limits or DiffLimits()
        old = self.old_path if self.old_path is not None else self.new_path
        new = self.new_path if self.new_path is not None else self.old_path
        result = bytearray()

        def emit(data):
            if len(result) + len(data) > limits.max_output:
                raise ObjectLimitExceeded('diff output byte limit exceeded')
            result.extend(data)

        emit(b'diff --git ' + quote_path(b'a/' + old) + b' ' + quote_path(b'b/' + new) + b'\n')
        if self.old_mode is None:emit(b'new file mode %06o\n' % self.new_mode)
        elif self.new_mode is None:emit(b'deleted file mode %06o\n' % self.old_mode)
        elif self.old_mode != self.new_mode:
            emit(b'old mode %06o\nnew mode %06o\n' % (self.old_mode, self.new_mode))
        if self.status == 'R':
            emit(b'similarity index %d%%\nrename from ' % self.similarity + quote_path(old) + b'\nrename to ' + quote_path(new) + b'\n')
        if self.old_oid != self.new_oid:
            width = (self.old_oid or self.new_oid).format.hex_size
            before = str(self.old_oid).encode() if self.old_oid else b'0' * width
            after = str(self.new_oid).encode() if self.new_oid else b'0' * width
            mode = b' %06o' % self.old_mode if self.old_mode == self.new_mode else b''
            emit(b'index ' + before + b'..' + after + mode + b'\n')
        a = quote_path(b'a/' + old) if self.old_path is not None else b'/dev/null'
        b = quote_path(b'b/' + new) if self.new_path is not None else b'/dev/null'
        if self.binary and self.old_oid != self.new_oid:
            emit(b'Binary files ' + a + b' and ' + b + b' differ\n')
        elif self.hunks:
            emit(b'--- ' + a + b'\n+++ ' + b + b'\n')
            for hunk in self.hunks:
                emit(b'@@ -%s +%s @@\n' % (_range(hunk.old_start, hunk.old_count), _range(hunk.new_start, hunk.new_count)))
                for line in hunk.lines:
                    emit(line.kind.encode() + line.data)
                    if not line.data.endswith(b'\n'):emit(b'\n\\ No newline at end of file\n')
        return bytes(result)


def quote_path(path):
    """Git-style ASCII C quoting, independent of locale and core.quotePath."""
    if not isinstance(path, bytes):raise TypeError('diff paths must be bytes')
    if all(33 <= b <= 126 and b not in (34, 92) for b in path):return path
    escapes = {9: b'\\t', 10: b'\\n', 13: b'\\r', 34: b'\\"', 92: b'\\\\'}
    return b'"' + b''.join(escapes.get(b, bytes((b,)) if 32 <= b <= 126 else b'\\%03o' % b) for b in path) + b'"'


def render_patch(data):
    """Escape controls/non-ASCII for display, leaving raw patch/content untouched."""
    if not isinstance(data, bytes):raise TypeError('patch presentation requires bytes')
    return ''.join(chr(b) if b == 10 or 32 <= b <= 126 else '\\x%02x' % b for b in data)


def _range(start, count):
    if count == 0:return ('%d,0' % (start - 1)).encode()
    return (str(start) if count == 1 else '%d,%d' % (start, count)).encode()


class _Budget:
    def __init__(self, limits):self.limits, self.bytes, self.work, self.hunks = limits, 0, 0, 0

    def charge(self, size):
        self.bytes += size
        if self.bytes > self.limits.max_bytes:raise ObjectLimitExceeded('diff input byte limit exceeded')

    def compare(self, work):
        self.work += work
        if self.work > self.limits.max_work:raise ObjectLimitExceeded('diff comparison work limit exceeded')


def _lines(data, limits):
    # LF is the sole separator. CR, invalid UTF-8 and an absent final LF survive.
    if data.count(b'\n') + bool(data and not data.endswith(b'\n')) > limits.max_lines:
        raise ObjectLimitExceeded('diff line limit exceeded')
    parts = data.split(b'\n')
    return [line + b'\n' for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _diff_bytes(old, new, context, budget):
    if not isinstance(old, bytes) or not isinstance(new, bytes):raise TypeError('diff content must be bytes')
    if type(context) is not int or not 0 <= context <= 1_000_000:raise ValueError('invalid diff context')
    binary = b'\0' in old or b'\0' in new
    if binary or old == new:return BlobDiff(binary, ())
    a, b = _lines(old, budget.limits), _lines(new, budget.limits)
    budget.compare(len(a) * len(b) + len(a) + len(b))
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    hunks = []
    for group in matcher.get_grouped_opcodes(context):
        budget.hunks += 1
        if budget.hunks > budget.limits.max_hunks:raise ObjectLimitExceeded('diff hunk limit exceeded')
        first, last = group[0], group[-1];lines = []
        for kind, i, j, k, l in group:
            if kind == 'equal':lines.extend(DiffLine(' ', line) for line in a[i:j])
            else:
                lines.extend(DiffLine('-', line) for line in a[i:j])
                lines.extend(DiffLine('+', line) for line in b[k:l])
        hunks.append(DiffHunk(first[1] + 1, last[2] - first[1], first[3] + 1, last[4] - first[3], tuple(lines)))
    return BlobDiff(False, tuple(hunks))


def diff_bytes(old, new, *, context=3, limits=None):
    budget = _Budget(limits or DiffLimits())
    if not isinstance(old, bytes) or not isinstance(new, bytes):raise TypeError('diff content must be bytes')
    budget.charge(len(old) + len(new))
    return _diff_bytes(old, new, context, budget)


def _tree(repo, root, budget):
    if root is None:return {}
    if not isinstance(root, ObjectId) or root.format != repo.object_format:raise InvalidObject('diff tree has a different object format')
    stack = [(b'', root, 0)];files = {};visited = 0
    while stack:
        prefix, oid, depth = stack.pop()
        if depth > budget.limits.max_depth:raise ObjectLimitExceeded('diff tree depth limit exceeded')
        raw = repo.objects.read(oid);budget.charge(len(raw.data))
        if raw.kind is not ObjectType.TREE:raise InvalidObject('diff requires explicit tree objects')
        for entry in Tree.from_bytes(raw.data, repo.object_format).entries:
            visited += 1
            if visited > budget.limits.max_paths:raise ObjectLimitExceeded('diff path limit exceeded')
            path = prefix + entry.name
            if entry.mode == 0o40000:stack.append((path + b'/', entry.oid, depth + 1))
            else:
                data = None
                if entry.mode != 0o160000:
                    blob = repo.objects.read(entry.oid);budget.charge(len(blob.data))
                    if blob.kind is not ObjectType.BLOB:raise InvalidObject('diff file must name a blob')
                    data = blob.data
                files[path] = (entry.mode, entry.oid, data)
    return files


def diff_trees(repo, old, new, *, context=3, detect_renames=False, rename_threshold=50, limits=None):
    """Compare explicit trees (None means empty), without reading a worktree."""
    if type(detect_renames) is not bool or type(rename_threshold) is not int or not 1 <= rename_threshold <= 100:
        raise ValueError('rename detection requires a boolean and a threshold from 1 to 100')
    budget = _Budget(limits or DiffLimits())
    if type(context) is not int or not 0 <= context <= 1_000_000:raise ValueError('invalid diff context')
    before, after = _tree(repo, old, budget), _tree(repo, new, budget)
    deleted = sorted(before.keys() - after.keys());added = sorted(after.keys() - before.keys());renames = []
    if detect_renames:
        if len(deleted) * len(added) > budget.limits.max_rename_pairs:raise ObjectLimitExceeded('rename candidate limit exceeded')
        candidates = []
        for a in deleted:
            for b in added:
                am, ao, ad = before[a];bm, bo, bd = after[b]
                if am & 0o170000 != bm & 0o170000:continue
                if ao == bo:score = 100
                elif ad is None or bd is None or b'\0' in ad or b'\0' in bd:continue
                else:
                    # Byte-weighted LF-line multiset overlap. No decoding, and
                    # a documented deterministic heuristic independent of Git's.
                    budget.compare(len(ad) + len(bd))
                    left, right = Counter(_lines(ad, budget.limits)), Counter(_lines(bd, budget.limits))
                    shared = sum(len(line) * min(n, right[line]) for line, n in left.items())
                    score = 100 * shared // max(len(ad), len(bd), 1)
                if score >= rename_threshold:candidates.append((-score, a, b))
        used_old, used_new = set(), set()
        for negative, a, b in sorted(candidates):
            if a not in used_old and b not in used_new:
                used_old.add(a);used_new.add(b);renames.append((a, b, -negative))
        deleted = [a for a in deleted if a not in used_old];added = [b for b in added if b not in used_new]
    pairs = [(a, a, None) for a in sorted(before.keys() & after.keys()) if before[a][:2] != after[a][:2]]
    pairs += [(a, None, None) for a in deleted] + [(None, b, None) for b in added] + renames
    result = []
    for a, b, score in sorted(pairs, key=lambda p: (p[1] if p[1] is not None else p[0], p[0] or b'')):
        am, ao, ad = before[a] if a is not None else (None, None, None)
        bm, bo, bd = after[b] if b is not None else (None, None, None)
        delta = _diff_bytes(ad or b'', bd or b'', context, budget)
        gitlink = am == 0o160000 or bm == 0o160000
        status = 'R' if score is not None else 'A' if a is None else 'D' if b is None else 'T' if am & 0o170000 != bm & 0o170000 else 'M'
        result.append(FileDiff(status, a, b, am, bm, ao, bo, ad, bd, delta.binary or gitlink, () if gitlink else delta.hunks, score))
    return tuple(result)
