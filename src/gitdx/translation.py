"""Explicit, isolated translation of ordinary unsigned native object graphs."""

from dataclasses import dataclass
import json
from itertools import islice
import sqlite3

from .errors import InvalidObject, UnsupportedFormat
from .filesystem import check_path
from .objects import Commit, Header, ObjectId, ObjectType, Tag, Tree, TreeEntry, parse_object
from .pack_writer import PackWriteLimits, reachable_objects


@dataclass(frozen=True, slots=True)
class TranslationResult:
    roots: tuple[ObjectId, ...]
    mappings: tuple[tuple[ObjectId, ObjectId], ...]


class GraphTranslator:
    """A restartable mapping cache; objects remain authoritative in both stores.

    The caller selects an existing, empty-of-refs bare target. Translation never
    changes refs or installs a migration/transfer policy. Storage is trusted and
    owner-controlled, under the ordinary local filesystem contract.
    """

    def __init__(self, source, target, *, limits=None):
        self.source = source
        self.target = target
        self.limits = limits or PackWriteLimits()
        a, b = source.git_dir.resolve(), target.git_dir.resolve()
        if a == b or a in b.parents or b in a.parents:
            raise ValueError("translation requires separate, non-nested stores")
        if source.object_format == target.object_format:
            raise ValueError("select different source and target object formats")
        if not target.bare or target.config.ref_storage != 'files':
            raise UnsupportedFormat("translation requires an isolated files-ref bare target")
        self.path = target.git_dir / 'translation.sqlite3'
        self._isolated()

    def _isolated(self):
        if self.target.refs.snapshot() or not self.target.refs.resolve().unborn:
            raise UnsupportedFormat("translation target must have no published refs")
        for repo in (self.source, self.target):
            check_path(repo.git_dir, repo.git_dir)
            info = repo.git_dir.stat()
            if repo.storage_identity is not None and (info.st_dev, info.st_ino) != repo.storage_identity:
                raise UnsupportedFormat("translation storage identity changed")

    def _profile(self):
        result = {'version': 1}
        for name, repo in (('source', self.source), ('target', self.target)):
            info = repo.git_dir.stat()
            result[name] = [str(repo.git_dir.resolve()), info.st_dev, info.st_ino, repo.object_format.value]
        return json.dumps(result, sort_keys=True)

    def translate(self, roots, *, check=None):
        check = check or (lambda: None)
        self._isolated()
        # Materialize and validate the complete bounded source closure before
        # opening the cache or writing any translated object.
        roots = tuple(islice(roots, self.limits.max_objects + 1))
        if len(roots) > self.limits.max_objects:
            from .object_store import ObjectLimitExceeded
            raise ObjectLimitExceeded('translation root limit exceeded')
        objects = dict(reachable_objects(self.source.objects, roots, limits=self.limits, check=check))
        parsed = {}
        dependencies = {}
        signatures = (b'-----BEGIN PGP SIGNATURE-----', b'-----BEGIN SSH SIGNATURE-----',
                      b'-----BEGIN SIGNED MESSAGE-----')
        for oid, raw in objects.items():
            check()
            obj = parse_object(raw, self.source.object_format)
            parsed[oid] = obj
            if isinstance(obj, Tree):
                if any(e.mode == 0o160000 for e in obj.entries):
                    raise UnsupportedFormat('cross-format gitlinks require an explicit external mapping policy')
                dependencies[oid] = tuple(e.oid for e in obj.entries)
            elif isinstance(obj, (Commit, Tag)):
                if any(h.name in (b'gpgsig', b'gpgsig-sha256', b'mergetag') for h in obj.headers):
                    raise UnsupportedFormat('signed objects and mergetags cannot be translated by this profile')
                if any(marker in obj.message for marker in signatures):
                    raise UnsupportedFormat('signature-bearing messages require an explicit policy')
                dependencies[oid] = (obj.tree, *obj.parents) if isinstance(obj, Commit) else (obj.target,)
            else:
                dependencies[oid] = ()
        # Iterative postorder avoids Python's recursion limit on deep histories.
        order, seen, visiting = [], set(), set()
        for root in roots:
            pending = [(root, False)]
            while pending:
                check()
                oid, leaving = pending.pop()
                if oid in seen:
                    continue
                if leaving:
                    visiting.remove(oid)
                    seen.add(oid)
                    order.append(oid)
                else:
                    if oid in visiting:
                        raise InvalidObject('translation dependency cycle')
                    visiting.add(oid)
                    pending.append((oid, True))
                    pending.extend((dep, False) for dep in reversed(dependencies[oid]))
        check_path(self.target.git_dir, self.path)
        check_path(self.target.git_dir, self.path.with_name(self.path.name + '-journal'))
        connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            connection.execute('BEGIN IMMEDIATE')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise UnsupportedFormat('unrecognized translation database')
                connection.execute('CREATE TABLE profile (value TEXT NOT NULL)')
                connection.execute('CREATE TABLE mappings (source TEXT PRIMARY KEY, target TEXT NOT NULL) WITHOUT ROWID')
                connection.execute('INSERT INTO profile VALUES (?)', (self._profile(),))
                connection.execute('PRAGMA user_version=1')
            elif version != 1:
                raise UnsupportedFormat('unsupported translation mapping version')
            if connection.execute('SELECT value FROM profile').fetchall() != [(self._profile(),)]:
                raise UnsupportedFormat('translation mapping belongs to another store or format')
            mapped = {}
            output_bytes = 0
            for oid in order:
                check()
                obj = parsed[oid]
                if isinstance(obj, Tree):
                    raw = Tree(tuple(TreeEntry(e.mode, e.name, mapped[e.oid]) for e in obj.entries),
                               self.target.object_format).to_raw()
                elif isinstance(obj, (Commit, Tag)):
                    names = (b'tree', b'parent') if isinstance(obj, Commit) else (b'object',)
                    headers = tuple(Header(h.name, str(mapped[ObjectId(h.value.decode('ascii'), self.source.object_format)]).encode('ascii'))
                                    if h.name in names else h for h in obj.headers)
                    raw = type(obj)(headers, obj.message, self.target.object_format).to_raw()
                else:
                    raw = objects[oid]
                output_bytes += len(raw.data)
                if len(raw.data) > self.limits.max_object_bytes or output_bytes > self.limits.max_bytes:
                    from .object_store import ObjectLimitExceeded
                    raise ObjectLimitExceeded('translated object byte limit exceeded')
                expected = raw.oid(self.target.object_format)
                cached = connection.execute('SELECT target FROM mappings WHERE source=?', (oid.hex,)).fetchone()
                if cached is not None and cached != (expected.hex,):
                    raise InvalidObject('cached translation conflicts with authoritative source objects')
                # Re-derive every mapping, verify existing bytes and repair an
                # absent object; a database row never substitutes for an object.
                actual = self.target.objects.write(raw)
                if actual != expected or self.target.objects.read(actual) != raw:
                    raise InvalidObject('translated object verification failed')
                mapped[oid] = actual
                connection.execute('INSERT OR IGNORE INTO mappings VALUES (?, ?)', (oid.hex, actual.hex))
            check()
            self._isolated()
            connection.execute('COMMIT')
            return TranslationResult(tuple(mapped[r] for r in roots), tuple(sorted(mapped.items(), key=lambda p: p[0].hex)))
        except BaseException:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            raise
        finally:
            connection.close()
