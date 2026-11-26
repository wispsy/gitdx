"""Separate reset/restore effects, native comparisons and journal recovery."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gitdx import (CheckoutConflict, CheckoutRecoveryRequired, InvalidObject,
                   ObjectFormat, RefConflict, Repository, UnsupportedFormat)
from test_merge import commit, checkout, IDENTITY, snapshot
import test_merge as merge_fixtures
import gitdx.worktree_transaction as transaction


def fixture(root,format):
    repo=Repository.init(root,object_format=format)
    old=commit(repo,{b"file":b"old\n",b"removed":b"removed\n",b"dir/keep":b"keep\n"})
    new=commit(repo,{b"file":b"new\n",b"added":b"added\n",b"dir/keep":b"keep\n"},(old,),b"new\n")
    checkout(repo,new)
    return repo,old,new


class ResetRestoreTests(unittest.TestCase):
    def test_reset_modes_have_distinct_effects_and_planning_is_pure(self):
        for format in ObjectFormat:
            for mode in ("soft","mixed","hard"):
                with self.subTest(format=format,mode=mode),tempfile.TemporaryDirectory() as temporary:
                    repo,old,new=fixture(Path(temporary)/"repo",format)
                    index=(repo.git_dir/"index").read_bytes();stat=(repo.git_dir/"index").stat()
                    before=snapshot(repo);plan=repo.plan_reset(old,mode=mode,expected_head=new)
                    self.assertEqual(snapshot(repo),before)
                    self.assertEqual(bool(plan.index_changes),mode!="soft")
                    self.assertEqual(bool(plan.worktree_changes),mode=="hard")
                    repo.apply_reset(plan)
                    self.assertEqual(repo.refs.resolve().oid,old)
                    self.assertEqual((repo.git_dir/"ORIG_HEAD").read_bytes(),new.hex.encode()+b"\n")
                    if mode=="soft":
                        self.assertEqual((repo.git_dir/"index").read_bytes(),index)
                        self.assertEqual((repo.git_dir/"index").stat().st_mtime_ns,stat.st_mtime_ns)
                    else:self.assertEqual({e.path for e in repo.index.read().entries},{b"file",b"removed",b"dir/keep"})
                    self.assertEqual((repo.work_tree/"file").read_bytes(),b"old\n" if mode=="hard" else b"new\n")
                    self.assertEqual((repo.work_tree/"added").exists(),mode!="hard")

    def test_dirty_staging_worktree_and_untracked_overwrites_are_explicit(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as temporary:
                repo,old,new=fixture(Path(temporary)/"repo",format)
                (repo.work_tree/"file").write_bytes(b"staged\n");repo.add("file")
                (repo.work_tree/"file").write_bytes(b"working\n")
                with self.assertRaises(CheckoutConflict):repo.plan_reset(old,mode="mixed",expected_head=new)
                with self.assertRaises(CheckoutConflict):repo.plan_reset(old,mode="hard",expected_head=new)
                repo.apply_reset(repo.plan_reset(old,mode="mixed",expected_head=new,discard_staged=True))
                self.assertEqual((repo.work_tree/"file").read_bytes(),b"working\n")
                with self.assertRaises(CheckoutConflict):repo.plan_reset(new,mode="hard",expected_head=old,overwrite_tracked=True)
                (repo.work_tree/"added").unlink()  # It became untracked after mixed reset.
                repo.apply_reset(repo.plan_reset(new,mode="hard",expected_head=old,overwrite_tracked=True))
                self.assertEqual((repo.work_tree/"file").read_bytes(),b"new\n")
                # Restoring the old tree must not replace this untracked path.
                (repo.work_tree/"removed").write_bytes(b"untracked\n")
                before=snapshot(repo)
                with self.assertRaises(CheckoutConflict):repo.plan_reset(old,mode="hard",expected_head=new,overwrite_tracked=True)
                self.assertEqual(snapshot(repo),before)

    def test_restore_staging_and_worktree_are_independent_and_preserve_unselected_edits(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as temporary:
                repo,old,new=fixture(Path(temporary)/"repo",format)
                (repo.work_tree/"dir/keep").write_bytes(b"untouched edit\n")
                (repo.work_tree/"untracked").write_bytes(b"untracked\n")
                original_index=(repo.git_dir/"index").read_bytes()
                before=snapshot(repo);plan=repo.plan_restore("file",source=old)
                self.assertEqual(snapshot(repo),before);self.assertFalse(plan.index_changes)
                repo.apply_restore(plan)
                self.assertEqual(repo.refs.resolve().oid,new);self.assertEqual((repo.git_dir/"index").read_bytes(),original_index)
                self.assertEqual((repo.work_tree/"file").read_bytes(),b"old\n")
                self.assertEqual((repo.work_tree/"dir/keep").read_bytes(),b"untouched edit\n")
                (repo.work_tree/"file").write_bytes(b"manual\n")
                plan=repo.plan_restore("file",source=old,staged=True,worktree=False)
                self.assertFalse(plan.worktree_changes);repo.apply_restore(plan)
                self.assertEqual((repo.work_tree/"file").read_bytes(),b"manual\n")
                with self.assertRaises(CheckoutConflict):repo.plan_restore("file")
                plan=repo.plan_restore("file",overwrite_tracked=True)
                repo.apply_restore(plan);self.assertEqual((repo.work_tree/"file").read_bytes(),b"old\n")
                with self.assertRaises(CheckoutConflict):repo.plan_restore("file",staged=True,worktree=False)
                repo.apply_restore(repo.plan_restore("file",staged=True,worktree=False,discard_staged=True))
                self.assertEqual((repo.work_tree/"file").read_bytes(),b"old\n")
                self.assertEqual((repo.work_tree/"untracked").read_bytes(),b"untracked\n")

    def test_restore_combined_directory_selection_and_untracked_concealment(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo,old,new=fixture(Path(temporary)/"repo",ObjectFormat.SHA256)
            (repo.work_tree/"file").write_bytes(b"manual");repo.add("file")
            with self.assertRaises(CheckoutConflict):repo.plan_restore(".",source=old,staged=True)
            plan=repo.plan_restore(".",source=old,staged=True,discard_staged=True,overwrite_tracked=True)
            repo.apply_restore(plan)
            self.assertEqual((repo.work_tree/"file").read_bytes(),b"old\n")
            self.assertFalse((repo.work_tree/"added").exists());self.assertEqual(repo.refs.resolve().oid,new)
            self.assertEqual({e.path for e in repo.index.read().entries},{b"file",b"removed",b"dir/keep"})
            repo.remove("removed");(repo.work_tree/"removed").write_bytes(b"untracked")
            with self.assertRaises(CheckoutConflict):repo.plan_restore("removed",source=old,overwrite_tracked=True)

    def test_invalid_modes_unknown_paths_stale_refs_and_stale_plans_never_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo,old,new=fixture(Path(temporary)/"repo",ObjectFormat.SHA1)
            before=snapshot(repo)
            with self.assertRaises(RefConflict):repo.plan_reset(old,mode="soft",expected_head=old)
            with self.assertRaises(UnsupportedFormat):repo.plan_reset(old,mode="merge",expected_head=new)
            with self.assertRaises(ValueError):repo.plan_restore("file",staged=False,worktree=False)
            with self.assertRaises(FileNotFoundError):repo.plan_restore("missing")
            self.assertEqual(snapshot(repo),before)
            plan=repo.plan_reset(old,mode="soft",expected_head=new)
            repo.refs.update("refs/heads/main",old,expected=new)
            with self.assertRaises(CheckoutConflict):repo.apply_reset(plan)
            repo.refs.update("refs/heads/main",new,expected=old)
            plan=repo.plan_restore("file",source=old)
            (repo.work_tree/"file").write_bytes(b"later")
            with self.assertRaises(CheckoutConflict):repo.apply_restore(plan)
            self.assertEqual((repo.work_tree/"file").read_bytes(),b"later")

    def test_staged_restore_refuses_file_directory_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo=Repository.init(Path(temporary)/"repo")
            ours=commit(repo,{b"p":b"file"});other=commit(repo,{b"p/q":b"child"})
            checkout(repo,ours)
            before=snapshot(repo)
            with self.assertRaises(InvalidObject):repo.plan_restore("p/q",source=other,staged=True,worktree=False)
            self.assertEqual(snapshot(repo),before)

    def test_reset_and_restore_interruption_recover_without_losing_later_content(self):
        for operation in ("reset","restore"):
            for committed in (False,True):
                with self.subTest(operation=operation,committed=committed),tempfile.TemporaryDirectory() as temporary:
                    repo,old,new=fixture(Path(temporary)/"repo",ObjectFormat.SHA256)
                    plan=repo.plan_reset(old,mode="hard",expected_head=new) if operation=="reset" else repo.plan_restore("file",source=old,staged=True)
                    save=transaction._save
                    def interrupted(root,receipt):
                        if receipt["phase"]=="committed":raise OSError("injected marker failure")
                        return save(root,receipt)
                    target="_finalize" if committed else "_save"
                    effect=OSError("cleanup failed") if committed else interrupted
                    with patch.object(transaction,target,side_effect=effect),self.assertRaises(CheckoutRecoveryRequired):
                        (repo.apply_reset if operation=="reset" else repo.apply_restore)(plan)
                    result=repo.recover_operation();self.assertEqual(result.rolled_back,not committed)
                    self.assertEqual((repo.work_tree/"file").read_bytes(),b"old\n" if committed else b"new\n")
                    self.assertEqual(repo.refs.resolve().oid,old if operation=="reset" and committed else new)
                    self.assertFalse((repo.git_dir/"operation-state").exists())


class NativeResetRestoreTests(merge_fixtures.NativeMergeTests):
    # Do not inherit the merge test: it has its own installed qualification.
    test_native_clean_tree_results_and_conflict_stages_for_both_hashes=None

    def test_native_reset_modes_and_restore_results_for_both_hashes(self):
        for format in ObjectFormat:
            for mode in ("soft","mixed","hard"):
                with self.subTest(format=format,mode=mode),tempfile.TemporaryDirectory() as temporary:
                    root=Path(temporary);repo,old,new=fixture(root/"repo",format)
                    self.git(root,"clone","--no-local",str(repo.work_tree),str(root/"native"));native=root/"native"
                    self.git(native,"reset","--"+mode,old.hex)
                    repo.apply_reset(repo.plan_reset(old,mode=mode,expected_head=new))
                    self.assertEqual(self.git(repo.work_tree,"ls-files","--stage").stdout,self.git(native,"ls-files","--stage").stdout)
                    for filename in ("file","added","removed","dir/keep"):
                        a=repo.work_tree/filename;b=native/filename
                        self.assertEqual(a.exists(),b.exists())
                        if a.exists():self.assertEqual(a.read_bytes(),b.read_bytes())
            for staged,worktree in ((False,True),(True,False),(True,True)):
                with self.subTest(format=format,staged=staged,worktree=worktree),tempfile.TemporaryDirectory() as temporary:
                    root=Path(temporary);repo,old,_=fixture(root/"repo",format)
                    self.git(root,"clone","--no-local",str(repo.work_tree),str(root/"native"));native=root/"native"
                    flags=(["--staged"] if staged else [])+(["--worktree"] if worktree else [])
                    self.git(native,"restore","--source",old.hex,*flags,"--","file","added","removed")
                    repo.apply_restore(repo.plan_restore("file","added","removed",source=old,staged=staged,worktree=worktree))
                    self.assertEqual(self.git(repo.work_tree,"ls-files","--stage").stdout,self.git(native,"ls-files","--stage").stdout)
                    for filename in ("file","added","removed"):
                        a=repo.work_tree/filename;b=native/filename
                        self.assertEqual(a.exists(),b.exists())
                        if a.exists():self.assertEqual(a.read_bytes(),b.read_bytes())


if __name__=="__main__":unittest.main()
