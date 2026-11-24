from dataclasses import replace
from pathlib import Path
import hashlib
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (Capability, InvalidObject, InvalidRef, LocalTransport, MissingObject, ObjectFormat, ObjectFormatMismatch,
                   ProtocolError, PushCommand, PushState, PushUncertain, PushUpdate, Repository, TransferCancelled,
                   TransferControl, UnsupportedFormat, encode_push_request, negotiate_push)
import test_objects as oracle
from test_checkout_plan import commit_tree


def snapshot(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


class LocalPushTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source=Repository.init(self.root/'source');self.target=Repository.init(self.root/'target.git',bare=True)
        self.tip=commit_tree(self.source,{b'file':b'data'});self.source.refs.update('refs/heads/main',self.tip,expected=None)
        (self.source.work_tree/'untracked').write_bytes(b'preserved local draft')
        self.transport=LocalTransport(self.target)

    def propose(self,new=None,old=None,name='refs/heads/main',force=False):
        return PushUpdate(name,new,expected_old=old,force=force)

    def test_create_update_delete_and_source_preservation_without_git(self):
        before=snapshot(self.source.work_tree)
        with patch.dict(os.environ,{'PATH':''}):
            result=self.source.push(self.transport,(self.propose(self.tip),))
        self.assertTrue(result.complete);self.assertFalse(result.atomic);self.assertFalse(result.peer_atomic)
        self.assertEqual(snapshot(self.source.work_tree),before)
        second=commit_tree(self.source,{b'file':b'new'},parents=(self.tip,));before=snapshot(self.source.work_tree)
        self.assertTrue(self.source.push(self.transport,(self.propose(second,self.tip),)).complete)
        self.assertTrue(self.source.push(self.transport,(self.propose(self.tip,name='refs/tags/copy'),)).complete)
        self.assertTrue(self.source.push(self.transport,(self.propose(None,self.tip,name='refs/tags/copy'),)).complete)
        self.assertEqual(self.target.refs.resolve().oid,second);self.assertIsNone(self.target.refs.read('refs/tags/copy'))
        self.assertEqual(snapshot(self.source.work_tree),before);self.assertFalse(list(self.target.objects.path.glob('incoming-*')))

    def test_stale_and_non_fast_forward_preflight_never_sends_batch(self):
        self.source.push(self.transport,(self.propose(self.tip),))
        unrelated=commit_tree(self.source,{b'file':b'unrelated'});before=snapshot(self.source.work_tree)
        result=self.source.push(self.transport,(self.propose(unrelated,self.tip),self.propose(self.tip,name='refs/heads/other')))
        self.assertEqual([o.state for o in result.outcomes],[PushState.REJECTED,PushState.NOT_SENT])
        self.assertEqual(self.target.refs.resolve().oid,self.tip);self.assertIsNone(self.target.refs.read('refs/heads/other'))
        result=self.source.push(self.transport,(self.propose(self.tip),))
        self.assertEqual(result.outcomes[0].reason,'stale expected-old value')
        self.assertTrue(self.source.push(self.transport,(self.propose(unrelated,self.tip,force=True),)).complete)
        self.assertEqual(snapshot(self.source.work_tree),before)

    def test_remote_policy_partial_acceptance_and_deletion_rejection(self):
        self.source.push(self.transport,(self.propose(self.tip),));other=commit_tree(self.source,{b'other':b'data'})
        with (self.target.git_dir/'config').open('ab') as stream:stream.write(b'[receive]\ndenyNonFastForwards=true\ndenyDeletes=true\n')
        before=snapshot(self.source.work_tree)
        result=self.source.push(self.transport,(self.propose(other,self.tip,force=True),self.propose(other,name='refs/heads/accepted')))
        self.assertEqual([o.state for o in result.outcomes],[PushState.REJECTED,PushState.ACCEPTED])
        self.assertEqual(self.target.refs.resolve().oid,self.tip);self.assertEqual(self.target.refs.resolve('refs/heads/accepted').oid,other)
        self.assertEqual(self.source.push(self.transport,(self.propose(None,self.tip),)).outcomes[0].state,PushState.REJECTED)
        self.assertEqual(snapshot(self.source.work_tree),before)

    def test_stale_writer_between_discovery_and_publication(self):
        self.source.push(self.transport,(self.propose(self.tip),));new=commit_tree(self.source,{b'file':b'new'},parents=(self.tip,))
        competing=commit_tree(self.target,{b'file':b'competing'});base=self.transport;target=self.target
        class Racing:
            discover_push=base.discover_push
            def send_pack(self,ad,wire,control):
                target.refs.update('refs/heads/main',competing,expected=ad.refs[0].oid)
                return base.send_pack(ad,wire,control)
        before=snapshot(self.source.work_tree);result=self.source.push(Racing(),(self.propose(new,self.tip),))
        self.assertEqual(result.outcomes[0].state,PushState.REJECTED);self.assertEqual(self.target.refs.resolve().oid,competing)
        self.assertEqual(snapshot(self.source.work_tree),before)

    def test_disconnect_after_acceptance_and_cancel_never_claim_rejection(self):
        base=self.transport
        class Disconnected:
            discover_push=base.discover_push
            def send_pack(self,ad,wire,control):base.send_pack(ad,wire,control);raise OSError('lost report')
        before=snapshot(self.source.work_tree)
        with self.assertRaises(PushUncertain) as caught:self.source.push(Disconnected(),(self.propose(self.tip),))
        self.assertEqual(caught.exception.result.outcomes[0].state,PushState.UNKNOWN)
        self.assertEqual(self.target.refs.resolve().oid,self.tip);self.assertEqual(snapshot(self.source.work_tree),before)
        event=threading.Event();event.set()
        with self.assertRaises(TransferCancelled):self.source.push(base,(self.propose(None,self.tip),),cancel=event)

    def test_corrupt_pack_does_not_publish_refs_or_live_objects(self):
        base=self.transport
        class Corrupt:
            discover_push=base.discover_push
            def send_pack(self,ad,wire,control):return base.send_pack(ad,wire[:-1]+bytes([wire[-1]^1]),control)
        result=self.source.push(Corrupt(),(self.propose(self.tip),))
        self.assertEqual(result.outcomes[0].reason,'unpack rejected');self.assertTrue(self.target.refs.resolve().unborn)
        with self.assertRaises(MissingObject):self.target.objects.read(self.tip)

    def test_atomic_and_namespace_claims_are_refused_before_mutation(self):
        before=snapshot(self.target.git_dir)
        with self.assertRaises(ProtocolError):self.source.push(self.transport,(self.propose(self.tip),),atomic=True)
        names=('refs/heads/a','refs/heads/a-1','refs/heads/a/b')
        with self.assertRaises(InvalidRef):self.source.push(self.transport,tuple(self.propose(self.tip,name=n) for n in names))
        control=TransferControl();ad=self.transport.discover_push(control);forged=replace(ad,capabilities=(*ad.capabilities,Capability(b'atomic')))
        command=PushCommand(b'refs/heads/main',None,self.tip);n=negotiate_push(forged,self.source.object_format,atomic=True)
        with self.assertRaises(ProtocolError):self.transport.send_pack(forged,encode_push_request(n,(command,),self.source.pack((self.tip,)).pack),control)
        self.assertEqual(snapshot(self.target.git_dir),before)

    def test_nonbare_hooks_and_format_refusals_never_execute_helpers(self):
        with self.assertRaises(UnsupportedFormat):self.source.push(LocalTransport(self.source),(self.propose(self.tip),))
        hooks=self.target.git_dir/'hooks';hooks.mkdir();(hooks/'update').write_bytes(b'never execute')
        with patch('subprocess.Popen',side_effect=AssertionError('helper invoked')):
            with self.assertRaises(UnsupportedFormat):self.source.push(self.transport,(self.propose(self.tip),))
        (hooks/'update').unlink()
        other=Repository.init(self.root/'other.git',bare=True,object_format=ObjectFormat.SHA256)
        with self.assertRaises(ObjectFormatMismatch):self.source.push(LocalTransport(other),(self.propose(self.tip),))
        self.assertTrue(self.target.refs.resolve().unborn)


class LocalPushGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_git_push_both_directions_and_hashes(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=Repository.init(root/'source',bare=True,object_format=format)
                target=Repository.init(root/'target.git',bare=True,object_format=format)
                tip=commit_tree(source,{b'file':b'initial'});source.refs.update('refs/heads/main',tip,expected=None)
                with patch.dict(os.environ,{'PATH':''}):
                    self.assertTrue(source.push(LocalTransport(target),(PushUpdate('refs/heads/main',tip,expected_old=None),)).complete)
                oracle.git(target.git_dir,'fsck','--full','--no-dangling')
                native=root/'native';oracle.git(root,'clone','--no-local',str(target.git_dir),str(native))
                (native/'file').write_bytes(b'native update');oracle.git(native,'add','file')
                oracle.git(native,'-c','user.name=Qualification','-c','user.email=qualification@example.invalid','commit','-m','native update')
                new=oracle.git(native,'rev-parse','HEAD').strip().decode();oracle.git(native,'push','origin','main')
                self.assertEqual(str(target.refs.resolve().oid),new);self.assertEqual(target.log()[0].commit.parents,(tip,))
