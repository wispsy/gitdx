from pathlib import Path
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (Blob, Commit, FilesRefStore, InvalidObject, InvalidRef, MissingObject,
                   ObjectFormat, ObjectId, ObjectQuarantine, PackLimits, PackWriteLimits,
                   PushCommand, QuarantineOwnershipError, ReceiveInterrupted, ReceiveLimits,
                   ReceivePolicy, ReceiveRejected, ReceiveState, Repository, TransferLimits,
                   UnsupportedFormat)
from test_checkout_plan import commit_tree
from test_objects import IDENTITY
from test_pack import fixture
import test_objects as oracle


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=Repository.init(self.root/'source',bare=True)
        self.target=Repository.init(self.root/'target',bare=True)
        self.tip=commit_tree(self.source,{b'file':b'content'})
        self.pack=self.source.pack((self.tip,)).pack
        self.command=PushCommand(b'refs/heads/main',None,self.tip)

    def test_accept_and_delete_with_explicit_policy_and_quarantined_view(self):
        calls=[]
        def ref_check(view,command):
            self.assertEqual(view.read(command.new),self.source.objects.read(self.tip))
            if not calls:
                with self.assertRaises(MissingObject):self.target.objects.read(self.tip)
            else:self.assertEqual(self.target.objects.read(self.tip),view.read(command.new))
            calls.append(command)
        with patch('subprocess.Popen',side_effect=AssertionError('external helper')):
            result=self.target.receive((self.command,),self.pack,policy=ReceivePolicy(ref_check=ref_check))
        self.assertTrue(result.complete);self.assertFalse(result.atomic);self.assertEqual(len(calls),2)
        self.assertEqual(result.received_objects,3);self.assertEqual(result.promoted_objects,3)
        self.assertFalse(result.cleanup_pending);self.assertFalse(list(self.target.objects.path.glob('incoming-*')))
        deletion=PushCommand(self.command.name,self.tip,None)
        denied=self.target.receive((deletion,),policy=ReceivePolicy())
        self.assertEqual(denied.outcomes[0].reason,b'deletion prohibited')
        self.assertTrue(self.target.receive((deletion,),policy=ReceivePolicy(allow_deletes=True)).complete)

    def test_policy_rejection_leaves_no_live_objects_and_partial_acceptance_is_explicit(self):
        with self.assertRaises(ReceiveRejected) as caught:
            self.target.receive((self.command,),self.pack,policy=ReceivePolicy(object_check=lambda oid,raw:False))
        self.assertFalse(caught.exception.result.unpack_ok)
        with self.assertRaises(MissingObject):self.target.objects.read(self.tip)
        rejected=self.target.receive((self.command,),self.pack,policy=ReceivePolicy(ref_check=lambda view,c:b'protected ref'))
        self.assertEqual(rejected.outcomes[0].state,ReceiveState.REJECTED)
        with self.assertRaises(MissingObject):self.target.objects.read(self.tip)
        other=PushCommand(b'refs/heads/accepted',None,self.tip)
        result=self.target.receive((self.command,other),self.pack,
            policy=ReceivePolicy(ref_check=lambda view,c:b'protected ref' if c.name==self.command.name else None))
        self.assertEqual([o.state for o in result.outcomes],[ReceiveState.REJECTED,ReceiveState.ACCEPTED])
        self.assertTrue(self.target.refs.resolve().unborn)
        self.assertEqual(self.target.refs.resolve('refs/heads/accepted').oid,self.tip)

    def test_complete_graphs_types_pack_corruption_and_unreferenced_objects_are_checked(self):
        missing=ObjectId('f'*40)
        bad=Commit.create(missing,author=IDENTITY,committer=IDENTITY,message=b'bad tree').to_raw()
        wrong=Commit.create(self.tip,author=IDENTITY,committer=IDENTITY,message=b'wrong tree kind').to_raw()
        for raw,extras in ((bad,()),(wrong,(self.source.objects.read(self.tip),))):
            data,_,_=fixture((raw,*extras))
            command=PushCommand(b'refs/heads/main',None,raw.oid())
            with self.assertRaises(ReceiveRejected):self.target.receive((command,),data,policy=ReceivePolicy())
        blob=Blob(b'not a commit').to_raw();data,_,_=fixture((blob,))
        with self.assertRaises(ReceiveRejected):self.target.receive((PushCommand(self.command.name,None,blob.oid()),),data,policy=ReceivePolicy())
        for malformed in (self.pack[:-1],self.pack[:-1]+bytes([self.pack[-1]^1])):
            with self.assertRaises(ReceiveRejected):self.target.receive((self.command,),malformed,policy=ReceivePolicy())
        # An unrelated bad graph cannot be hidden behind a valid selected tip.
        raws=tuple(raw for _,raw in self._closure())
        data,_,_=fixture((*raws,bad))
        with self.assertRaises(ReceiveRejected):self.target.receive((self.command,),data,policy=ReceivePolicy())
        self.assertTrue(self.target.refs.resolve().unborn)
        with self.assertRaises(MissingObject):self.target.objects.read(self.tip)

    def _closure(self):
        from gitdx import reachable_objects
        return reachable_objects(self.source.objects,(self.tip,))

    def test_pack_graph_quarantine_and_wire_quotas(self):
        cases=(ReceiveLimits(transfer=TransferLimits(pack=PackLimits(max_objects=1))),
               ReceiveLimits(graph=PackWriteLimits(max_objects=1)),
               ReceiveLimits(graph=PackWriteLimits(max_edges=1)),
               ReceiveLimits(graph=PackWriteLimits(max_bytes=1)),
               ReceiveLimits(max_quarantine_bytes=1))
        for limits in cases:
            with self.subTest(limits=limits),self.assertRaises(ReceiveRejected):
                self.target.receive((self.command,),self.pack,policy=ReceivePolicy(),limits=limits)
        self.assertEqual(list(self.target.objects.path.glob('incoming-*')),[])
        self.assertTrue(self.target.refs.resolve().unborn)

    def test_stale_refs_and_policy_are_rechecked_after_object_promotion(self):
        calls=0
        def changed(view,command):
            nonlocal calls
            calls+=1
            return None if calls==1 else b'policy changed'
        result=self.target.receive((self.command,),self.pack,policy=ReceivePolicy(ref_check=changed))
        self.assertEqual(result.outcomes[0].reason,b'policy changed');self.assertTrue(self.target.refs.resolve().unborn)
        competing=commit_tree(self.target,{b'file':b'competing'})
        original=ObjectQuarantine.promote
        def publish_then_race(incoming):
            original(incoming);self.target.refs.update('refs/heads/main',competing,expected=None)
        with patch.object(ObjectQuarantine,'promote',publish_then_race):
            result=self.target.receive((self.command,),self.pack,policy=ReceivePolicy())
        self.assertEqual(result.outcomes[0].reason,b'stale reference')
        self.assertEqual(self.target.refs.resolve().oid,competing)

    def test_object_and_ref_faults_preserve_explicit_completed_and_unknown_outcomes(self):
        other=PushCommand(b'refs/heads/other',None,self.tip)
        original=FilesRefStore.update
        def fail_after_publication(store,name,*args,**kwargs):
            result=original(store,name,*args,**kwargs)
            if name=='refs/heads/other':raise OSError('injected after publication')
            return result
        with patch.object(FilesRefStore,'update',fail_after_publication):
            with self.assertRaises(ReceiveInterrupted) as caught:
                self.target.receive((self.command,other),self.pack,policy=ReceivePolicy())
        result=caught.exception.result
        self.assertEqual([o.state for o in result.outcomes],[ReceiveState.ACCEPTED,ReceiveState.UNKNOWN])
        self.assertEqual(self.target.refs.resolve('refs/heads/other').oid,self.tip)
        self.assertFalse(result.cleanup_pending)
        destination=Repository.init(self.root/'failed',bare=True)
        with patch.object(type(destination.objects),'write',side_effect=OSError('object publication failed')):
            with self.assertRaises(ReceiveInterrupted) as caught:destination.receive((self.command,),self.pack,policy=ReceivePolicy())
        self.assertEqual(caught.exception.result.outcomes[0].state,ReceiveState.NOT_ATTEMPTED)
        self.assertTrue(destination.refs.resolve().unborn)

    def test_cancellation_after_one_ref_and_cleanup_failure_report_progress(self):
        event=threading.Event();original=FilesRefStore.update
        def cancel_after(store,*args,**kwargs):
            result=original(store,*args,**kwargs);event.set();return result
        other=PushCommand(b'refs/heads/other',None,self.tip)
        with patch.object(FilesRefStore,'update',cancel_after):
            with self.assertRaises(ReceiveInterrupted) as caught:
                self.target.receive((self.command,other),self.pack,policy=ReceivePolicy(),cancel=event)
        self.assertEqual([o.state for o in caught.exception.result.outcomes],[ReceiveState.ACCEPTED,ReceiveState.NOT_ATTEMPTED])
        destination=Repository.init(self.root/'cleanup',bare=True)
        with patch('gitdx.quarantine.shutil.rmtree',side_effect=OSError('cleanup interrupted')):
            result=destination.receive((self.command,),self.pack,policy=ReceivePolicy())
        self.assertTrue(result.complete);self.assertTrue(result.cleanup_pending);self.assertEqual(result.cleanup_error,'OSError')
        self.assertEqual(len(list(destination.objects.path.glob('incoming-*'))),1)

    def test_foreign_quarantine_identity_is_never_promoted_or_deleted(self):
        with ObjectQuarantine(self.target) as incoming:
            incoming.receive(self.pack);incoming.validate((self.tip,))
            path=incoming.path;moved=path.with_name('retained-owned');path.rename(moved)
            path.mkdir();(path/'owner').write_bytes((moved/'owner').read_bytes());(path/'keep').write_bytes(b'foreign')
            with self.assertRaises(QuarantineOwnershipError):incoming.promote()
        self.assertEqual((path/'keep').read_bytes(),b'foreign')
        self.assertTrue(moved.exists());self.assertTrue(incoming.cleanup_pending)
        with self.assertRaises(MissingObject):self.target.objects.read(self.tip)
        with self.assertRaises(ValueError):incoming.read(self.tip)

    def test_namespace_format_profile_and_active_hooks_refused_without_execution(self):
        for commands in ((self.command,self.command),
                         (PushCommand(b'refs/heads/a',None,self.tip),PushCommand(b'refs/heads/a/b',None,self.tip)),
                         (PushCommand(b'refs/heads/Main',None,self.tip),self.command)):
            with self.assertRaises(InvalidRef):self.target.receive(commands,self.pack,policy=ReceivePolicy())
        with self.assertRaises(InvalidObject):self.target.receive((self.command,),policy=ReceivePolicy())
        hooks=self.target.git_dir/'hooks';hooks.mkdir();(hooks/'update').write_bytes(b'not executed')
        with patch('subprocess.Popen',side_effect=AssertionError('hook executed')):
            with self.assertRaises(UnsupportedFormat):self.target.receive((self.command,),self.pack,policy=ReceivePolicy())


class ReceiveNativeTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_pack_receive_and_fsck_both_hash_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                source=Repository.init(Path(tmp)/'source',bare=True,object_format=format)
                tip=commit_tree(source,{b'file':b'native compatible'})
                source.refs.update('refs/heads/main',tip,expected=None)
                data=oracle.git(source.git_dir,'pack-objects','--stdout','--revs',data=str(tip).encode()+b'\n')
                target=Repository.init(Path(tmp)/'target',bare=True,object_format=format)
                with patch.dict(os.environ,{'PATH':''}),patch('subprocess.Popen',side_effect=AssertionError('runtime helper')):
                    result=target.receive((PushCommand(b'refs/heads/main',None,tip),),data,policy=ReceivePolicy())
                self.assertTrue(result.complete)
                oracle.git(target.git_dir,'fsck','--strict','--no-dangling')
                self.assertEqual(oracle.git(target.git_dir,'rev-parse','HEAD').strip().decode(),str(tip))
