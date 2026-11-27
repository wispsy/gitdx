"""Explicit staged commits and bounded, deterministic history queries."""

from dataclasses import dataclass
from itertools import islice
import os
import re

from .errors import InvalidObject, UnsupportedFormat
from .filesystem import check_path
from .object_store import ObjectLimitExceeded
from .objects import Commit, ObjectId, ObjectType, Tree, TreeEntry
from .refs import DirectRef, RefConflict, SymbolicRef


@dataclass(frozen=True, slots=True)
class HistoryLimits:
    max_commits: int = 100_000
    max_bytes: int = 256 * 1024 * 1024
    max_parents: int = 64
    max_paths: int = 100_000
    max_depth: int = 128
    max_refs: int = 10_000

    def __post_init__(self):
        if any(type(getattr(self, name)) is not int or getattr(self, name) <= 0 for name in self.__slots__):
            raise ValueError("history limits must be positive integers")


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    oid: ObjectId
    commit: Commit


def validate_identity(value):
    match = re.fullmatch(rb"([^<>\x00-\x1f\x7f]+) <([^<>\x00-\x20\x7f]+)> (0|[1-9][0-9]{0,18}) ([+-])([0-9]{2})([0-9]{2})", value) if isinstance(value, bytes) else None
    if not match or match[1].strip() != match[1] or int(match[3]) > 0x7fffffffffffffff or int(match[5]) > 23 or int(match[6]) > 59:
        raise InvalidObject("identity requires explicit name, email, Unix timestamp and numeric timezone")


def read_commit(repo, oid):
    if not isinstance(oid, ObjectId) or oid.format != repo.object_format:
        raise InvalidObject("commit ID must use the repository object format")
    raw = repo.objects.read(oid)
    if raw.kind is not ObjectType.COMMIT:raise InvalidObject("history target must be a commit")
    return Commit.from_bytes(raw.data, repo.object_format), len(raw.data)


def walk_history(repo, starts, *, limits=None):
    limits = limits or HistoryLimits()
    stack = []
    for oid in starts:
        if len(stack) >= limits.max_commits:raise ObjectLimitExceeded("too many history roots")
        stack.append(oid)
    stack.reverse();seen = set();total = 0
    while stack:
        oid = stack.pop()
        if oid in seen:continue
        if len(seen) >= limits.max_commits:raise ObjectLimitExceeded("history commit limit exceeded")
        commit, size = read_commit(repo, oid);total += size
        if total > limits.max_bytes or len(commit.parents) > limits.max_parents:raise ObjectLimitExceeded("history byte/parent limit exceeded")
        seen.add(oid)
        yield HistoryEntry(oid, commit)
        stack.extend(reversed(commit.parents))
        if len(stack) > limits.max_commits * limits.max_parents:raise ObjectLimitExceeded("history pending work limit exceeded")


def log(repo, start=None, *, max_count=100, limits=None):
    limits = limits or HistoryLimits()
    if type(max_count) is not int or not 1 <= max_count <= limits.max_commits:raise ValueError("invalid history window")
    start = start if start is not None else repo.refs.resolve().oid
    return tuple(islice(walk_history(repo, () if start is None else (start,), limits=limits), max_count))


def write_tree(repo, index, limits):
    directories = {b"": {}};leaves = set();total = 0
    if len(index.entries) > limits.max_paths:raise ObjectLimitExceeded("staged path limit exceeded")
    for entry in index.entries:
        if entry.stage:raise UnsupportedFormat("cannot commit an unmerged index")
        if entry.mode == 0o160000:raise UnsupportedFormat("submodule commits require a separate profile")
        parts = entry.path.split(b"/")
        if len(parts) > limits.max_depth:raise ObjectLimitExceeded("staged tree depth exceeded")
        for i in range(len(parts)-1):
            parent = b"/".join(parts[:i]);path = b"/".join(parts[:i+1])
            if path in leaves:raise InvalidObject("staged file/directory collision")
            directories.setdefault(path, {})
            directories[parent][parts[i]] = path
            if len(directories) > limits.max_paths:raise ObjectLimitExceeded("staged directory limit exceeded")
        if entry.path in directories:raise InvalidObject("staged file/directory collision")
        raw = repo.objects.read(entry.oid);total += len(raw.data)
        if raw.kind is not ObjectType.BLOB:raise InvalidObject("staged file must name a blob")
        if total > limits.max_bytes:raise ObjectLimitExceeded("staged object byte limit exceeded")
        directories[b"/".join(parts[:-1])][parts[-1]] = entry
        leaves.add(entry.path)
    written = {}
    for path in sorted(directories, key=lambda p: (p.count(b"/")+bool(p), p), reverse=True):
        entries = []
        for name, value in directories[path].items():
            entries.append(TreeEntry(0o40000, name, written[value]) if isinstance(value, bytes) else TreeEntry(value.mode, name, value.oid))
        tree = Tree(tuple(sorted(entries, key=lambda e: e.sort_key)), repo.object_format)
        raw = tree.to_raw();total += len(raw.data)
        if total > limits.max_bytes:raise ObjectLimitExceeded("staged tree byte limit exceeded")
        written[path] = repo.objects.write(raw)
    return written[b""]


