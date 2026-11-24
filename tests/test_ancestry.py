import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitdx import (Blob, Commit, InvalidObject, MissingObject, ObjectFormat, ObjectId, ObjectLimitExceeded,
                   Repository, RevisionLimits, Tag, Tree, UnsupportedFormat)
import test_objects as oracle
from test_objects import IDENTITY, git
from test_revisions import commit


def criss_cross(repo):
    root = commit(repo,message=b'root');a = commit(repo,parents=(root,),message=b'a');b = commit(repo,parents=(root,),message=b'b')
    left = commit(repo,parents=(a,b),message=b'left');right = commit(repo,parents=(b,a),message=b'right')
    detached = commit(repo,message=b'disconnected')
    return root,a,b,left,right,detached


class AncestryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.repo = Repository.init(self.root)

    def test_criss_cross_all_bases_dominated_roots_and_disconnected(self):
        root,a,b,left,right,detached = criss_cross(self.repo)
        expected = tuple(sorted((a,b),key=str))
        self.assertEqual(self.repo.merge_bases(left,right),expected)
        self.assertEqual(self.repo.merge_bases(right,left),expected)
        self.assertEqual(self.repo.merge_bases(left,a),(a,))
        self.assertEqual(self.repo.merge_bases(left,left),(left,))
        self.assertEqual(self.repo.merge_bases(left,detached),())
        self.assertTrue(self.repo.is_ancestor(root,right));self.assertTrue(self.repo.is_ancestor(right,right))
        self.assertFalse(self.repo.is_ancestor(left,right));self.assertFalse(self.repo.is_ancestor(detached,right))
        tag = self.repo.objects.write(Tag.create(left,target_type='commit',name=b'tag',message=b'tag').to_raw())
        self.assertEqual(self.repo.merge_bases(tag,right),expected)
        self.assertTrue(self.repo.is_ancestor(a,tag))

    def test_deep_history_is_iterative_and_shared_limits_are_strict(self):
        first = commit(self.repo);tip = first
        for n in range(1050):tip = commit(self.repo,parents=(tip,),message=str(n).encode())
        self.assertTrue(self.repo.is_ancestor(first,tip))
        self.assertEqual(self.repo.merge_bases(tip,first),(first,))
        for limits in (RevisionLimits(max_objects=10),RevisionLimits(max_bytes=100),RevisionLimits(max_steps=10)):
            with self.assertRaises(ObjectLimitExceeded):self.repo.merge_bases(first,tip,limits=limits)
            with self.assertRaises(ObjectLimitExceeded):self.repo.is_ancestor(first,tip,limits=limits)

    def test_missing_wrong_type_and_cycles_never_mean_negative_results(self):
        first = commit(self.repo);missing = ObjectId('1'*40)
        broken = commit(self.repo,parents=(first,missing))
        with self.assertRaises(MissingObject):self.repo.is_ancestor(first,broken)
        with self.assertRaises(MissingObject):self.repo.merge_bases(first,broken)
        blob = self.repo.objects.write(Blob(b'x').to_raw());wrong = commit(self.repo,parents=(blob,))
        with self.assertRaises(InvalidObject):self.repo.is_ancestor(first,wrong)
        with self.assertRaises(InvalidObject):self.repo.merge_bases(blob,first)
        tree = self.repo.objects.write(Tree().to_raw())
        cycle = Commit.create(tree,author=IDENTITY,committer=IDENTITY,message=b'cycle',parents=(first,)).to_raw()
        original = self.repo.objects.read
        with patch.object(type(self.repo.objects),'read',lambda store,oid:cycle if oid==first else original(oid)):
            with self.assertRaises(InvalidObject):self.repo.merge_bases(first,first)

    def test_no_git_cli_states_and_replacement_refusal(self):
        root,a,b,left,right,detached = criss_cross(self.repo)
        self.repo.refs.update('refs/heads/main',left,expected=None)
        with patch('subprocess.run',side_effect=AssertionError('runtime helper executed')):
            self.assertTrue(self.repo.is_ancestor(root,left))
            self.assertEqual(set(self.repo.merge_bases(left,right)),{a,b})
        command = [sys.executable,'-I','-m','gitdx','-C',str(self.root)]
        env = dict(os.environ,PATH='')
        for args, expected in ((['is-ancestor',str(root),str(left)],0),(['is-ancestor',str(left),str(right)],1),
                               (['is-ancestor','missing',str(left)],2),(['merge-base',str(left),str(right)],0),
                               (['merge-base',str(left),str(detached)],1),(['merge-base','missing',str(left)],2)):
            result = subprocess.run(command+args,env=env,capture_output=True)
            self.assertEqual(result.returncode,expected,result.stderr)
            if args[0]=='merge-base' and expected==0:self.assertEqual(result.stdout.splitlines(),[str(oid).encode() for oid in sorted((a,b),key=str)])
        self.repo.refs.update('refs/replace/'+str(root),left,expected=None)
        with self.assertRaises(UnsupportedFormat):self.repo.is_ancestor(root,left)


class AncestryGitTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_all_native_merge_bases_and_ancestry_both_formats_after_gc(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);repo = Repository.init(root,object_format=format)
                first,a,b,left,right,detached = criss_cross(repo)
                repo.refs.update('refs/heads/main',left,expected=None);repo.refs.update('refs/heads/right',right,expected=None)
                repo.refs.update('refs/heads/disconnected',detached,expected=None)
                git(root,'gc','--prune=now')
                for one,two in ((left,right),(right,left),(first,left),(a,left),(left,left)):
                    expected = set(git(root,'merge-base','--all',str(one),str(two)).splitlines())
                    self.assertEqual({str(oid).encode() for oid in repo.merge_bases(one,two)},expected)
                native = subprocess.run(['git','-C',str(root),'merge-base','--all',str(left),str(detached)],capture_output=True)
                self.assertEqual(native.returncode,1);self.assertEqual(repo.merge_bases(left,detached),())
                for one,two in ((first,left),(left,left),(left,right),(left,detached),(b,right)):
                    native = subprocess.run(['git','-C',str(root),'merge-base','--is-ancestor',str(one),str(two)],capture_output=True)
                    self.assertIn(native.returncode,(0,1));self.assertEqual(repo.is_ancestor(one,two),native.returncode==0)

    def test_native_deep_chain_and_nonrecursive_parent_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);repo = Repository.init(root);first = commit(repo);tip = first
            for n in range(1010):tip = commit(repo,parents=(tip,),message=str(n).encode())
            repo.refs.update('refs/heads/main',tip,expected=None)
            self.assertEqual(repo.resolve('HEAD~1010'),first)
            self.assertEqual(git(root,'rev-parse','HEAD~1010').strip().decode(),str(first))
            self.assertEqual(git(root,'merge-base','--all',str(first),str(tip)).strip().decode(),str(repo.merge_bases(first,tip)[0]))
