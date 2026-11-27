"""An explicit revision subset with bounded object and abbreviation lookup."""

from dataclasses import dataclass
import os
import re

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .filesystem import check_path, read_file, storage_exists
from .names import validate_refname
from .object_store import ObjectLimitExceeded
from .objects import Commit, ObjectId, ObjectType, Tag
from .pack import InvalidPack, PackIndex


class RevisionNotFound(GitdxError, LookupError):
    """The requested ref, object prefix or parent does not exist."""


class AmbiguousRevision(GitdxError, ValueError):
    """A short name has more than one ref/object interpretation."""


class UnsupportedRevision(UnsupportedFormat):
    """A revision uses syntax outside the documented supported subset."""


@dataclass(frozen=True, slots=True)
class RevisionLimits:
    max_syntax_bytes: int = 4096
    max_objects: int = 100_000
    max_bytes: int = 256 * 1024 * 1024
    max_steps: int = 100_000
    max_parents: int = 64
    max_tag_depth: int = 128
    max_scan_entries: int = 200_000

    def __post_init__(self):
        if any(type(getattr(self, k)) is not int or getattr(self, k) <= 0 for k in self.__slots__):
            raise ValueError('revision limits must be positive integers')


class _Context:
    def __init__(self, repo, limits):
        self.repo, self.limits = repo, limits
        self.cache = {};self.bytes = 0;self.steps = 0;self.scanned = 0

    def step(self, count=1):
        self.steps += count
        if self.steps > self.limits.max_steps:raise ObjectLimitExceeded('revision traversal step limit exceeded')

    def charge(self, size):
        self.bytes += size
        if self.bytes > self.limits.max_bytes:raise ObjectLimitExceeded('revision byte limit exceeded')

    def scan(self):
        self.scanned += 1
        if self.scanned > self.limits.max_scan_entries:raise ObjectLimitExceeded('revision object scan limit exceeded')

    def read(self, oid):
        if not isinstance(oid, ObjectId) or oid.format != self.repo.object_format:raise InvalidObject('revision object format mismatch')
        if oid not in self.cache:
            if len(self.cache) >= self.limits.max_objects:raise ObjectLimitExceeded('revision object limit exceeded')
            raw = self.repo.objects.read(oid);self.charge(len(raw.data));self.cache[oid] = raw
        return self.cache[oid]

    def peel(self, oid, required=None):
        seen = set();depth = 0
        while True:
            self.step();raw = self.read(oid)
            if required == 'object':return oid
            if raw.kind is ObjectType.TAG:
                tag = Tag.from_bytes(raw.data, self.repo.object_format)
                if required is ObjectType.TAG:return oid
                if oid in seen:raise InvalidObject('tag cycle in revision')
                seen.add(oid);depth += 1
                if depth > self.limits.max_tag_depth:raise ObjectLimitExceeded('revision tag depth exceeded')
                target = self.read(tag.target)
                declared = next(h.value for h in tag.headers if h.name == b'type')
                if target.kind.value.encode() != declared:raise InvalidObject('tag target type does not match its declaration')
                oid = tag.target
            elif required is None or raw.kind is required:return oid
            elif raw.kind is ObjectType.COMMIT and required is ObjectType.TREE:
                commit = Commit.from_bytes(raw.data, self.repo.object_format)
                if len(commit.parents) > self.limits.max_parents:raise ObjectLimitExceeded('revision parent limit exceeded')
                oid = commit.tree
            else:raise InvalidObject('revision cannot be peeled to the requested object type')

    def commit(self, oid, *, peel=True):
        if peel:oid = self.peel(oid, ObjectType.COMMIT)
        raw = self.read(oid)
        if raw.kind is not ObjectType.COMMIT:raise InvalidObject('commit parent must name a commit object')
        commit = Commit.from_bytes(raw.data, self.repo.object_format)
        if len(commit.parents) > self.limits.max_parents:raise ObjectLimitExceeded('revision parent limit exceeded')
        return oid, commit

    def prefix(self, prefix):
        store = self.repo.objects;matches = set();directory = store.path / prefix[:2]
        check_path(store.path, directory)
        if directory.exists():
            with os.scandir(directory) as children:
                for child in children:
                    self.scan();name = prefix[:2] + child.name
                    if re.fullmatch('[0-9a-f]{%d}' % store.format.hex_size, name) and name.startswith(prefix):
                        check_path(store.path, directory / child.name)
                        if child.is_file(follow_symlinks=False):matches.add(ObjectId(name, store.format))
        packs = store.path / 'pack';check_path(store.path, packs);paths = []
        if packs.exists():
            with os.scandir(packs) as children:
                for child in children:
                    self.scan()
                    if child.name.startswith('pack-') and child.name.endswith('.idx'):
                        if not re.fullmatch('pack-[0-9a-f]{%d}\\.idx' % store.format.hex_size, child.name):raise InvalidPack('malformed pack-index filename')
                        paths.append(packs / child.name)
                        if len(paths) > store.pack_limits.max_packs:raise ObjectLimitExceeded('revision pack count exceeded')
        for path in sorted(paths):
            allowance = min(store.pack_limits.max_index_bytes, self.limits.max_bytes - self.bytes)
            data = read_file(store.path, path, allowance, limit_error=ObjectLimitExceeded);self.charge(len(data))
            index = PackIndex.from_bytes(data, store.format, limits=store.pack_limits)
            if path.stem[5:] != index.pack_checksum.hex():raise InvalidPack('pack filename does not match its checksum')
            for entry in index.entries:
                self.scan()
                if entry.oid.hex.startswith(prefix):matches.add(entry.oid)
        if len(matches) > 1:raise AmbiguousRevision('object abbreviation is ambiguous')
        return next(iter(matches)) if matches else None

    def base(self, name):
        if name == 'HEAD':
            oid = self.repo.refs.resolve().oid
            if oid is None:raise RevisionNotFound('HEAD is unborn or absent')
            return oid
        width = self.repo.object_format.hex_size
        if re.fullmatch('[0-9a-fA-F]{%d}' % width, name):return ObjectId(name.lower(), self.repo.object_format)
        if name.startswith('refs/'):
            validate_refname(name,portable=self.repo.config.ref_storage=='files');oid = self.repo.refs.resolve(name).oid
            if oid is None:raise RevisionNotFound('fully qualified ref is absent')
            return oid
        names = ['refs/' + name, 'refs/tags/' + name, 'refs/heads/' + name,
                 'refs/remotes/' + name, 'refs/remotes/' + name + '/HEAD']
        refs = []
        for candidate in names:
            validate_refname(candidate,portable=self.repo.config.ref_storage=='files');oid = self.repo.refs.resolve(candidate).oid
            if oid is not None:refs.append(oid)
        if len(refs) > 1:raise AmbiguousRevision('short ref name is ambiguous; select a fully qualified ref')
        abbreviated = self.prefix(name.lower()) if re.fullmatch('[0-9a-fA-F]{4,%d}' % (width - 1), name) else None
        if refs and abbreviated is not None:raise AmbiguousRevision('revision matches both a ref and an object abbreviation')
        if refs:return refs[0]
        if abbreviated is not None:return abbreviated
        raise RevisionNotFound('revision name or object abbreviation is absent')


