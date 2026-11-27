from pathlib import Path
import os
import importlib
import errno
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (Blob, ConfinementError, FilesystemLimits, IngestionLimits, IngestionProfile,
                   InvalidObject, ObjectFormat, ObjectId, ObjectLimitExceeded, PackLimits,
                   PushCommand, RawObject, ReceiveInterrupted, ReceiveLimits, ReceivePolicy,
                   ReceiveRejected, Repository, TransferCancelled, UnsupportedFormat)
from gitdx.confined_fs import ConfinedStorage, active, supported
from gitdx.delta import DeltaBudget, _apply
from gitdx.ingestion import ObjectValidator
from gitdx.quarantine import unpack_objects
from test_checkout_plan import commit_tree
from test_pack import fixture
import test_objects as oracle


class IngestionTests(unittest.TestCase):
    def test_sha1_gate_precedes_all_storage_pack_and_policy_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Repository.init(tmp,bare=True)
            policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED,object_check=lambda *a:self.fail('object policy called'))
            with patch('gitdx.receive.receive_profile',side_effect=AssertionError('filesystem inspected')):
                with self.assertRaises(UnsupportedFormat):repo.receive((),b'not even a pack',policy=policy)
            self.assertEqual(list(repo.objects.path.glob('incoming-*')),[])

    def test_actual_platform_refusal_or_qualified_sha256_receive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Repository.init(Path(tmp)/'source',bare=True,object_format='sha256')
            target=Repository.init(Path(tmp)/'target',bare=True,object_format='sha256')
            tip=commit_tree(source,{b'file':b'data'});command=PushCommand(b'refs/heads/main',None,tip)
            arguments=((command,),source.pack((tip,)).pack)
            policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED)
            if supported():
                result=target.receive(*arguments,policy=policy,coordinated=True)
                self.assertTrue(result.complete);self.assertEqual(result.storage_identity,target.storage_identity)
            else:
                with self.assertRaises(UnsupportedFormat):target.receive(*arguments,policy=policy)
                self.assertTrue(target.refs.resolve().unborn)

    def test_header_and_tree_bounds_precede_parser_allocation(self):
        format=ObjectFormat.SHA256
        raw=RawObject('commit',b'tree '+b'1'*64+b'\n'+b'extra x\n'*50+b'\nmessage')
        validator=ObjectValidator(format,limits=IngestionLimits(max_headers=4))
        with patch('gitdx.ingestion.parse_object',side_effect=AssertionError('decoded excessive headers')):
            with self.assertRaises(ObjectLimitExceeded):validator(raw)
        validator=ObjectValidator(format,limits=IngestionLimits(max_header_bytes=16))
        with self.assertRaises(ObjectLimitExceeded):validator(raw)
        tree=RawObject('tree',b'100644 a\0'+bytes(32)+b'100644 b\0'+bytes(32))
        with patch('gitdx.ingestion.parse_object',side_effect=AssertionError('decoded excessive entries')):
            with self.assertRaises(ObjectLimitExceeded):ObjectValidator(format,limits=IngestionLimits(max_tree_entries=1))(tree)

    def test_unsafe_tree_names_and_noncanonical_identities_refused(self):
        for name in (b'.',b'..',b'.git',b'.GiT'):
            with self.subTest(name=name),self.assertRaises(InvalidObject):
                ObjectValidator(ObjectFormat.SHA256)(RawObject('tree',b'100644 '+name+b'\0'+b'\x01'*32))
        with self.assertRaises(InvalidObject):
            ObjectValidator(ObjectFormat.SHA256)(RawObject('tree',b'120000 .gitmodules\0'+b'\x01'*32))
        raw=RawObject('commit',b'tree '+b'1'*64+b'\nauthor invalid\ncommitter invalid\n\nmessage')
        with self.assertRaises(InvalidObject):ObjectValidator(ObjectFormat.SHA256)(raw)

    def test_inflation_reservation_precedes_decompression_and_delta_checks_cancel(self):
        data,_,_=fixture((Blob(b'x'*100000).to_raw(),))
        with patch('gitdx.quarantine.zlib.decompressobj',side_effect=AssertionError('allocated excess inflation')):
            with self.assertRaises(ObjectLimitExceeded):unpack_objects(data,ObjectFormat.SHA1,limits=PackLimits(max_delta_bytes=8))
        calls=0
        def cancel():
            nonlocal calls
            calls+=1
            if calls==2:raise TransferCancelled('test cancellation')
        budget=DeltaBudget(PackLimits(),check=cancel)
        with self.assertRaises(TransferCancelled):_apply(b'',b'\x00\x80\x20'+b'\x01x'*4096,budget)
        self.assertEqual(budget.operations,1024)


