from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitdx import CheckoutConflict, CheckoutRecoveryRequired, DirectRef, ObjectFormat, Repository, SymbolicRef, UnsupportedFormat
import gitdx.checkout_transaction as transaction
import test_objects as oracle
from test_objects import IDENTITY,git
from test_checkout_plan import commit_tree


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository.init(self.root)
        (self.root/"old").write_bytes(b"initial");self.repo.add("old")
        self.first=self.repo.commit(author=IDENTITY,committer=IDENTITY,message=b"initial\n",expected_head=None)
        self.target=commit_tree(self.repo,{b"old":b"new",b"dir/file":b"added"},parents=(self.first,))
        self.repo.refs.update("refs/heads/next",self.target,expected=None)

    def fail_file_write(self):
        original=transaction._atomic_leaf;count=0
        def interrupted(*args):
            nonlocal count
            count+=1
            if count==2:raise OSError("injected second file failure")
            return original(*args)
        with patch.object(transaction,"_atomic_leaf",interrupted):
            with self.assertRaises(CheckoutRecoveryRequired):self.repo.apply_checkout(self.repo.plan_checkout("next"))

    def test_apply_clean_branch_detached_unborn_and_explicit_tracked_overwrite(self):
        (self.root/"keep").write_bytes(b"untracked")
        result=self.repo.apply_checkout(self.repo.plan_checkout("next"))
        self.assertEqual(result.target,self.target);self.assertEqual(self.repo.refs.read("HEAD"),SymbolicRef("refs/heads/next"))
        self.assertEqual((self.root/"old").read_bytes(),b"new");self.assertEqual((self.root/"keep").read_bytes(),b"untracked")
        self.repo.apply_checkout(self.repo.plan_checkout(self.first))
        self.assertEqual(self.repo.refs.read("HEAD"),DirectRef(self.first));self.assertFalse((self.root/"dir").exists())
        (self.root/"old").write_bytes(b"local")
        with self.assertRaises(CheckoutConflict):self.repo.plan_checkout("next")
        self.repo.apply_checkout(self.repo.plan_checkout("next",overwrite_tracked=True))
        self.repo.apply_checkout(self.repo.plan_checkout("orphan",unborn=True))
        self.assertTrue(self.repo.refs.resolve().unborn);self.assertEqual(self.repo.index.read().entries,())
        self.assertFalse((self.root/"old").exists());self.assertEqual((self.root/"keep").read_bytes(),b"untracked")

    def test_stale_and_forged_plans_never_change_working_content(self):
        plan=self.repo.plan_checkout("next")
        (self.root/"old").write_bytes(b"edited after review")
        with self.assertRaises(CheckoutConflict):self.repo.apply_checkout(plan)
        self.assertEqual((self.root/"old").read_bytes(),b"edited after review")
        (self.root/"old").write_bytes(b"initial")
        forged=replace(plan,changes=())
        with self.assertRaises(CheckoutConflict):self.repo.apply_checkout(forged)
        self.repo.refs.update("refs/heads/next",self.first,expected=self.target)
        with self.assertRaises(CheckoutConflict):self.repo.apply_checkout(plan)
        self.assertFalse((self.repo.git_dir/"checkout-state").exists())

    def test_file_failure_retains_journal_blocks_mutation_and_rolls_back_after_restart(self):
        before=self.repo.index.path.read_bytes();self.fail_file_write()
        self.assertTrue((self.repo.git_dir/"checkout-state").is_dir())
        with self.assertRaises(UnsupportedFormat):self.repo.add("old")
        with self.assertRaises(UnsupportedFormat):self.repo.refs.update("refs/heads/main",self.target,expected=self.first)
        result=Repository.open(self.root).recover_checkout()
        self.assertTrue(result.rolled_back);self.assertEqual(self.repo.refs.resolve().oid,self.first)
        self.assertEqual(self.repo.index.path.read_bytes(),before);self.assertEqual((self.root/"old").read_bytes(),b"initial")
        self.assertFalse((self.root/"dir").exists());self.assertFalse((self.repo.git_dir/"checkout-state").exists())

    def test_index_and_head_publication_failures_are_recoverable(self):
        for failed in ("index","HEAD"):
            with self.subTest(failed=failed):
                original=transaction.os.replace
                def interrupted(source,destination):
                    if Path(destination)==self.repo.git_dir/failed:raise OSError("injected metadata publication failure")
                    return original(source,destination)
                with patch.object(transaction.os,"replace",interrupted):
                    with self.assertRaises(CheckoutRecoveryRequired):self.repo.apply_checkout(self.repo.plan_checkout("next"))
                self.repo.recover_checkout()
                self.assertEqual(self.repo.refs.resolve().oid,self.first);self.assertEqual((self.root/"old").read_bytes(),b"initial")
                self.assertFalse((self.root/"dir").exists())

    def test_recovery_protects_late_edits_and_untracked_directory_contents(self):
        self.fail_file_write()
        (self.root/"dir/local").write_bytes(b"late untracked")
        with self.assertRaises(CheckoutConflict):self.repo.recover_checkout()
        self.assertEqual((self.root/"dir/local").read_bytes(),b"late untracked")
        (self.root/"dir/local").unlink()
        (self.root/"dir/file").write_bytes(b"late tracked edit")
        with self.assertRaises(CheckoutConflict):self.repo.recover_checkout()
        self.assertEqual((self.root/"dir/file").read_bytes(),b"late tracked edit")
        (self.root/"dir/file").unlink();self.repo.recover_checkout()

    def test_completed_journal_finalizes_after_cleanup_interruption(self):
        with patch.object(transaction,"_complete",side_effect=OSError("injected cleanup interruption")):
            with self.assertRaises(CheckoutRecoveryRequired):self.repo.apply_checkout(self.repo.plan_checkout("next"))
        self.assertEqual(self.repo.refs.resolve().oid,self.target)
        result=self.repo.recover_checkout()
        self.assertFalse(result.rolled_back);self.assertEqual(result.target,self.target)
        self.assertEqual((self.root/"old").read_bytes(),b"new")

    def test_late_index_and_ref_writers_recheck_pending_state_under_locks(self):
        state=self.repo.git_dir/"checkout-state";original=os.open
        for selected,operation in (("index.lock",lambda:self.repo.index.transaction().__enter__()),
                                   ("packed-refs.lock",lambda:self.repo.refs.update("refs/heads/main",self.target,expected=self.first))):
            def appeared(path,*args,**kwargs):
                if Path(path).name==selected and not state.exists():state.mkdir()
                return original(path,*args,**kwargs)
            try:
                with patch.object(os,"open",appeared):
                    with self.assertRaises(UnsupportedFormat):operation()
            finally:state.rmdir()
            self.assertEqual(self.repo.refs.resolve().oid,self.first)
            self.assertFalse((self.repo.git_dir/"index.lock").exists())
            self.assertFalse((self.repo.git_dir/"packed-refs.lock").exists())

    def test_recovery_rechecks_journal_identity_after_acquiring_locks(self):
        self.fail_file_write();path=self.repo.git_dir/"checkout-state/receipt.json";original=path.read_bytes()
        locks=transaction._locks
        @contextmanager
        def changed(*args,**kwargs):
            with locks(*args,**kwargs):
                value=json.loads(original);value["token"]="0"*32;path.write_text(json.dumps(value))
                yield
        with patch.object(transaction,"_locks",changed):
            with self.assertRaises(CheckoutConflict):self.repo.recover_checkout()
        path.write_bytes(original);self.repo.recover_checkout()

    def test_tampered_journal_paths_or_payloads_are_refused(self):
        self.fail_file_write();state=self.repo.git_dir/"checkout-state"
        payload=next(p for p in state.iterdir() if p.name.startswith("p") and p.name.endswith("-b"))
        original=payload.read_bytes();payload.write_bytes(b"tampered")
        with self.assertRaises(CheckoutRecoveryRequired):self.repo.recover_checkout()
        payload.write_bytes(original)
        receipt=state/"receipt.json";raw=receipt.read_bytes();value=json.loads(raw)
        value["paths"][0]["path"]=b".git/config".hex();receipt.write_text(json.dumps(value))
        with self.assertRaises(CheckoutRecoveryRequired):self.repo.recover_checkout()
        receipt.write_bytes(raw);self.repo.recover_checkout()

    def test_directory_file_transitions_and_symlink_values(self):
        directory=commit_tree(self.repo,{b"old/child":b"child"})
        self.repo.apply_checkout(self.repo.plan_checkout(directory))
        self.assertEqual((self.root/"old/child").read_bytes(),b"child")
        self.repo.apply_checkout(self.repo.plan_checkout(self.first))
        self.assertTrue((self.root/"old").is_file())
        config=self.repo.git_dir/"config"
        config.write_bytes(config.read_bytes()+b"[core]\nsymlinks = false\n")
        link=commit_tree(self.repo,{b"link":(0o120000,b"../outside")})
        self.repo.apply_checkout(self.repo.plan_checkout(link))
        self.assertFalse((self.root/"link").is_symlink());self.assertEqual((self.root/"link").read_bytes(),b"../outside")
        self.assertEqual(self.repo.status(),())

    def test_cli_review_and_apply_without_git(self):
        command=[sys.executable,"-I","-m","gitdx","-C",str(self.root),"checkout","next"]
        review=subprocess.run(command+["--dry-run"],capture_output=True,env=dict(os.environ,PATH=""),check=True)
        self.assertEqual(json.loads(review.stdout)["target"],str(self.target))
        self.assertEqual((self.root/"old").read_bytes(),b"initial")
        subprocess.run(command,capture_output=True,env=dict(os.environ,PATH=""),check=True)
        self.assertEqual((self.root/"old").read_bytes(),b"new")

    @unittest.skipIf(os.name=="nt","POSIX native symlink checkout profile")
    def test_native_symlink_is_a_leaf_and_can_transition_to_a_directory(self):
        link=commit_tree(self.repo,{b"link":(0o120000,b"../outside")})
        self.repo.apply_checkout(self.repo.plan_checkout(link))
        self.assertTrue((self.root/"link").is_symlink());self.assertEqual(os.readlink(self.root/"link"),"../outside")
        directory=commit_tree(self.repo,{b"link/file":b"inside"})
        self.repo.apply_checkout(self.repo.plan_checkout(directory))
        self.assertEqual((self.root/"link/file").read_bytes(),b"inside")


class CheckoutGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_stock_git_worktree_index_head_and_rollback_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);repo=Repository.init(root,object_format=format)
                first=commit_tree(repo,{b"file":b"one"})
                repo.refs.update("refs/heads/main",first,expected=None)
                git(root,"reset","--hard","HEAD")
                target=commit_tree(repo,{b"nested/data":b"binary\0\xff",b"exec":(0o100755,b"run")},parents=(first,))
                repo.refs.update("refs/heads/next",target,expected=None)
                git(root,"pack-refs","--all")
                repo.apply_checkout(repo.plan_checkout("next"))
                self.assertEqual(git(root,"symbolic-ref","HEAD").strip(),b"refs/heads/next")
                self.assertEqual(git(root,"status","--porcelain"),b"")
                self.assertEqual((root/"nested/data").read_bytes(),b"binary\0\xff")
                self.assertEqual(git(root,"write-tree").strip().decode(),str(repo.log()[0].commit.tree))
                repo.apply_checkout(repo.plan_checkout(first))
                self.assertEqual(git(root,"rev-parse","HEAD").strip().decode(),str(first))
                self.assertEqual(git(root,"status","--porcelain"),b"")