def create_commit(repo, *, author, committer, message, expected_head, parents=None, limits=None):
    from .ref_transaction import require_no_ref_transaction
    require_no_ref_transaction(repo.git_dir)
    for name in ("checkout-state", "operation-state", "merge-state", "MERGE_HEAD"):
        pending = repo.git_dir/name;check_path(repo.git_dir, pending)
        if pending.exists():raise UnsupportedFormat("finish or recover the active worktree operation before committing")
    limits = limits or HistoryLimits()
    validate_identity(author);validate_identity(committer)
    if not isinstance(message, bytes):raise TypeError("commit message must be bytes")
    if len(message) > limits.max_bytes:raise ObjectLimitExceeded("commit message byte limit exceeded")
    refs = repo.refs;head = refs.read("HEAD");resolved = refs.resolve()
    if resolved.oid != expected_head:raise RefConflict("HEAD changed since its expected ID")
    if isinstance(head, SymbolicRef) and (resolved.symbolic_chain != ("HEAD",) or not resolved.name.startswith("refs/heads/")):
        raise UnsupportedFormat("commits require detached HEAD or a direct branch symbolic HEAD")
    if isinstance(head, SymbolicRef) and resolved.name != head.target:
        raise RefConflict("HEAD changed while capturing its branch target")
    if head is None:raise InvalidObject("HEAD must exist")
    selected = (expected_head,) if parents is None and expected_head is not None else () if parents is None else tuple(islice(parents, limits.max_parents+1))
    if len(selected) > limits.max_parents or len(set(selected)) != len(selected):raise InvalidObject("invalid or excessive commit parents")
    if expected_head is not None and (not selected or selected[0] != expected_head):raise InvalidObject("the first parent must be the expected HEAD")
    parent_bytes = 0
    for oid in selected:
        _, size = read_commit(repo, oid);parent_bytes += size
        if parent_bytes > limits.max_bytes:raise ObjectLimitExceeded("parent commit byte limit exceeded")
    with repo.index.transaction() as transaction:
        tree = write_tree(repo, transaction.index, limits)
        oid = repo.objects.write(Commit.create(tree, author=author, committer=committer, message=message, parents=selected).to_raw())
        if isinstance(head, SymbolicRef):
            # Guard HEAD's identity as well as the branch tip. All locks are
            # exclusive/nonblocking; a concurrent checkout cannot redirect us.
            with refs._lock(refs.path/"HEAD"):
                if refs.read("HEAD") != head:raise RefConflict("symbolic HEAD changed during commit")
                refs.update(resolved.name, oid, expected=expected_head)
        else:
            refs.detach_head(oid, expected=DirectRef(expected_head))
        return oid


def list_refs(repo, prefix, *, limits=None, check=None):
    if prefix not in ("refs/heads/", "refs/tags/"):raise ValueError("select branches or tags explicitly")
    limits = limits or HistoryLimits();refs = repo.refs
    values = refs.snapshot(prefix,max_refs=limits.max_refs,max_depth=limits.max_depth,check=check)
    result=[]
    for name,value in values:
        if check:check()
        result.append((name,value.oid if isinstance(value,DirectRef) else refs.resolve(name).oid))
    return tuple(result)
