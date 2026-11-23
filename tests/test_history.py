from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitdx import (Blob, Commit, DirectRef, HistoryLimits, IndexEntry, InvalidObject,
                   ObjectFormat, ObjectLimitExceeded, RefConflict, Repository, Tree, UnsupportedFormat)
import test_objects as oracle
from test_objects import IDENTITY, git


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.repo = Repository.init(self.root)

    def commit(self, expected=None, **kwargs):
        return self.repo.commit(author=IDENTITY, committer=IDENTITY, message=b"message\n", expected_head=expected, **kwargs)

    def test_identity_is_explicit_and_rejections_preserve_head_and_index(self):
        before = (self.repo.git_dir/"HEAD").read_bytes()
        for bad in (None, b"Name <email>", b"Name <email> 1 +2460", b"Name\n <email> 1 +0000", b" <email> 1 +0000"):
            with self.assertRaises(InvalidObject):self.repo.commit(author=bad, committer=IDENTITY, message=b"x", expected_head=None)
        self.assertEqual((self.repo.git_dir/"HEAD").read_bytes(), before)
        self.assertFalse(self.repo.index.path.exists())

    def test_stale_head_and_changed_symbolic_target_preserve_authority(self):
        first = self.commit()
        with self.assertRaises(RefConflict):self.commit()
        self.repo.refs.update("refs/heads/other", first, expected=None)
        original = self.repo.objects.write
        def retarget(raw):
            oid = original(raw)
            if raw.kind.value == "commit":
                self.repo.refs.set_symbolic("HEAD", "refs/heads/other", expected=self.repo.refs.read("HEAD"))
            return oid
        with patch.object(type(self.repo.objects), "write", lambda store, raw: retarget(raw)):
            with self.assertRaises(RefConflict):self.commit(first)
        self.assertEqual(self.repo.refs.resolve().oid, first)
        self.assertEqual(self.repo.refs.read("refs/heads/main"), DirectRef(first))
        self.assertFalse((self.repo.git_dir/"HEAD.lock").exists())

    def test_unmerged_index_missing_blob_wrong_type_and_limits(self):
        blob = self.repo.objects.write(Blob(b"x").to_raw())
        with self.repo.index.transaction() as tx:tx.replace((IndexEntry(b"file", blob, stage=2),));tx.commit()
        with self.assertRaises(UnsupportedFormat):self.commit()
        tree = self.repo.objects.write(Tree().to_raw())
        with self.repo.index.transaction() as tx:tx.replace((IndexEntry(b"file", tree),));tx.commit()
        with self.assertRaises(InvalidObject):self.commit()
        with self.repo.index.transaction() as tx:tx.replace((IndexEntry(b"dir/file", blob),));tx.commit()
        with self.assertRaises(ObjectLimitExceeded):self.commit(limits=HistoryLimits(max_depth=1))
        self.assertTrue(self.repo.refs.resolve().unborn)

    def test_transient_checkout_during_snapshot_cannot_select_another_branch(self):
        (self.root/"file").write_bytes(b"staged");self.repo.add("file")
        first = self.commit();self.repo.refs.update("refs/heads/other",first,expected=None)
        before = self.repo.index.path.read_bytes()
        kind = type(self.repo.refs);original = kind.resolve;injected = False
        def transient(store, name="HEAD"):
            nonlocal injected
            if name != "HEAD" or injected:return original(store,name)
            injected = True
            captured = store.read("HEAD")
            store.set_symbolic("HEAD","refs/heads/other",expected=captured)
            result = original(store,"HEAD")
            store.set_symbolic("HEAD",captured.target,expected=store.read("HEAD"))
            return result
        with patch.object(kind,"resolve",transient), patch.object(type(self.repo.objects),"write",side_effect=AssertionError("objects written before a consistent HEAD snapshot")):
            with self.assertRaises(RefConflict):self.commit(first)
        self.assertEqual(self.repo.refs.read("refs/heads/main"),DirectRef(first))
        self.assertEqual(self.repo.refs.read("refs/heads/other"),DirectRef(first))
        self.assertEqual(self.repo.index.path.read_bytes(),before)

    def test_iterative_deep_history_deduplication_and_budgets(self):
        tree = self.repo.objects.write(Tree().to_raw());parent = None
        for _ in range(1100):
            parent = self.repo.objects.write(Commit.create(tree, author=IDENTITY, committer=IDENTITY, message=b"x", parents=() if parent is None else (parent,)).to_raw())
        self.assertEqual(len(tuple(self.repo.walk_history((parent, parent)))), 1100)
        self.assertEqual(len(self.repo.log(parent, max_count=2)), 2)
        with self.assertRaises(ObjectLimitExceeded):tuple(self.repo.walk_history((parent,), limits=HistoryLimits(max_commits=10)))
        with self.assertRaises(ObjectLimitExceeded):self.repo.log(parent, limits=HistoryLimits(max_bytes=1))

    def test_detached_commits_and_no_git_cli(self):
        first = self.commit();self.repo.refs.detach_head(first, expected=self.repo.refs.read("HEAD"))
        second = self.commit(first)
        self.assertEqual(self.repo.refs.read("HEAD"), DirectRef(second))
        self.assertEqual(self.repo.refs.read("refs/heads/main"), DirectRef(first))
        command = [sys.executable, "-I", "-m", "gitdx", "-C", str(self.root)]
        result = subprocess.run(command+["commit", "--author", IDENTITY.decode(), "--committer", IDENTITY.decode(), "--expected-head", str(second), "-m", "CLI"], env=dict(os.environ, PATH=""), capture_output=True, check=True)
        log = subprocess.run(command+["log", "--max-count", "1"], env=dict(os.environ, PATH=""), capture_output=True, check=True)
        self.assertEqual(json.loads(log.stdout)["oid"], result.stdout.decode().strip())


class HistoryGitTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_root_repeat_merge_trees_refs_and_fsck_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);repo = Repository.init(root, object_format=format)
                (root/"dir").mkdir();(root/"dir/file").write_bytes(b"binary\0\xff")
                (root/"dir.x").write_bytes(b"other");repo.add(".")
                index = repo.index.path.read_bytes()
                first = repo.commit(author=IDENTITY, committer=IDENTITY, message=b"root\n", expected_head=None)
                self.assertEqual(repo.index.path.read_bytes(), index)
                self.assertEqual(git(root, "write-tree").strip().decode(), str(repo.log()[0].commit.tree))
                second = repo.commit(author=IDENTITY, committer=IDENTITY, message=b"repeat\n", expected_head=first)
                side = repo.objects.write(Commit.create(repo.log()[0].commit.tree, author=IDENTITY, committer=IDENTITY, message=b"side\n", parents=(first,)).to_raw())
                merge = repo.commit(author=IDENTITY, committer=IDENTITY, message=b"merge\n", expected_head=second, parents=(second, side))
                self.assertEqual(git(root, "show", "-s", "--format=%P", str(merge)).strip().decode().split(), [str(second), str(side)])
                self.assertEqual({e.oid for e in repo.log()}, {first, second, side, merge})
                repo.refs.update("refs/tags/root", first, expected=None)
                git(root, "pack-refs", "--all")
                self.assertEqual(repo.branches(), (("refs/heads/main", merge),))
                self.assertEqual(repo.tags(), (("refs/tags/root", first),))
                git(root, "fsck", "--strict", "--no-reflogs")
