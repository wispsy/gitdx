from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (Blob, DirectRef, InvalidObject, ObjectFormat, ObjectLimitExceeded, RefConflict, RefLocked,
                   RefTransactionState, ReftableChange, ReftableWriteInterrupted, Repository, SymbolicRef)
from gitdx.reftable import Reftable, ReftableLimits, ReftableRefStore
from gitdx.reftable_writer import STATE
from test_checkout_plan import commit_tree
import test_objects as oracle


class ReftableWriterNativeTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def fixture(self, root, format=ObjectFormat.SHA1):
        repo = Repository.init(root, bare=True, object_format=format)
        first = commit_tree(repo, {b'file': b'old'})
        tip = commit_tree(repo, {b'file': b'new'}, parents=(first,))
        repo.refs.update('refs/heads/main', first, expected=None)
        oracle.git(repo.git_dir, 'refs', 'migrate', '--ref-format=reftable')
        return Repository.open(root), first, tip

    def test_native_read_write_exact_case_symbols_objects_and_compaction(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                repo, first, tip = self.fixture(tmp, format);writer = repo.refs.writer()
                with patch('subprocess.Popen', side_effect=AssertionError('runtime helper')):
                    result = writer.transaction((ReftableChange('refs/heads/main', DirectRef(tip), DirectRef(first)),
                        ReftableChange('refs/heads/Foo', DirectRef(first), None),
                        ReftableChange('refs/heads/foo', DirectRef(tip), None),
                        ReftableChange('refs/tags/CON', DirectRef(tip), None),
                        ReftableChange('refs/tags/é', DirectRef(first), None)))
                    self.assertTrue(result.committed);self.assertTrue(result.atomic)
                    blob = writer.write_object(Blob(b'explicit writer').to_raw())
                    self.assertEqual(repo.objects.read(blob).data, b'explicit writer')
                    writer.transaction((ReftableChange('HEAD', DirectRef(first), SymbolicRef('refs/heads/main')),))
                self.assertEqual(oracle.git(repo.git_dir, 'rev-parse', 'HEAD').strip().decode(), first.hex)
                oracle.git(repo.git_dir, 'update-ref', 'refs/heads/main', first.hex, tip.hex)
                writer.transaction((ReftableChange('refs/heads/Foo', None, DirectRef(first)),))
                before = repo.refs.snapshot();retired = writer.compact()
                self.assertTrue(retired.retired_tables)
                self.assertEqual(repo.refs.snapshot(), before)
                for name in retired.retired_tables: self.assertTrue((repo.git_dir/'reftable'/name).exists())
                expected = [n.encode()+b' '+v.oid.hex.encode() for n, v in before]
                self.assertEqual(oracle.git(repo.git_dir, 'for-each-ref', '--format=%(refname) %(objectname)').splitlines(), expected)
                oracle.git(repo.git_dir, 'fsck', '--strict', '--no-dangling')

    def test_native_reflog_payloads_and_update_indexes_survive_compaction(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                repo, first, tip = self.fixture(tmp, format)
                oracle.git(repo.git_dir, '-c', 'core.logAllRefUpdates=always', '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'update-ref', '-m', 'native message', 'refs/heads/main', tip.hex, first.hex)
                expected = oracle.git(repo.git_dir, 'reflog', 'show', '--format=%H %gs', 'refs/heads/main')
                root = repo.git_dir/'reftable'
                tables = [Reftable.from_bytes((root/n).read_bytes(), format) for n in (root/'tables.list').read_text().splitlines()]
                updates = {};logs = {}
                for table in tables:
                    updates.update(table.ref_updates)
                    logs.update({key: (kind, data) for key, kind, data in table.logs})
                repo.refs.writer().compact()
                name = (root/'tables.list').read_text().strip();table = Reftable.from_bytes((root/name).read_bytes(), format)
                self.assertEqual(dict(table.ref_updates), updates)
                self.assertEqual({key: (kind, data) for key, kind, data in table.logs}, logs)
                self.assertEqual(oracle.git(repo.git_dir, 'reflog', 'show', '--format=%H %gs', 'refs/heads/main'), expected)

    def test_cas_contention_limits_policy_and_symbol_cycles_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, first, tip = self.fixture(tmp);writer = repo.refs.writer()
            before = (repo.git_dir/'reftable/tables.list').read_bytes()
            with self.assertRaises(RefConflict): writer.transaction((ReftableChange('refs/heads/main', DirectRef(tip), None),))
            with self.assertRaises(InvalidObject): writer.transaction((ReftableChange('HEAD', SymbolicRef('refs/heads/cycle'), SymbolicRef('refs/heads/main')),
                ReftableChange('refs/heads/cycle', SymbolicRef('refs/heads/cycle'), None)))
            with self.assertRaisesRegex(RuntimeError, 'policy'):
                writer.transaction((ReftableChange('refs/heads/main', DirectRef(tip), DirectRef(first)),),
                    before_publish=lambda: (_ for _ in ()).throw(RuntimeError('policy')))
            lock = repo.git_dir/'reftable/tables.list.lock';lock.write_bytes(b'unknown')
            with self.assertRaises(RefLocked): writer.compact()
            self.assertEqual(lock.read_bytes(), b'unknown');lock.unlink()
            limited = ReftableRefStore(repo.git_dir, repo.objects, limits=ReftableLimits(max_tables=1)).writer()
            with self.assertRaises(ObjectLimitExceeded): limited.transaction((ReftableChange('refs/heads/main', DirectRef(tip), DirectRef(first)),))
            self.assertEqual((repo.git_dir/'reftable/tables.list').read_bytes(), before)

    def test_atomic_snapshot_visibility_during_repeated_native_compatible_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, first, tip = self.fixture(tmp);writer = repo.refs.writer()
            writer.transaction(tuple(ReftableChange('refs/heads/'+name, DirectRef(first), None) for name in ('a', 'b')))
            done = threading.Event();ready = threading.Event();failures = [];observations = []
            def reader():
                try:
                    while not done.is_set():
                        values = dict(repo.refs.snapshot());a = values['refs/heads/a'];b = values['refs/heads/b']
                        if a != b: raise AssertionError('partially published reftable batch')
                        native = oracle.git(repo.git_dir, 'for-each-ref', '--format=%(objectname)',
                            'refs/heads/a', 'refs/heads/b').splitlines()
                        if len(native) != 2 or native[0] != native[1]: raise AssertionError('partial native reader snapshot')
                        observations.append(a)
                        ready.set()
                except BaseException as error: failures.append(error)
            worker = threading.Thread(target=reader);worker.start()
            try:
                self.assertTrue(ready.wait(10))
                old = first
                for i in range(8):
                    new = tip if old == first else first
                    writer.transaction(tuple(ReftableChange('refs/heads/'+name, DirectRef(new), DirectRef(old)) for name in ('a', 'b')))
                    old = new
            finally: done.set();worker.join(10)
            self.assertEqual(failures, []);self.assertTrue(observations)

    def test_interrupted_append_and_compaction_before_after_publication_and_later_edits(self):
        from gitdx import reftable_writer
        for compact in (False, True):
            for after in (False, True):
                with self.subTest(compact=compact, after=after), tempfile.TemporaryDirectory() as tmp:
                    repo, first, tip = self.fixture(tmp);writer = repo.refs.writer()
                    original = reftable_writer.replace_file
                    def fail(root, path, data):
                        if path == repo.git_dir/'reftable/tables.list':
                            if after: original(root, path, data)
                            raise OSError('interrupted stack publication')
                        return original(root, path, data)
                    with patch.object(reftable_writer, 'replace_file', fail), self.assertRaises(ReftableWriteInterrupted) as caught:
                        if compact: writer.compact()
                        else: writer.transaction((ReftableChange('refs/heads/main', DirectRef(tip), DirectRef(first)),))
                    self.assertEqual(caught.exception.result.state, RefTransactionState.COMMITTED if after else RefTransactionState.PREPARED)
                    result = Repository.open(tmp).refs.writer().recover()
                    self.assertEqual(result.state, RefTransactionState.COMMITTED if after else RefTransactionState.ROLLED_BACK)
                    self.assertEqual(repo.refs.resolve().oid, tip if after and not compact else first)
                    oracle.git(repo.git_dir, 'fsck', '--strict', '--no-dangling')

    def test_recovery_preserves_later_native_edits(self):
        from gitdx import reftable_writer
        with tempfile.TemporaryDirectory() as tmp:
            repo, first, tip = self.fixture(tmp);writer = repo.refs.writer()
            original = reftable_writer.replace_file
            def fail(root, path, data):
                original(root, path, data)
                if path == repo.git_dir/'reftable/tables.list': raise OSError('lost response')
            with patch.object(reftable_writer, 'replace_file', fail), self.assertRaises(ReftableWriteInterrupted):
                writer.transaction((ReftableChange('refs/heads/main', DirectRef(tip), DirectRef(first)),))
            oracle.git(repo.git_dir, 'update-ref', 'refs/heads/later', tip.hex)
            listing = (repo.git_dir/'reftable/tables.list').read_bytes()
            with self.assertRaises(RefConflict): Repository.open(tmp).refs.writer().recover()
            self.assertEqual((repo.git_dir/'reftable/tables.list').read_bytes(), listing)
            self.assertEqual(repo.refs.resolve('refs/heads/later').oid, tip)

    def test_process_exit_recovery_never_steals_native_stack_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, first, tip = self.fixture(tmp)
            code = '''import os, sys
from gitdx import Repository, ReftableChange, DirectRef, ObjectId
repo=Repository.open(sys.argv[1])
def check():
    if (repo.git_dir/'reftable-write-state.json').exists(): os._exit(61)
repo.refs.writer().transaction((ReftableChange('refs/heads/main', DirectRef(ObjectId(sys.argv[3])), DirectRef(ObjectId(sys.argv[2]))),), check=check)
'''
            result = subprocess.run([sys.executable, '-c', code, tmp, first.hex, tip.hex], capture_output=True)
            self.assertEqual(result.returncode, 61, result.stderr)
            with self.assertRaises(RefLocked): Repository.open(tmp).refs.writer().recover()
            lock = repo.git_dir/'reftable/tables.list.lock';self.assertEqual(lock.read_bytes(), b'');lock.unlink()
            result = Repository.open(tmp).refs.writer().recover()
            self.assertEqual(result.state, RefTransactionState.ROLLED_BACK)
            self.assertEqual(repo.refs.resolve().oid, first)
