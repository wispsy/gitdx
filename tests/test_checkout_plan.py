from dataclasses import FrozenInstanceError
from pathlib import Path
import os
import tempfile
import unittest

from gitdx import (Blob, CheckoutConflict, CheckoutLimits, Commit, Index, ObjectFormat,
                   ObjectLimitExceeded, Repository, Tree, TreeEntry, UnsafePath, UnsupportedFormat)
from gitdx.working_tree import portable_path
import test_objects as oracle
from test_objects import IDENTITY,git


def commit_tree(repo, files, parents=()):
    """Build a fixture tree without writing through the working directory."""
    directories={b"":{}}
    for path,value in files.items():
        mode,data = value if isinstance(value,tuple) else (0o100644,value)
        parts=path.split(b"/")
        for i in range(len(parts)-1):
            base=b"/".join(parts[:i]);child=b"/".join(parts[:i+1]);directories.setdefault(child,{})
            directories[base][parts[i]]=child
        directories[b"/".join(parts[:-1])][parts[-1]]=(mode,repo.objects.write(Blob(data).to_raw()))
    trees={}
    for path in sorted(directories,key=lambda p:(p.count(b"/")+bool(p),p),reverse=True):
        entries=[TreeEntry(0o40000,name,trees[value]) if isinstance(value,bytes) else TreeEntry(value[0],name,value[1]) for name,value in directories[path].items()]
        trees[path]=repo.objects.write(Tree(tuple(entries),repo.object_format).to_raw())
    return repo.objects.write(Commit.create(trees[b""],author=IDENTITY,committer=IDENTITY,message=b"fixture\n",parents=parents).to_raw())


class CheckoutPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository.init(self.root)
        (self.root/"old").write_bytes(b"initial");self.repo.add("old")
        self.first=self.repo.commit(author=IDENTITY,committer=IDENTITY,message=b"initial\n",expected_head=None)

    def target(self,files):
        oid=commit_tree(self.repo,files,parents=(self.first,))
        self.repo.refs.update("refs/heads/next",oid,expected=None)
        return oid

    def test_plan_is_read_only_immutable_and_lists_directory_transitions(self):
        oid=self.target({b"old/child":b"next",b"new":b"payload"})
        index=self.repo.index.path.read_bytes();head=(self.repo.git_dir/"HEAD").read_bytes()
        plan=self.repo.plan_checkout("next")
        self.assertEqual(plan.target,oid)
        self.assertEqual({c.path for c in plan.changes},{b"old",b"old/child",b"new"})
        self.assertEqual(self.repo.index.path.read_bytes(),index)
        self.assertEqual((self.repo.git_dir/"HEAD").read_bytes(),head)
        self.assertEqual((self.root/"old").read_bytes(),b"initial")
        self.assertFalse((self.root/"new").exists())
        with self.assertRaises(FrozenInstanceError):plan.target=None
        self.assertNotIn("payload",repr(plan))

    def test_modified_and_staged_content_need_explicit_policy(self):
        self.target({b"old":b"next"})
        (self.root/"old").write_bytes(b"local")
        with self.assertRaises(CheckoutConflict):self.repo.plan_checkout("next")
        self.repo.plan_checkout("next",overwrite_tracked=True)
        self.repo.add("old")
        with self.assertRaises(CheckoutConflict):self.repo.plan_checkout("next")
        self.repo.plan_checkout("next",overwrite_tracked=True)

    def test_untracked_and_ignored_files_are_protected_with_overwrite(self):
        self.target({b"collision":b"target"})
        (self.root/"collision").write_bytes(b"untracked")
        (self.root/".gitignore").write_bytes(b"collision\n")
        for overwrite in (False,True):
            with self.assertRaises(CheckoutConflict):self.repo.plan_checkout("next",overwrite_tracked=overwrite)
        self.assertEqual((self.root/"collision").read_bytes(),b"untracked")

    def test_tree_metadata_platform_and_case_aliases_are_refused(self):
        for path in (b".GIT/config",b"git~2/config",b".git\xe2\x80\x8c/config",b"name:stream",b"CON/file",b"trail. "):
            with self.subTest(path=path):
                oid=commit_tree(self.repo,{path:b"unsafe"})
                with self.assertRaises(UnsafePath):self.repo.plan_checkout(oid)
        for path in ("COM\u00b9/file",".\uff47\uff49\uff54/config"):
            with self.assertRaises(UnsafePath):portable_path(path)
        oid=commit_tree(self.repo,{b"Old":b"case"})
        with self.assertRaises(UnsafePath):self.repo.plan_checkout(oid)

    def test_symlink_parent_cannot_redirect_checkout(self):
        outside=self.root/"outside";outside.mkdir();(outside/"keep").write_bytes(b"private")
        link=self.root/"link"
        try:link.symlink_to(outside,target_is_directory=True)
        except OSError as error:self.skipTest(str(error))
        oid=self.target({b"link/keep":b"malicious"})
        with self.assertRaises(CheckoutConflict):self.repo.plan_checkout(oid,overwrite_tracked=True)
        self.assertEqual((outside/"keep").read_bytes(),b"private")

    def test_unborn_detached_limits_attributes_and_active_operations(self):
        plan=self.repo.plan_checkout("empty",unborn=True)
        self.assertIsNone(plan.target);self.assertTrue(plan.unborn)
        self.assertEqual(self.repo.plan_checkout(self.first).target,self.first)
        with self.assertRaises(CheckoutConflict):self.repo.plan_checkout("missing")
        with self.assertRaises(ObjectLimitExceeded):self.repo.plan_checkout(self.first,limits=CheckoutLimits(max_bytes=1))
        unsafe=commit_tree(self.repo,{b".gitattributes":b"* filter=external\n"})
        with self.assertRaises(UnsupportedFormat):self.repo.plan_checkout(unsafe)
        (self.repo.git_dir/"MERGE_HEAD").write_text(str(self.first))
        with self.assertRaises(UnsupportedFormat):self.repo.plan_checkout(self.first)


class CheckoutPlanGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_planned_index_matches_stock_git_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                repo=Repository.init(tmp,object_format=format)
                oid=commit_tree(repo,{b"dir/file":b"binary\0\xff",b"exec":(0o100755,b"run")})
                repo.refs.update("refs/heads/next",oid,expected=None)
                plan=repo.plan_checkout("next")
                self.assertIsNone(plan.old_index)
                git(repo.work_tree,"read-tree",str(oid))
                actual=Index.from_bytes(plan.new_index,format)
                self.assertEqual([(e.path,e.mode,e.oid) for e in actual.entries],[(e.path,e.mode,e.oid) for e in repo.index.read().entries])