@unittest.skipUnless(os.name=='posix','descriptor mechanics require POSIX; the public profile refuses other platforms')
class DescriptorStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'repo';self.root.mkdir();self.outside=Path(self.temp.name)/'outside';self.outside.mkdir()
        info=self.root.stat();self.identity=(info.st_dev,info.st_ino)
        # Exercise the same POSIX primitives on macOS, without advertising that
        # platform as a qualified hosting profile. Linux uses its real support.
        override=patch('gitdx.confined_fs.supported',return_value=True)
        override.start();self.addCleanup(override.stop)

    def test_regular_descriptor_reads_and_writes_and_namespace_escape_refusal(self):
        with ConfinedStorage(self.root,self.identity) as storage:
            storage.write(self.root/'nested'/'file',b'data')
            self.assertEqual(storage.read(self.root/'nested'/'file',4,ValueError),b'data')
            with self.assertRaises(ConfinementError):storage.read(self.outside/'file',4,ValueError)
            with self.assertRaises(ConfinementError):storage.write(self.root/'..'/'escape',b'no')
        self.assertFalse((self.outside/'file').exists())

    def test_full_initial_and_fast_forward_receive_avoid_path_based_metadata_probes(self):
        source=Repository.init(self.root/'source',bare=True,object_format='sha256')
        target=Repository.init(self.root/'target',bare=True,object_format='sha256')
        old=commit_tree(source,{b'file':b'old'});new=commit_tree(source,{b'file':b'new'},parents=(old,))
        first=source.pack((old,)).pack;second=source.pack((new,)).pack
        policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED)
        with patch.object(Path,'exists',side_effect=AssertionError('path-based exists')),\
             patch.object(Path,'is_dir',side_effect=AssertionError('path-based directory probe')),\
             patch.object(Path,'stat',side_effect=AssertionError('path-based stat')),\
             patch.object(Path,'lstat',side_effect=AssertionError('path-based lstat')):
            self.assertTrue(target.receive((PushCommand(b'refs/heads/main',None,old),),first,policy=policy,coordinated=True).complete)
            self.assertTrue(target.receive((PushCommand(b'refs/heads/main',old,new),),second,policy=policy,coordinated=True).complete)
        self.assertEqual(target.refs.resolve().oid,new)

    def test_read_parent_replacement_cannot_follow_the_outside_symlink(self):
        parent=self.root/'nested';parent.mkdir();(parent/'file').write_bytes(b'inside')
        (self.outside/'file').write_bytes(b'outside');original=os.open;moved=self.root/'retained'
        def race(name,*args,**kwargs):
            if name=='file' and kwargs.get('dir_fd') is not None and not moved.exists():
                parent.rename(moved);parent.symlink_to(self.outside,target_is_directory=True)
            return original(name,*args,**kwargs)
        with ConfinedStorage(self.root,self.identity) as storage,patch('gitdx.confined_fs.os.open',race):
            self.assertEqual(storage.read(parent/'file',32,ValueError),b'inside')
            with self.assertRaises(OSError):storage.read(parent/'file',32,ValueError)
        self.assertEqual((self.outside/'file').read_bytes(),b'outside')

    def test_publish_parent_replacement_stays_with_the_opened_directory(self):
        parent=self.root/'nested';parent.mkdir();(self.outside/'file').write_bytes(b'outside')
        original=os.link;moved=self.root/'retained'
        def race(source,target,*args,**kwargs):
            if target=='file' and not moved.exists():
                parent.rename(moved);parent.symlink_to(self.outside,target_is_directory=True)
            return original(source,target,*args,**kwargs)
        with ConfinedStorage(self.root,self.identity) as storage,patch('gitdx.confined_fs.os.link',race):
            storage.write(parent/'file',b'inside',immutable=True)
        self.assertEqual((moved/'file').read_bytes(),b'inside');self.assertEqual((self.outside/'file').read_bytes(),b'outside')

    def test_symlinks_hardlinks_fifo_and_wrong_identity_are_refused(self):
        outside=self.outside/'data';outside.write_bytes(b'preserved')
        (self.root/'link').symlink_to(outside);os.link(outside,self.root/'hard');os.mkfifo(self.root/'pipe',0o600)
        with ConfinedStorage(self.root,self.identity) as storage:
            with self.assertRaises(OSError):storage.read(self.root/'link',100,ValueError)
            for name in ('hard','pipe'):
                with self.assertRaises(ConfinementError):storage.read(self.root/name,100,ValueError)
        with self.assertRaises(ConfinementError):
            with ConfinedStorage(self.root,(0,0)):pass
        self.assertEqual(outside.read_bytes(),b'preserved')

    def test_permissions_and_allocation_work_and_byte_budgets_fail_closed(self):
        self.root.chmod(0o770)
        with self.assertRaises(ConfinementError):
            with ConfinedStorage(self.root,self.identity):pass
        self.root.chmod(0o700)
        with ConfinedStorage(self.root,self.identity,limits=FilesystemLimits(max_reserved_bytes=1)) as storage:
            with self.assertRaises(ConfinementError):storage.write(self.root/'nested'/'file',b'x')
        self.assertFalse((self.root/'nested').exists())
        (self.root/'data').write_bytes(b'bytes')
        with ConfinedStorage(self.root,self.identity,limits=FilesystemLimits(max_io_bytes=1)) as storage:
            with self.assertRaises(ConfinementError):storage.read(self.root/'data',10,ValueError)
        with ConfinedStorage(self.root,self.identity,limits=FilesystemLimits(max_entries=1)) as storage:
            with self.assertRaises(ConfinementError):storage.names(self.root);storage.names(self.root)

    def test_complete_receive_and_post_preflight_ref_symlink_attack(self):
        source=Repository.init(self.root/'source',bare=True,object_format='sha256')
        target=Repository.init(self.root/'target',bare=True,object_format='sha256')
        tip=commit_tree(source,{b'file':b'data'});command=PushCommand(b'refs/heads/main',None,tip)
        pack=source.pack((tip,)).pack;policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED)
        result=target.receive((command,),pack,policy=policy,coordinated=True)
        self.assertTrue(result.complete);self.assertFalse(result.cleanup_pending)
        victim=Repository.init(self.root/'victim',bare=True,object_format='sha256');called=False
        def attack(view,command):
            nonlocal called
            if not called:
                called=True;heads=victim.git_dir/'refs/heads';heads.rename(heads.with_name('retained'))
                heads.symlink_to(self.outside,target_is_directory=True)
        with self.assertRaises(ReceiveInterrupted):
            victim.receive((command,),pack,policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED,ref_check=attack),coordinated=True)
        self.assertEqual(list(self.outside.iterdir()),[])

    def test_repository_replacement_after_open_refused_before_any_write(self):
        repo=Repository.init(self.root/'target',bare=True,object_format='sha256')
        moved=self.root/'original';repo.git_dir.rename(moved)
        foreign=Repository.init(repo.git_dir,bare=True,object_format='sha256')
        with self.assertRaises(ConfinementError):repo.receive((),policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED))
        self.assertTrue(foreign.refs.resolve().unborn);self.assertFalse(list(foreign.objects.path.glob('incoming-*')))

    def test_interrupted_coordinated_receive_recovers_through_same_confinement(self):
        source=Repository.init(self.root/'source',bare=True,object_format='sha256')
        target=Repository.init(self.root/'target',bare=True,object_format='sha256')
        tip=commit_tree(source,{b'file':b'data'});pack=source.pack((tip,)).pack
        command=PushCommand(b'refs/heads/main',None,tip)
        transaction=importlib.import_module('gitdx.ref_transaction');original=transaction._put
        def fault(store,name,data):
            original(store,name,data)
            if name=='refs/heads/main':raise OSError('injected after ref publication')
        with patch.object(transaction,'_put',fault),self.assertRaises(ReceiveInterrupted) as caught:
            target.receive((command,),pack,policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED),coordinated=True)
        self.assertTrue(caught.exception.result.ref_transaction.journal_pending)
        result=target.recover_receive(ingestion=IngestionProfile.UNTRUSTED)
        self.assertEqual(result.state.value,'rolled-back');self.assertTrue(target.refs.resolve().unborn)

    def test_file_exists_after_publication_is_unknown_not_a_lock_rejection(self):
        source=Repository.init(self.root/'source',bare=True,object_format='sha256')
        target=Repository.init(self.root/'target',bare=True,object_format='sha256')
        tip=commit_tree(source,{b'file':b'data'});original=target.refs.__class__._write
        def fault(store,*args,**kwargs):
            result=original(store,*args,**kwargs);raise FileExistsError('injected after publication')
        with patch.object(target.refs.__class__,'_write',fault),self.assertRaises(ReceiveInterrupted) as caught:
            target.receive((PushCommand(b'refs/heads/main',None,tip),),source.pack((tip,)).pack,
                           policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED))
        self.assertEqual(caught.exception.result.outcomes[0].state.value,'unknown')
        self.assertEqual(target.refs.resolve().oid,tip)

    def test_cleanup_has_its_own_bounded_budget_after_cancellation_or_work_exhaustion(self):
        source=Repository.init(self.root/'source',bare=True,object_format='sha256')
        tip=commit_tree(source,{b'file':b'data'});pack=source.pack((tip,)).pack
        for mode in ('cancel','work'):
            target=Repository.init(self.root/mode,bare=True,object_format='sha256');cancel=threading.Event()
            def reject(oid,raw):
                if mode=='cancel':cancel.set()
                else:active().charge(entries=active().limits.max_entries+1)
                return False
            with self.subTest(mode=mode),self.assertRaises(ReceiveRejected) as caught:
                target.receive((PushCommand(b'refs/heads/main',None,tip),),pack,
                               policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED,object_check=reject),cancel=cancel)
            self.assertFalse(caught.exception.result.cleanup_pending)
            self.assertEqual(list(target.objects.path.glob('incoming-*')),[])
            self.assertTrue(target.refs.resolve().unborn)

    def test_disk_full_during_quarantine_write_preserves_refs_and_cleans_owned_storage(self):
        source=Repository.init(self.root/'source',bare=True,object_format='sha256')
        target=Repository.init(self.root/'target',bare=True,object_format='sha256')
        tip=commit_tree(source,{b'file':b'data'});original=os.fsync;calls=0
        def full(fd):
            nonlocal calls
            calls+=1
            if calls==2:raise OSError(errno.ENOSPC,'injected disk full')
            return original(fd)
        with patch('gitdx.confined_fs.os.fsync',full),self.assertRaises(ReceiveInterrupted) as caught:
            target.receive((PushCommand(b'refs/heads/main',None,tip),),source.pack((tip,)).pack,
                           policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED))
        self.assertFalse(caught.exception.result.cleanup_pending)
        self.assertTrue(target.refs.resolve().unborn);self.assertEqual(list(target.objects.path.glob('incoming-*')),[])

    def test_process_exit_keeps_prepared_journal_for_explicit_confined_recovery(self):
        source=Repository.init(self.root/'source',bare=True,object_format='sha256')
        target=Repository.init(self.root/'target',bare=True,object_format='sha256')
        tip=commit_tree(source,{b'file':b'data'});pack=self.root/'input.pack';pack.write_bytes(source.pack((tip,)).pack)
        script='''
import importlib, os, sys
from pathlib import Path
from gitdx import Repository, PushCommand, ObjectId, IngestionProfile, ReceivePolicy
import gitdx.confined_fs as fs
fs.supported=lambda: True
transaction=importlib.import_module('gitdx.ref_transaction');original=transaction._put
def exit_after_write(store,name,data):
    original(store,name,data)
    if name=='refs/heads/main':os._exit(86)
transaction._put=exit_after_write
repo=Repository.open(sys.argv[1])
repo.receive((PushCommand(b'refs/heads/main',None,ObjectId(sys.argv[3],repo.object_format)),),
             Path(sys.argv[2]).read_bytes(),policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED),coordinated=True)
'''
        result=subprocess.run([sys.executable,'-I','-c',script,str(target.git_dir),str(pack),str(tip)],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,86,result.stderr.decode())
        self.assertTrue((target.git_dir/'ref-transaction-state').is_dir())
        self.assertEqual(target.refs.resolve().oid,tip)
        # Only fixture locks owned by this exact child, after waiting for its
        # confirmed exit. Production recovery never steals unknown locks.
        for name in ('packed-refs.lock','refs/heads/main.lock'):
            lock=target.git_dir/name;self.assertEqual(lock.read_bytes(),b'');lock.unlink()
        result=target.recover_receive(ingestion=IngestionProfile.UNTRUSTED)
        self.assertEqual(result.state.value,'rolled-back');self.assertTrue(target.refs.resolve().unborn)
        self.assertEqual(len(list(target.objects.path.glob('incoming-*'))),1)


class IngestionNativeTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_fsck_valid_graph_and_bad_identity_match_selected_validation(self):
        from gitdx import Commit
        with tempfile.TemporaryDirectory() as tmp:
            repo=Repository.init(tmp,bare=True,object_format='sha256')
            tip=commit_tree(repo,{b'file':b'data'});repo.refs.update('refs/heads/main',tip,expected=None)
            oracle.git(repo.git_dir,'fsck','--strict','--no-dangling')
            raw=repo.objects.read(tip);ObjectValidator(repo.object_format)(raw)
            commit=Commit.from_bytes(raw.data,repo.object_format)
            bad=Commit.create(commit.tree,author=b'invalid',committer=b'invalid',message=b'bad').to_raw()
            repo.objects.write(bad)
            with self.assertRaises(InvalidObject):ObjectValidator(repo.object_format)(bad)
            with self.assertRaises(subprocess.CalledProcessError):oracle.git(repo.git_dir,'fsck','--strict','--no-dangling')
