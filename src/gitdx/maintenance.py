"""Bounded bare-store inspection, retain-all repacking and restartable pruning."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from .errors import InvalidObject, UnsupportedFormat
from .filesystem import check_path, read_file, replace_file
from .object_store import ObjectLimitExceeded
from .objects import ObjectId
from .pack import PackFile, PackIndex
from .pack_writer import PackWriteLimits, reachable_objects, write_pack
from .refs import DirectRef, RefConflict, RefLocked, SymbolicRef


@dataclass(frozen=True, slots=True)
class MaintenancePlan:
    identity: tuple[int, int]
    object_ids: tuple[ObjectId, ...]
    reachable: tuple[ObjectId, ...]
    # oid, device, inode, size, mtime_ns; all candidates also enter the new pack.
    prune_candidates: tuple[tuple, ...]
    cutoff_ns: int

    @property
    def unreachable(self):
        retained = set(self.reachable)
        return tuple(oid for oid in self.object_ids if oid not in retained)


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    pack_checksum: str
    loose_pruned: int


def _identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


def _bare(repo):
    from .repository import Repository
    current = Repository.open(repo.git_dir)
    if not current.bare or current.config.ref_storage != 'files':
        raise UnsupportedFormat('maintenance selects bare files-ref storage only')
    if current.object_format != repo.object_format or current.storage_identity != repo.storage_identity:
        raise UnsupportedFormat('maintenance storage identity or format changed')
    for name in ('ref-transaction-state', 'checkout-state', 'operation-state', 'merge-state'):
        path = repo.git_dir/name
        check_path(repo.git_dir, path)
        if path.exists():
            raise UnsupportedFormat('recover the pending repository operation before maintenance')


def _files(root, directory, maximum, check):
    pending = [(directory, 0)];count = 0
    while pending:
        parent, depth = pending.pop();check_path(root, parent)
        if not parent.exists():
            continue
        if depth > 64:
            raise ObjectLimitExceeded('maintenance directory depth exceeded')
        with os.scandir(parent) as entries:
            for entry in entries:
                check();count += 1
                if count > maximum:
                    raise ObjectLimitExceeded('maintenance file scan exceeded')
                path = Path(entry.path);check_path(root, path)
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    pending.append((path, depth+1))
                elif stat.S_ISREG(info.st_mode):
                    yield path
                else:
                    raise UnsupportedFormat('maintenance refuses special storage files')


def _snapshot(repo, limits, check):
    refs = (('HEAD', repo.refs.read('HEAD')), *repo.refs.snapshot(max_refs=limits.max_objects, check=check))
    roots = [value.oid for _, value in refs if isinstance(value, DirectRef)]
    logs = [];total = 0
    for path in _files(repo.git_dir, repo.git_dir/'logs', limits.max_objects, check):
        if path.name.endswith('.lock'):
            raise RefLocked('reflog writer is active')
        data = read_file(repo.git_dir, path, min(8*1024*1024, limits.max_bytes-total), limit_error=ObjectLimitExceeded)
        total += len(data)
        for line in data.splitlines():
            check();parts = line.split(b' ', 2)
            if len(parts) != 3:
                raise InvalidObject('malformed retained reflog')
            for value in parts[:2]:
                oid = ObjectId(value.decode('ascii'), repo.object_format)
                if any(oid.bytes): roots.append(oid)
            if len(roots) > limits.max_objects:
                raise ObjectLimitExceeded('maintenance retention root count exceeded')
        logs.append((path.relative_to(repo.git_dir).as_posix(), data))
    return refs, tuple(logs), tuple(roots)


def plan_maintenance(repo, *, grace_seconds=14*24*3600, now_ns=None, limits=None, check=None):
    """Inspect all inventoried objects; retain refs and every extant reflog root.

    Pruning removes only old loose duplicates after retaining *every* planned
    object in an immutable pack. Unreachable expiry and old-pack deletion are
    deliberately absent from this online profile.
    """
    limits = limits or PackWriteLimits();check = check or (lambda: None)
    if type(grace_seconds) is not int or grace_seconds < 0:
        raise ValueError('select a nonnegative explicit grace period')
    if now_ns is not None and (type(now_ns) is not int or now_ns < 0):
        raise ValueError('invalid maintenance clock')
    _bare(repo)
    cutoff = (time.time_ns() if now_ns is None else now_ns)-grace_seconds*1_000_000_000
    refs, logs, roots = _snapshot(repo, limits, check)
    reachable = reachable_objects(repo.objects, roots, limits=limits, check=check)
    ids = {oid for oid, _ in reachable};loose = [];scanned = 0
    for path in _files(repo.git_dir, repo.objects.path, 2*limits.max_objects+1024, check):
        relative = path.relative_to(repo.objects.path).as_posix()
        if re.fullmatch(r'[0-9a-f]{2}/[0-9a-f]{'+str(repo.object_format.hex_size-2)+r'}', relative):
            oid = ObjectId(relative.replace('/', ''), repo.object_format)
            info = path.stat();ids.add(oid)
            if info.st_mtime_ns <= cutoff:
                loose.append((oid, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        elif relative.startswith('pack/') and path.suffix == '.idx':
            raw = read_file(repo.git_dir, path, min(repo.objects.pack_limits.max_index_bytes, limits.max_bytes-scanned), limit_error=ObjectLimitExceeded)
            scanned += len(raw)
            index = PackIndex.from_bytes(raw, repo.object_format, limits=repo.objects.pack_limits)
            if path.name != 'pack-'+index.pack_checksum.hex()+'.idx':
                raise InvalidObject('maintenance pack name differs from checksum')
            data = read_file(repo.git_dir, path.with_suffix('.pack'), min(repo.objects.pack_limits.max_pack_bytes, limits.max_bytes-scanned), limit_error=ObjectLimitExceeded)
            scanned += len(data);PackFile(data, index, limits=repo.objects.pack_limits)
            ids.update(e.oid for e in index.entries)
        if len(ids) > limits.max_objects:
            raise ObjectLimitExceeded('maintenance inventory count exceeded')
    # Validate unreachable graphs too, including types and dependencies.
    objects = reachable_objects(repo.objects, ids, limits=limits, check=check)
    _bare(repo)
    return MaintenancePlan(_identity(repo.git_dir), tuple(oid for oid, _ in objects),
                           tuple(oid for oid, _ in reachable), tuple(loose), cutoff)


@contextmanager
def _lock(repo):
    path = repo.git_dir/'maintenance.lock';check_path(repo.git_dir, path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RefLocked('maintenance lock belongs to another operation') from error
    os.close(fd)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _publish(repo, bundle):
    # Readers discover indexes, so publish the complete pack before its index.
    root = repo.objects.path/'pack';check_path(repo.git_dir, root)
    for extension, data in (('.pack', bundle.pack), ('.idx', bundle.index)):
        target = root/('pack-'+bundle.checksum.hex()+extension)
        check_path(repo.git_dir, target)
        if target.exists():
            if read_file(repo.git_dir, target, len(data)) != data:
                raise InvalidObject('existing maintenance pack conflicts')
        else:
            # The filename is content-addressed; concurrent correct publishers
            # select the same complete bytes. No ref points at temporary data.
            replace_file(repo.git_dir, target, data)


def _encoded(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii')
    return json.dumps({'state': value, 'sha256': hashlib.sha256(raw).hexdigest()}, sort_keys=True).encode('ascii')


def _prune(repo, state, *, limits, check):
    if state['identity'] != list(_identity(repo.git_dir)):
        raise UnsupportedFormat('maintenance checkpoint belongs to another repository')
    checksum = state['pack']
    if not re.fullmatch('[0-9a-f]{'+str(repo.object_format.hex_size)+'}', checksum):
        raise InvalidObject('invalid maintenance pack checksum')
    root = repo.objects.path/'pack'/('pack-'+checksum)
    index = PackIndex.from_bytes(read_file(repo.git_dir, root.with_suffix('.idx'), limits.max_index_bytes), repo.object_format)
    pack = PackFile(read_file(repo.git_dir, root.with_suffix('.pack'), limits.max_pack_bytes), index)
    if index.pack_checksum.hex() != checksum or len(state['candidates']) > limits.max_objects:
        raise InvalidObject('maintenance checkpoint exceeds its selected pack')
    removed = 0
    for value in state['candidates']:
        check()
        if len(value) != 5 or any(type(v) is not int for v in value[1:]):
            raise InvalidObject('invalid maintenance candidate identity')
        oid = ObjectId(value[0], repo.object_format)
        # Verify the retained object even if a previous attempt removed loose.
        if pack.read(oid).oid(repo.object_format) != oid:
            raise InvalidObject('pruning would remove an unretained object')
        path = repo.objects.object_path(oid)
        try: info = path.stat()
        except FileNotFoundError: continue
        if [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns] != value[1:]:
            continue  # A later writer's copy is never this plan's property.
        path.unlink();removed += 1
    check()
    (repo.git_dir/'maintenance-state.json').unlink()
    return MaintenanceResult(checksum, removed)


def apply_maintenance(repo, plan, *, prune=False, limits=None, check=None):
    limits = limits or PackWriteLimits();check = check or (lambda: None)
    if not isinstance(plan, MaintenancePlan): raise TypeError('select a MaintenancePlan')
    _bare(repo)
    with _lock(repo):
        state_path = repo.git_dir/'maintenance-state.json';check_path(repo.git_dir, state_path)
        if state_path.exists(): raise UnsupportedFormat('resume the pending maintenance checkpoint first')
        if _identity(repo.git_dir) != plan.identity: raise RefConflict('maintenance plan belongs to another store')
        bundle = write_pack(repo.objects, plan.object_ids, limits=limits, check=check)
        # Candidate IDs must all be retained; do not trust a caller-modified plan.
        if any(value[0] not in bundle.object_ids for value in plan.prune_candidates):
            raise InvalidObject('maintenance candidates are outside the retained pack')
        _publish(repo, bundle)
        if not prune: return MaintenanceResult(bundle.checksum.hex(), 0)
        candidates = []
        for oid, dev, ino, size, mtime in plan.prune_candidates:
            if mtime > plan.cutoff_ns: raise InvalidObject('pruning violates the selected grace period')
            candidates.append([oid.hex, dev, ino, size, mtime])
        state = {'version': 1, 'identity': list(plan.identity), 'pack': bundle.checksum.hex(), 'candidates': candidates}
        replace_file(repo.git_dir, state_path, _encoded(state))
        return _prune(repo, state, limits=limits, check=check)


def recover_maintenance(repo, *, limits=None, check=None):
    limits = limits or PackWriteLimits();check = check or (lambda: None)
    _bare(repo)
    with _lock(repo):
        path = repo.git_dir/'maintenance-state.json'
        try: envelope = json.loads(read_file(repo.git_dir, path, min(limits.max_bytes, 48*1024*1024)))
        except FileNotFoundError: return None
        state = envelope['state']
        if state.get('version') != 1 or _encoded(state) != json.dumps(envelope, sort_keys=True).encode('ascii'):
            raise InvalidObject('maintenance checkpoint checksum or version mismatch')
        return _prune(repo, state, limits=limits, check=check)


def backup_repository(repo, target, *, writer_guard, limits=None, check=None):
    """Create a fresh independently restorable bare snapshot under caller quiescence.

    The context manager must exclude ALL ref/reflog writers, including native
    Git. This cannot be inferred from native lockfiles. No config, credentials,
    hooks, worktree or platform catalog is copied.
    """
    from .repository import Repository
    limits = limits or PackWriteLimits();check = check or (lambda: None)
    _bare(repo);target = Path(target).absolute()
    if target == repo.git_dir or repo.git_dir in target.parents or target in repo.git_dir.parents:
        raise ValueError('backup requires a separate destination')
    check_path(target, target)
    with writer_guard:
        before = _snapshot(repo, limits, check)
        bundle = write_pack(repo.objects, before[2], limits=limits, check=check)
        if _snapshot(repo, limits, check) != before:
            raise RefConflict('repository changed while taking a backup')
        target.mkdir()  # Exclusive; existing backups are never overwritten.
        destination = Repository.init(target, bare=True, object_format=repo.object_format)
        _publish(destination, bundle)
        for name, value in before[0]:
            check()
            if value is None: continue
            data = (b'ref: '+value.target.encode('utf-8') if isinstance(value, SymbolicRef)
                    else value.oid.hex.encode('ascii'))+b'\n'
            path = target/name;check_path(target, path);path.parent.mkdir(parents=True, exist_ok=True)
            replace_file(target, path, data)
        for name, data in before[1]:
            path = target/name;check_path(target, path);path.parent.mkdir(parents=True, exist_ok=True)
            replace_file(target, path, data)
        check()
        if _snapshot(repo, limits, check) != before:
            raise RefConflict('repository changed before backup completion')
        # Only completed snapshots get the receipt. An interruption leaves an
        # incomplete, unadvertised destination for explicit owner inspection.
        manifest = {'version': 1, 'format': repo.object_format.value, 'pack': bundle.checksum.hex(),
                    'refs': [(n, v.target if isinstance(v, SymbolicRef) else v.oid.hex if v else None,
                              isinstance(v, SymbolicRef)) for n, v in before[0]],
                    'logs': [(n, hashlib.sha256(data).hexdigest()) for n, data in before[1]]}
        replace_file(target, target/'backup.json', _encoded(manifest))
        verify_backup(target, limits=limits, check=check)
        return destination


def verify_backup(path, *, limits=None, check=None):
    from .repository import Repository
    limits = limits or PackWriteLimits();check = check or (lambda: None)
    repo = Repository.open(path);_bare(repo)
    envelope = json.loads(read_file(repo.git_dir, repo.git_dir/'backup.json', 16*1024*1024))
    value = envelope['state']
    if value.get('version') != 1 or _encoded(value) != json.dumps(envelope, sort_keys=True).encode('ascii'):
        raise InvalidObject('backup receipt checksum or version mismatch')
    refs, logs, roots = _snapshot(repo, limits, check)
    expected = [(n, v.target if isinstance(v, SymbolicRef) else v.oid.hex if v else None, isinstance(v, SymbolicRef)) for n, v in refs]
    if value['format'] != repo.object_format.value or value['refs'] != [list(v) for v in expected] or value['logs'] != [[n, hashlib.sha256(d).hexdigest()] for n, d in logs]:
        raise InvalidObject('backup metadata differs from its receipt')
    checksum = value['pack']
    if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{'+str(repo.object_format.hex_size)+'}', checksum):
        raise InvalidObject('invalid backup pack checksum')
    stem = repo.objects.path/'pack'/('pack-'+checksum)
    index = PackIndex.from_bytes(read_file(repo.git_dir, stem.with_suffix('.idx'), limits.max_index_bytes), repo.object_format)
    pack = PackFile(read_file(repo.git_dir, stem.with_suffix('.pack'), limits.max_pack_bytes), index)
    closure = reachable_objects(repo.objects, roots, limits=limits, check=check)
    if index.pack_checksum.hex() != checksum or {e.oid for e in index.entries} != {oid for oid, _ in closure}:
        raise InvalidObject('backup object closure differs from its receipt')
    for oid, raw in closure:
        check()
        if pack.read(oid) != raw: raise InvalidObject('backup pack object differs from its closure')
    return repo