_SUFFIX = re.compile(r'\^(?:\{(?:object|commit|tree|blob|tag)?\}|[0-9]*)|~[0-9]*')


def _syntax(value, limits):
    if not isinstance(value, str):raise TypeError('revision must be a string or ObjectId')
    if len(value.encode('utf-8')) > limits.max_syntax_bytes:raise ObjectLimitExceeded('revision syntax byte limit exceeded')
    if not value or any(ord(c)<=32 or ord(c)==127 for c in value) or any(s in value for s in (':', '@{', '..', '\\')) or value.startswith(('-', '@')):
        raise UnsupportedRevision('unsupported revision syntax')
    match = re.match(r'[^~^]+', value)
    if match is None:raise UnsupportedRevision('revision requires an explicit base')
    base = match[0];position = match.end();suffixes = []
    if any(c in base for c in '{}?*['):raise UnsupportedRevision('unsupported revision base')
    while position < len(value):
        suffix = _SUFFIX.match(value, position)
        if suffix is None:raise UnsupportedRevision('unsupported revision suffix')
        token = suffix[0];position = suffix.end()
        if not token.startswith('^{'):
            number = token[1:]
            if len(number) > 9:raise ObjectLimitExceeded('revision parent count exceeds its limit')
            n = int(number) if number else 1
            if n > limits.max_steps:raise ObjectLimitExceeded('revision parent count exceeds its limit')
        suffixes.append(token)
    return base, suffixes


def _resolve(context, revision):
    if isinstance(revision, ObjectId):
        context.read(revision);return revision
    base, suffixes = _syntax(revision, context.limits)
    oid = context.base(base);context.read(oid)
    for suffix in suffixes:
        context.step()
        if suffix.startswith('^{'):
            kind = suffix[2:-1]
            oid = context.peel(oid, 'object' if kind == 'object' else ObjectType(kind) if kind else None)
        else:
            oid, commit = context.commit(oid);count = int(suffix[1:]) if suffix[1:] else 1
            if suffix[0] == '^':
                if count:
                    if count > len(commit.parents):raise RevisionNotFound('requested parent is absent')
                    oid, _ = context.commit(commit.parents[count - 1], peel=False)
            else:
                for _ in range(count):
                    context.step()
                    if not commit.parents:raise RevisionNotFound('requested first-parent ancestor is absent')
                    oid, commit = context.commit(commit.parents[0], peel=False)
    return oid


def resolve_revision(repo, revision, *, limits=None):
    """Resolve exactly the documented syntax, with no helpers or implicit fetch."""
    # Repeat required-profile checks even for an already-open repository.
    check_history_profile(repo)
    return _resolve(_Context(repo, limits or RevisionLimits()), revision)


def check_history_profile(repo):
    replacement = repo.refs.snapshot('refs/replace/')
    for relative in ('info/grafts', 'refs/replace'):
        path = repo.git_dir / relative;check_path(repo.git_dir, path)
        if storage_exists(path):raise UnsupportedFormat('replacement and graft history is unsupported')
    if replacement:raise UnsupportedFormat('replacement history is unsupported')
