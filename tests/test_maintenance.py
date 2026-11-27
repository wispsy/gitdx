from contextlib import nullcontext
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (Blob, InvalidObject, MissingObject, ObjectFormat, ObjectLimitExceeded, PackWriteLimits,
                   RefConflict, RefLocked, ReflogUpdate, Repository, UnsupportedFormat,
                   apply_maintenance, backup_repository, plan_maintenance, recover_maintenance, verify_backup)
from test_checkout_plan import commit_tree
from test_objects import IDENTITY
import test_objects as oracle


class MaintenanceTests(unittest.TestCase):
    def test_inventory_retention_grace_and_safe_loose_pruning_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                repo = Repository.init(tmp, bare=True, object_format=format)
                first = commit_tree(repo, {b'file': b'old'})
                tip = commit_tree(repo, {b'file': b'current'}, parents=(first,))
                repo.refs.update('refs/heads/main', first, expected=None, reflog=ReflogUpdate(IDENTITY, b'old'))
                repo.refs.update('refs/heads/main', tip, expected=first, reflog=ReflogUpdate(IDENTITY, b'new'))
                orphan = repo.objects.write(Blob(b'unreachable').to_raw())
                young = repo.objects.write(Blob(b'young').to_raw())
                os.utime(repo.objects.object_path(orphan), ns=(1, 1))
                plan = plan_maintenance(repo, grace_seconds=1, now_ns=2_000_000_000)
                self.assertIn(orphan, plan.unreachable)
                self.assertIn(first, plan.reachable)
                self.assertEqual([v[0] for v in plan.prune_candidates], [orphan])
                result = apply_maintenance(repo, plan, prune=True)
                self.assertEqual(result.loose_pruned, 1)
                self.assertFalse(repo.objects.object_path(orphan).exists())
                self.assertTrue(repo.objects.object_path(young).exists())
                self.assertEqual(repo.objects.read(orphan), Blob(b'unreachable').to_raw())
                self.assertEqual(repo.refs.resolve().oid, tip)

    def test_stale_plan_preserves_a_live_writer_and_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp, bare=True);tip = commit_tree(repo, {b'file': b'old'})
            repo.refs.update('refs/heads/main', tip, expected=None)
            plan = plan_maintenance(repo, grace_seconds=0)
            ready = threading.Event();done = threading.Event();failures = []
            def writer():
                ready.wait()
                try:
                    new = commit_tree(repo, {b'file': b'new'}, parents=(tip,))
                    repo.refs.update('refs/heads/main', new, expected=tip)
                    for oid in plan.object_ids: repo.objects.read(oid)
                except BaseException as error: failures.append(error)
                finally: done.set()
            worker = threading.Thread(target=writer);worker.start()
            def check():
                if not ready.is_set():
                    ready.set()
                    if not done.wait(10): raise RuntimeError('writer did not settle')
            try: apply_maintenance(repo, plan, prune=True, check=check)
            finally: ready.set();worker.join(10)
            self.assertEqual(failures, [])
            self.assertNotEqual(repo.refs.resolve().oid, tip)
            self.assertEqual(len(repo.log()), 2)
            for oid in plan.object_ids: repo.objects.read(oid)

    def test_pruning_interruption_restart_changed_copies_and_unknown_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp, bare=True);tip = commit_tree(repo, {b'file': b'data'})
            repo.refs.update('refs/heads/main', tip, expected=None)
            plan = plan_maintenance(repo, grace_seconds=0)
            original = Path.unlink
            def fail(path, *args, **kwargs):
                if path == repo.objects.object_path(tip): raise OSError('interrupted deletion')
                return original(path, *args, **kwargs)
            with patch.object(Path, 'unlink', fail), self.assertRaises(OSError):
                apply_maintenance(repo, plan, prune=True)
            self.assertTrue((repo.git_dir/'maintenance-state.json').exists())
            with self.assertRaises(UnsupportedFormat): apply_maintenance(repo, plan)
            # New identity/mtime belongs to a later writer and is preserved.
            os.utime(repo.objects.object_path(tip), None)
            result = recover_maintenance(Repository.open(tmp))
            self.assertIsNotNone(result)
            self.assertTrue(repo.objects.object_path(tip).exists())
            self.assertIsNone(recover_maintenance(repo))
            lock = repo.git_dir/'maintenance.lock';lock.write_bytes(b'unknown owner')
            with self.assertRaises(RefLocked): apply_maintenance(repo, plan)
            self.assertEqual(lock.read_bytes(), b'unknown owner')

    def test_corrupt_missing_graph_limits_and_tampered_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp, bare=True);tip = commit_tree(repo, {b'file': b'data'})
            repo.refs.update('refs/heads/main', tip, expected=None)
            with self.assertRaises(ObjectLimitExceeded): plan_maintenance(repo, limits=PackWriteLimits(max_objects=1))
            with self.assertRaises(ValueError): plan_maintenance(repo, grace_seconds=-1)
            plan = plan_maintenance(repo, grace_seconds=0)
            with self.assertRaises(RefConflict): apply_maintenance(repo, replace(plan, identity=(-1, -1)))
            repo.objects.object_path(tip).write_bytes(b'broken')
            with self.assertRaises(InvalidObject): plan_maintenance(repo)

    def test_backup_restore_manifest_and_explicit_quiescence(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);repo = Repository.init(root/'source', bare=True, object_format=format)
                old = commit_tree(repo, {b'file': b'old'})
                tip = commit_tree(repo, {b'file': b'new'})
                repo.refs.update('refs/heads/main', old, expected=None, reflog=ReflogUpdate(IDENTITY, b'old'))
                repo.refs.update('refs/heads/main', tip, expected=old, reflog=ReflogUpdate(IDENTITY, b'new'))
                with patch('subprocess.Popen', side_effect=AssertionError('runtime helper')):
                    backup = backup_repository(repo, root/'backup', writer_guard=nullcontext())
                    restored = verify_backup(backup.git_dir)
                    self.assertEqual(restored.refs.snapshot(), repo.refs.snapshot())
                    self.assertEqual(restored.objects.read(old), repo.objects.read(old))
                    apply_maintenance(repo, plan_maintenance(repo, grace_seconds=0), prune=True)
                with self.assertRaises(FileExistsError): backup_repository(repo, root/'backup', writer_guard=nullcontext())
                (backup.git_dir/'refs/heads/main').unlink()
                with self.assertRaises(InvalidObject): verify_backup(backup.git_dir)

    def test_backup_detects_writer_and_does_not_copy_hooks_or_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);repo = Repository.init(root/'source', bare=True)
            tip = commit_tree(repo, {b'file': b'data'});repo.refs.update('refs/heads/main', tip, expected=None)
            (repo.git_dir/'hooks').mkdir();(repo.git_dir/'hooks/post-update').write_bytes(b'never execute')
            backup = backup_repository(repo, root/'backup', writer_guard=nullcontext())
            self.assertFalse((backup.git_dir/'hooks').exists())
            from gitdx import maintenance
            pack = maintenance.write_pack
            def racing(store, roots, **options):
                result = pack(store, roots, **options)
                repo.refs.update('refs/heads/side', tip, expected=None)
                return result
            with patch.object(maintenance, 'write_pack', racing), self.assertRaises(RefConflict):
                backup_repository(repo, root/'racing', writer_guard=nullcontext())
            self.assertFalse((root/'racing').exists())

    def test_real_process_exit_retains_checkpoint_and_requires_owned_lock_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp, bare=True)
            tip = commit_tree(repo, {b'file': b'data'});repo.refs.update('refs/heads/main', tip, expected=None)
            code = '''import os, sys
from gitdx import Repository, plan_maintenance, apply_maintenance
repo = Repository.open(sys.argv[1])
plan = plan_maintenance(repo, grace_seconds=0)
def check():
    if (repo.git_dir/'maintenance-state.json').exists(): os._exit(59)
apply_maintenance(repo, plan, prune=True, check=check)
'''
            result = subprocess.run([sys.executable, '-c', code, tmp], capture_output=True)
            self.assertEqual(result.returncode, 59, result.stderr)
            with self.assertRaises(RefLocked): recover_maintenance(repo)
            lock = repo.git_dir/'maintenance.lock'
            self.assertEqual(lock.read_bytes(), b'')
            lock.unlink()  # This fixture has observed the exact lock owner's exit.
            recover_maintenance(Repository.open(tmp))
            self.assertEqual(repo.refs.resolve().oid, tip)
            self.assertEqual(len(repo.log()), 1)


class MaintenanceNativeTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_gc_input_fsck_and_independent_restore_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);repo = Repository.init(root/'source', bare=True, object_format=format)
                tip = commit_tree(repo, {b'file': b'data'});repo.refs.update('refs/heads/main', tip, expected=None)
                oracle.git(repo.git_dir, 'gc', '--prune=now')
                orphan = repo.objects.write(Blob(b'unreachable').to_raw())
                result = apply_maintenance(repo, plan_maintenance(repo, grace_seconds=0), prune=True)
                oracle.git(repo.git_dir, 'fsck', '--strict', '--no-dangling')
                oracle.git(repo.git_dir, 'verify-pack', '-v', str(repo.objects.path/'pack'/('pack-'+result.pack_checksum+'.idx')))
                backup = backup_repository(repo, root/'backup', writer_guard=nullcontext())
                oracle.git(backup.git_dir, 'fsck', '--strict', '--no-dangling')
                self.assertEqual(oracle.git(backup.git_dir, 'show', 'main:file'), b'data')
                with self.assertRaises(MissingObject): backup.objects.read(orphan)
