from dataclasses import replace
from pathlib import Path
import os
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (CloneIncomplete, DirectRef, FetchInterrupted, LocalTransport, MissingObject, ObjectFormat,
                   ObjectFormatMismatch, Repository, TransferCancelled, TransferControl, TransferError, TransferLimits,
                   TransferTimeout, UnsupportedFormat)
import test_objects as oracle
from test_checkout_plan import commit_tree


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=Repository.init(self.root/'source')
        self.tip=commit_tree(self.source,{b'file':b'data'});self.source.refs.update('refs/heads/main',self.tip,expected=None)
        self.target=Repository.init(self.root/'target')

    def test_fetch_tracking_refs_fast_forward_rewind_policy_and_no_worktree_changes(self):
        transport=LocalTransport(self.source)
        result=self.target.fetch(transport);self.assertTrue(result.complete)
        self.assertEqual(self.target.refs.resolve('refs/remotes/origin/main').oid,self.tip)
        self.assertTrue(self.target.refs.resolve().unborn);self.assertFalse((self.root/'target/file').exists())
        second=commit_tree(self.source,{b'file':b'new'},parents=(self.tip,));self.source.refs.update('refs/heads/main',second,expected=self.tip)
        self.assertTrue(self.target.fetch(transport).complete)
        self.source.refs.update('refs/heads/main',self.tip,expected=second)
        result=self.target.fetch(transport);self.assertFalse(result.complete);self.assertEqual(result.rejected[0].reason,'non-fast-forward')
        self.assertEqual(self.target.refs.resolve('refs/remotes/origin/main').oid,second)
        self.assertTrue(self.target.fetch(transport,allow_rewind=True).complete)

    def test_stale_cas_conflict_never_overwrites_competing_writer(self):
        target=self.target;tip=self.tip;local=LocalTransport(self.source)
        class Racing:
            discover=local.discover
            def fetch_pack(self,ad,wants,control):
                pack=local.fetch_pack(ad,wants,control)
                # Publish a competing valid ref while transfer is still private.
                other=commit_tree(target,{b'file':b'competing'})
                target.refs.update('refs/remotes/origin/main',other,expected=None)
                return pack
        result=target.fetch(Racing());self.assertFalse(result.complete)
        self.assertEqual(result.rejected[0].reason,'RefConflict')
        self.assertNotEqual(target.refs.resolve('refs/remotes/origin/main').oid,tip)

    def test_bad_pack_cancel_deadline_and_format_mismatch_preserve_refs(self):
        local=LocalTransport(self.source)
        class Corrupt:
            discover=local.discover
            def fetch_pack(self,ad,wants,control):return local.fetch_pack(ad,wants,control)[:-1]
        with self.assertRaises(ValueError):self.target.fetch(Corrupt())
        self.assertIsNone(self.target.refs.read('refs/remotes/origin/main'))
        with self.assertRaises(MissingObject):self.target.objects.read(self.tip)
        event=threading.Event();event.set()
        with self.assertRaises(TransferCancelled):self.target.fetch(local,cancel=event)
        control=TransferControl();control.deadline=0
        with self.assertRaises(TransferTimeout):control.check()
        other=Repository.init(self.root/'other',object_format=ObjectFormat.SHA256)
        with self.assertRaises(ObjectFormatMismatch):other.fetch(local)
        self.assertIsNone(other.refs.read('refs/remotes/origin/main'))
        with self.assertRaises(TransferError):self.target.fetch(local,branches=('missing',))

    def test_partial_ref_outcomes_are_explicit_and_pending_checkout_refuses(self):
        self.source.refs.update('refs/heads/next',self.tip,expected=None)
        original=type(self.target.refs).update
        def interrupted(store,name,*args,**kwargs):
            if store.path==self.target.git_dir and name.endswith('/next'):raise OSError('injected publication failure')
            return original(store,name,*args,**kwargs)
        with patch('gitdx.refs.FilesRefStore.update',interrupted):
            with self.assertRaises(FetchInterrupted) as caught:self.target.fetch(LocalTransport(self.source))
        self.assertEqual([r.name for r in caught.exception.updated],['refs/remotes/origin/main'])
        self.assertIsNone(self.target.refs.read('refs/remotes/origin/next'))
        (self.target.git_dir/'checkout-state').mkdir()
        with self.assertRaises(UnsupportedFormat):self.target.fetch(LocalTransport(self.source))

    def test_clone_empty_detached_bare_create_only_and_incomplete_state(self):
        for format in ObjectFormat:
            source=Repository.init(self.root/('empty-'+format.value),object_format=format,initial_branch='new')
            empty=Repository.clone(LocalTransport(source),self.root/('clone-'+format.value))
            self.assertEqual(empty.object_format,format);self.assertTrue(empty.refs.resolve().unborn)
            self.assertEqual(empty.refs.resolve().name,'refs/heads/new')
        self.source.refs.detach_head(self.tip,expected=self.source.refs.read('HEAD'))
        detached=Repository.clone(LocalTransport(self.source),self.root/'detached')
        self.assertTrue(detached.refs.resolve().detached);self.assertEqual((detached.work_tree/'file').read_bytes(),b'data')
        bare=Repository.clone(LocalTransport(self.source),self.root/'bare',bare=True)
        self.assertTrue(bare.bare);self.assertEqual(bare.refs.resolve('refs/heads/main').oid,self.tip)
        with self.assertRaises(FileExistsError):Repository.clone(LocalTransport(self.source),self.root/'detached')
        local=LocalTransport(self.source)
        class Broken:
            discover=local.discover
            def fetch_pack(self,*args):raise TransferError('injected interruption')
        path=self.root/'incomplete'
        with self.assertRaises(CloneIncomplete) as caught:Repository.clone(Broken(),path)
        self.assertEqual(caught.exception.path,path)
        self.assertTrue(Repository.open(path).refs.resolve().unborn)
        self.assertFalse((path/'file').exists())

    def test_cli_clone_and_fetch_without_git_or_implicit_transport(self):
        env=dict(os.environ,PATH='');path=self.root/'cli'
        args=[sys.executable,'-m','gitdx','clone',str(self.source.work_tree),str(path),'--transport','local']
        result=subprocess.run(args,capture_output=True,env=env,check=True)
        self.assertEqual(json.loads(result.stdout)['head'],str(self.tip))
        result=subprocess.run([sys.executable,'-m','gitdx','-C',str(path),'fetch',str(self.source.work_tree),'--transport','local'],capture_output=True,env=env,check=True)
        self.assertTrue(json.loads(result.stdout)['complete'])
        result=subprocess.run(args[:-2],capture_output=True,env=env)
        self.assertEqual(result.returncode,2)


class TransferGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_git_to_python_and_reverse_clone_fetch_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=Repository.init(root/'source',object_format=format)
                first=commit_tree(source,{b'file':b'initial'});source.refs.update('refs/heads/main',first,expected=None)
                oracle.git(source.git_dir,'gc','--prune=now')
                with patch.dict(os.environ,{'PATH':''}):
                    clone=Repository.clone(LocalTransport(source),root/'python')
                self.assertEqual(clone.refs.resolve().oid,first);self.assertEqual(oracle.git(clone.work_tree,'status','--porcelain'),b'')
                native=root/'native'
                oracle.git(root,'clone','--no-local',str(clone.work_tree),str(native))
                self.assertEqual(oracle.git(native,'rev-parse','HEAD').strip().decode(),str(first))
                second=commit_tree(source,{b'file':b'new'},parents=(first,));source.refs.update('refs/heads/main',second,expected=first)
                self.assertTrue(clone.fetch(LocalTransport(source)).complete)
                # Fetch never changes the active local branch/worktree.
                self.assertEqual(clone.refs.resolve().oid,first)
                self.assertEqual(clone.refs.resolve('refs/remotes/origin/main').oid,second)
                oracle.git(native,'fetch',str(clone.work_tree),'refs/remotes/origin/main:refs/remotes/python/main')
                self.assertEqual(oracle.git(native,'rev-parse','refs/remotes/python/main').strip().decode(),str(second))
