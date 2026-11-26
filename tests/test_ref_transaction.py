from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (FilesRefStore, IndexFile, InvalidRef, ObjectFormat, PackedRef, PackedRefs,
                   PushCommand, ReceiveInterrupted, ReceivePolicy, ReceiveState, RefChange,
                   RefConflict, RefLocked, RefRecoveryRequired, RefTransactionInterrupted,
                   RefTransactionState, Repository, UnsupportedFormat)
import gitdx.ref_transaction as transactions
from test_checkout_plan import commit_tree
import test_objects as oracle


def snapshot(repo):
    return {p.relative_to(repo.git_dir).as_posix():p.read_bytes() for p in (repo.git_dir/'refs').rglob('*') if p.is_file()} | {
        'packed-refs':(repo.git_dir/'packed-refs').read_bytes() if (repo.git_dir/'packed-refs').exists() else None}


class RefTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository.init(self.root/'repo',bare=True)
        self.old=commit_tree(self.repo,{b'file':b'old'})
        self.new=commit_tree(self.repo,{b'file':b'new'},parents=(self.old,))
        names=('refs/heads/a','refs/heads/b','refs/tags/dead','refs/tags/keep')
        for name in names:self.repo.refs.update(name,self.old,expected=None)
        (self.repo.git_dir/'packed-refs').write_bytes(PackedRefs(tuple(PackedRef(n,self.old) for n in names)).encode())
        self.changes=(RefChange(names[0],self.new,self.old),RefChange(names[1],self.new,self.old),RefChange(names[2],None,self.old))
        self.before=snapshot(self.repo)

    def interrupt(self):
        original=transactions._put
        def after_first(store,name,data):
            original(store,name,data)
            if name==self.changes[0].name:raise OSError('injected after first ref')
        with patch.object(transactions,'_put',after_first):
            with self.assertRaises(RefTransactionInterrupted) as caught:self.repo.refs.transaction(self.changes)
        return caught.exception.result

    def test_deterministic_locks_all_value_cas_and_atomic_refusal(self):
        observed=[];original=FilesRefStore._lock
        @contextmanager
        def record(store,path):
            observed.append(path.relative_to(store.path).as_posix())
            with original(store,path):yield
        with patch.object(FilesRefStore,'_lock',record):result=self.repo.refs.transaction(reversed(self.changes))
        self.assertTrue(result.committed);self.assertFalse(result.atomic)
        self.assertEqual(observed,['packed-refs',*(c.name for c in self.changes)])
        self.assertEqual(self.repo.refs.read('refs/heads/a').oid,self.new)
        self.assertEqual(self.repo.refs.read('refs/heads/b').oid,self.new)
        self.assertIsNone(self.repo.refs.read('refs/tags/dead'))
        self.assertEqual(self.repo.refs.read('refs/tags/keep').oid,self.old)
        with self.assertRaises(UnsupportedFormat):self.repo.refs.transaction(self.changes,atomic=True)

    def test_every_expected_old_is_validated_before_any_publication(self):
        stale=RefChange('refs/heads/b',self.new,self.new)
        with self.assertRaises(RefConflict):self.repo.refs.transaction((self.changes[0],stale))
        self.assertEqual(snapshot(self.repo),self.before)
        self.assertFalse((self.repo.git_dir/transactions.STATE_NAME).exists())
        for values in ((),self.changes*100,(self.changes[0],self.changes[0]),
                       (RefChange('refs/heads/a/x',self.new,None),self.changes[0])):
            with self.assertRaises((ValueError,InvalidRef)):self.repo.refs.transaction(values)
        with self.assertRaises(InvalidRef):self.repo.refs.transaction((RefChange('refs/heads/A',self.new,self.old),))
        with self.assertRaises(RuntimeError):self.repo.refs.transaction(self.changes,before_publish=lambda:(_ for _ in ()).throw(RuntimeError('policy failed')))
        self.assertEqual(snapshot(self.repo),self.before)

    def test_competing_writers_fail_without_stealing_locks(self):
        ready=threading.Event();release=threading.Event()
        def guard():
            ready.set()
            if not release.wait(10):raise RuntimeError('test guard timed out')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.repo.refs.transaction,self.changes,before_publish=guard)
            try:
                self.assertTrue(ready.wait(10))
                for action in (lambda:self.repo.refs.transaction(self.changes),
                               lambda:self.repo.refs.update('refs/heads/a',self.new,expected=self.old)):
                    with self.assertRaises(RefLocked):action()
                self.assertEqual(snapshot(self.repo),{**self.before,**{n+'.lock':b'' for n in ('refs/heads/a','refs/heads/b','refs/tags/dead')}})
            finally:release.set()
            self.assertTrue(future.result(timeout=30).committed)
        self.assertFalse(list(self.repo.git_dir.rglob('*.lock')))

    def test_prepared_faults_at_every_file_boundary_roll_back_exact_bytes(self):
        original=transactions._put
        for boundary in range(1,5):
            calls=0
            def fault(store,name,data):
                nonlocal calls
                original(store,name,data);calls+=1
                if calls==boundary:raise OSError('injected file boundary')
            with self.subTest(boundary=boundary),patch.object(transactions,'_put',fault):
                with self.assertRaises(RefTransactionInterrupted) as caught:self.repo.refs.transaction(self.changes)
            self.assertEqual(caught.exception.result.state,RefTransactionState.PREPARED)
            self.assertTrue(caught.exception.result.journal_pending)
            for action in (lambda:self.repo.refs.update('refs/heads/a',self.old,expected=self.new),
                           lambda:self.repo.refs.transaction(self.changes),
                           lambda:IndexFile(self.repo.git_dir,self.repo.object_format).transaction().__enter__()):
                with self.assertRaises(RefRecoveryRequired):action()
            result=self.repo.refs.recover_transaction()
            self.assertEqual(result.state,RefTransactionState.ROLLED_BACK)
            self.assertEqual(snapshot(self.repo),self.before)

    def test_commit_marker_before_after_and_retirement_faults_have_explicit_recovery(self):
        original=transactions._save
        for after in (False,True):
            def marker(root,journal):
                if journal['phase']=='committed':
                    if after:original(root,journal)
                    raise OSError('injected marker boundary')
                original(root,journal)
            with self.subTest(after=after),patch.object(transactions,'_save',marker):
                with self.assertRaises(RefTransactionInterrupted) as caught:self.repo.refs.transaction(self.changes)
            self.assertEqual(caught.exception.result.committed,after)
            recovered=self.repo.refs.recover_transaction()
            self.assertEqual(recovered.committed,after)
            if after:
                self.assertIsNone(self.repo.refs.read('refs/tags/dead'))
                # Restore the fixture explicitly for the next independent fault.
                self.repo.refs.transaction(tuple(RefChange(c.name,c.expected,c.new) for c in self.changes))
                (self.repo.git_dir/'packed-refs').write_bytes(self.before['packed-refs'])
            self.assertEqual(snapshot(self.repo),self.before)
        with patch.object(transactions,'_retire',side_effect=OSError('before retirement')):
            with self.assertRaises(RefTransactionInterrupted) as caught:self.repo.refs.transaction(self.changes)
        self.assertTrue(caught.exception.result.committed)
        self.assertTrue(self.repo.refs.recover_transaction().committed)

    def test_retired_cleanup_failure_does_not_block_later_transactions(self):
        with patch.object(transactions.shutil,'rmtree',side_effect=OSError('cleanup failed')):
            result=self.repo.refs.transaction(self.changes)
        self.assertTrue(result.committed);self.assertFalse(result.journal_pending)
        self.assertTrue(result.retired_path.is_dir())
        self.assertTrue(self.repo.refs.transaction((RefChange('refs/tags/after',self.new,None),)).committed)

    def test_recovery_refuses_later_external_edits_and_corrupt_journals(self):
        self.interrupt();foreign=commit_tree(self.repo,{b'file':b'foreign'})
        path=self.repo.git_dir/self.changes[0].name;path.write_bytes(str(foreign).encode()+b'\n')
        with self.assertRaises(RefConflict):self.repo.refs.recover_transaction()
        self.assertEqual(path.read_bytes(),str(foreign).encode()+b'\n')
        state=self.repo.git_dir/transactions.STATE_NAME/'state.json'
        data=json.loads(state.read_text());data['id']='f'*32;state.write_text(json.dumps(data))
        with self.assertRaises(RefRecoveryRequired):self.repo.refs.recover_transaction()
        self.assertEqual(path.read_bytes(),str(foreign).encode()+b'\n')

    def test_journal_rechecked_after_single_ref_and_index_lock_acquisition(self):
        original=FilesRefStore._mutation_locks
        @contextmanager
        def late(store,name):
            with original(store,name) as value:
                (store.path/transactions.STATE_NAME).mkdir()
                yield value
        with patch.object(FilesRefStore,'_mutation_locks',late):
            with self.assertRaises(RefRecoveryRequired):self.repo.refs.update('refs/heads/a',self.new,expected=self.old)
        self.assertEqual(snapshot(self.repo),self.before)
        (self.repo.git_dir/transactions.STATE_NAME).rmdir()
        indexed=Repository.init(self.root/'indexed')
        original_open=os.open
        def late_index(path,*args,**kwargs):
            fd=original_open(path,*args,**kwargs)
            if Path(path).name=='index.lock':(indexed.git_dir/transactions.STATE_NAME).mkdir()
            return fd
        with patch('gitdx.index.os.open',late_index):
            with self.assertRaises(RefRecoveryRequired):
                with indexed.index.transaction():pass
        self.assertFalse((indexed.git_dir/'index.lock').exists())
        (indexed.git_dir/transactions.STATE_NAME).rmdir()
        with indexed.index.transaction() as mutation:
            (indexed.git_dir/transactions.STATE_NAME).mkdir()
            with self.assertRaises(RefRecoveryRequired):mutation.commit()
        self.assertFalse(indexed.index.path.exists())
        self.assertFalse((indexed.git_dir/'index.lock').exists())

    def test_coordinated_receive_rejects_stale_batch_and_recovers_partial_files(self):
        source=Repository.init(self.root/'source',bare=True)
        tip=commit_tree(source,{b'new':b'data'});pack=source.pack((tip,)).pack
        commands=(PushCommand(b'refs/heads/new',None,tip),PushCommand(b'refs/heads/a',self.old,tip))
        policy=ReceivePolicy(allow_non_fast_forwards=True)
        with self.assertRaises(UnsupportedFormat):self.repo.receive(commands,pack,policy=policy,atomic=True)
        stale=(commands[0],PushCommand(b'refs/heads/a',self.new,tip))
        result=self.repo.receive(stale,pack,policy=policy,coordinated=True)
        self.assertTrue(all(o.state is ReceiveState.REJECTED for o in result.outcomes))
        self.assertIsNone(self.repo.refs.read('refs/heads/new'))
        original=transactions._put
        def fault(store,name,data):
            original(store,name,data)
            if name=='refs/heads/a':raise OSError('after coordinated ref')
        with patch.object(transactions,'_put',fault):
            with self.assertRaises(ReceiveInterrupted) as caught:self.repo.receive(commands,pack,policy=policy,coordinated=True)
        result=caught.exception.result
        self.assertTrue(result.coordinated);self.assertTrue(result.ref_transaction.journal_pending)
        self.assertTrue(all(o.state is ReceiveState.UNKNOWN for o in result.outcomes))
        self.repo.refs.recover_transaction();self.assertEqual(snapshot(self.repo),self.before)
        with patch('subprocess.Popen',side_effect=AssertionError('runtime helper')):
            result=self.repo.receive(commands,pack,policy=policy,coordinated=True)
        self.assertTrue(result.complete);self.assertTrue(result.ref_transaction.committed);self.assertFalse(result.atomic)


class RefTransactionNativeTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_readers_see_intermediate_values_and_selected_writers_are_locked(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                repo=Repository.init(tmp,bare=True,object_format=format)
                old=commit_tree(repo,{b'file':b'old'});new=commit_tree(repo,{b'file':b'new'},parents=(old,))
                for name in ('a','b','unselected'):repo.refs.update('refs/heads/'+name,old,expected=None)
                oracle.git(repo.git_dir,'pack-refs','--all')
                changes=tuple(RefChange('refs/heads/'+n,new,old) for n in ('a','b'))
                observed=[];original=transactions._put;guard_calls=0
                def guard():
                    nonlocal guard_calls
                    guard_calls+=1
                    if guard_calls==1:
                        with self.assertRaises(subprocess.CalledProcessError):oracle.git(repo.git_dir,'update-ref','refs/heads/a',str(new),str(old))
                        oracle.git(repo.git_dir,'update-ref','refs/heads/unselected',str(new),str(old))
                def inspect(store,name,data):
                    original(store,name,data)
                    if name=='refs/heads/a':
                        observed.append(oracle.git(repo.git_dir,'for-each-ref','--format=%(objectname)','refs/heads/a','refs/heads/b').splitlines())
                with patch.object(transactions,'_put',inspect):result=repo.refs.transaction(changes,before_publish=guard)
                self.assertEqual(observed,[[str(new).encode(),str(old).encode()]])
                self.assertFalse(result.atomic);self.assertTrue(result.committed)
                self.assertEqual(repo.refs.read('refs/heads/unselected').oid,new)
                oracle.git(repo.git_dir,'fsck','--strict','--no-dangling')

    def test_process_exit_retains_journal_and_only_proven_dead_fixture_locks_are_removed(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                repo=Repository.init(tmp,bare=True,object_format=format)
                old=commit_tree(repo,{b'file':b'old'});new=commit_tree(repo,{b'file':b'new'},parents=(old,))
                for name in ('a','b'):repo.refs.update('refs/heads/'+name,old,expected=None)
                script="""
import os,sys
from gitdx import Repository,RefChange,ObjectId
import gitdx.ref_transaction as transaction
repo=Repository.open(sys.argv[1]);old=ObjectId(sys.argv[2],repo.object_format);new=ObjectId(sys.argv[3],repo.object_format)
original=transaction._put
def crash(store,name,data):
    original(store,name,data)
    if name=='refs/heads/a':os._exit(86)
transaction._put=crash
repo.refs.transaction(tuple(RefChange('refs/heads/'+name,new,old) for name in ('a','b')))
"""
                result=subprocess.run([sys.executable,'-I','-c',script,str(repo.git_dir),str(old),str(new)],capture_output=True,timeout=30)
                self.assertEqual(result.returncode,86,result.stderr)
                self.assertTrue((repo.git_dir/transactions.STATE_NAME).is_dir())
                # The test owns this isolated fixture and waited for this exact
                # process to exit. Production never removes unknown lockfiles.
                for name in ('packed-refs','refs/heads/a','refs/heads/b'):
                    lock=repo.git_dir/(name+'.lock');self.assertEqual(lock.read_bytes(),b'');lock.unlink()
                self.assertEqual(repo.refs.recover_transaction().state,RefTransactionState.ROLLED_BACK)
                self.assertEqual([repo.refs.read('refs/heads/'+n).oid for n in ('a','b')],[old,old])
