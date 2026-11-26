"""Merge state, native trees/index stages and guarded interrupted recovery."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gitdx import (Blob, CheckoutConflict, CheckoutLimits, CheckoutRecoveryRequired,
                   Commit, Index, IndexEntry, MergeLimits, ObjectFormat,
                   ObjectLimitExceeded, RefConflict, Repository, UnsupportedFormat)
from gitdx.history import HistoryLimits, write_tree
import gitdx.worktree_transaction as transaction

IDENTITY=b"Merge Test <merge@example.invalid> 1700000000 +0000"


def commit(repo, files, parents=(), message=b"fixture\n"):
    entries=[]
    for path,value in files.items():
        data,mode=value if isinstance(value,tuple) else (value,0o100644)
        entries.append(IndexEntry(path,repo.objects.write(Blob(data).to_raw()),mode))
    tree=write_tree(repo,Index(tuple(entries),repo.object_format),HistoryLimits())
    return repo.objects.write(Commit.create(tree,author=IDENTITY,committer=IDENTITY,message=message,parents=parents).to_raw())


def checkout(repo, oid):
    previous=repo.refs.resolve().oid
    repo.refs.update("refs/heads/main",oid,expected=previous)
    repo.apply_checkout(repo.plan_checkout("main",overwrite_tracked=True))


def fixture(root, format, base, ours, theirs):
    repo=Repository.init(root,object_format=format)
    ancestor=commit(repo,base)
    left=commit(repo,ours,(ancestor,),b"ours\n")
    right=commit(repo,theirs,(ancestor,),b"theirs\n")
    checkout(repo,left);repo.refs.update("refs/heads/other",right,expected=None)
    return repo,ancestor,left,right


def snapshot(repo):
    return {str(p.relative_to(repo.git_dir.parent)):(p.lstat().st_mode,hashlib.sha256(p.read_bytes()).hexdigest())
            for p in repo.git_dir.parent.rglob("*") if p.is_file() and not p.is_symlink()}


class MergeTests(unittest.TestCase):
    def test_clean_text_merge_is_pure_and_requires_explicit_completion(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as temporary:
                base=b"one\ntwo\nthree\nfour\nfive\n"
                ours=base.replace(b"one",b"OURS");theirs=base.replace(b"five",b"THEIRS")
                repo,ancestor,left,right=fixture(Path(temporary)/"repo",format,{b"file":base},{b"file":ours},{b"file":theirs})
                before=snapshot(repo)
                with patch("subprocess.Popen",side_effect=AssertionError("runtime invoked an external program")):
                    plan=repo.plan_merge("other")
                self.assertEqual(snapshot(repo),before)
                self.assertEqual(plan.base,ancestor);self.assertFalse(plan.conflicts)
                self.assertEqual(repo.apply_merge(plan).state,"ready")
                self.assertEqual(repo.refs.resolve().oid,left)
                self.assertEqual((repo.work_tree/"file").read_bytes(),ours.replace(b"five",b"THEIRS"))
                self.assertEqual((repo.git_dir/"MERGE_HEAD").read_bytes(),right.hex.encode()+b"\n")
                with self.assertRaises(UnsupportedFormat):
                    repo.commit(author=IDENTITY,committer=IDENTITY,message=b"wrong parent\n",expected_head=left)
                with self.assertRaises(CheckoutConflict):
                    repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b"merge\n",expected_head=left,expected_other=right)
                new=repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b"merge\n",expected_head=left,expected_other=right,resolved=True)
                result=Commit.from_bytes(repo.objects.read(new).data,format)
                self.assertEqual(result.parents,(left,right));self.assertEqual(repo.refs.resolve().oid,new)
                self.assertFalse((repo.git_dir/"merge-state").exists());self.assertFalse((repo.git_dir/"MERGE_HEAD").exists())
                self.assertFalse(repo.status())

    def test_conflicts_are_staged_and_explicit_resolution_is_committed(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as temporary:
                repo,_,left,right=fixture(Path(temporary)/"repo",format,{b"file":b"base\n"},{b"file":b"ours\n"},{b"file":b"theirs\n"})
                plan=repo.plan_merge(right);self.assertEqual(plan.conflicts[0].kind,"content")
                self.assertEqual([e.stage for e in plan.index.entries],[1,2,3])
                repo.apply_merge(plan);self.assertEqual(repo.status()[0].xy,"UU")
                self.assertIn(b"<<<<<<< HEAD",(repo.work_tree/"file").read_bytes())
                with self.assertRaises(CheckoutConflict):
                    repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b"unresolved",expected_head=left,expected_other=right,resolved=True)
                (repo.work_tree/"file").write_bytes(b"chosen\0resolution\n");repo.add("file")
                with self.assertRaises(RefConflict):
                    repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b"stale",expected_head=right,expected_other=left,resolved=True)
                oid=repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b"resolved\n",expected_head=left,expected_other=right,resolved=True)
                self.assertEqual(Commit.from_bytes(repo.objects.read(oid).data,format).parents,(left,right))
                self.assertFalse(repo.status())

    def test_binary_type_add_delete_and_directory_file_conflicts_survive_abort(self):
        cases=(
            ({b"f":b"a\0"},{b"f":b"b\0"},{b"f":b"c\0"},"binary"),
            ({},{b"f":b"one"},{b"f":b"two"},"add-add"),
            ({b"f":b"base"},{b"f":b"changed"},{},"modify-delete"),
            ({b"f":b"base"},{b"f":(b"ours",0o100755)},{b"f":(b"elsewhere",0o120000)},"type"),
            ({},{b"p":b"file"},{b"p/q":b"child"},"directory-file"),
            ({},{b"p/q":b"child"},{b"p":b"file"},"directory-file"))
        for format in ObjectFormat:
            for old,left,right,kind in cases:
                with self.subTest(format=format,kind=kind,left=left),tempfile.TemporaryDirectory() as temporary:
                    repo,_,head,other=fixture(Path(temporary)/"repo",format,old,left,right)
                    index=(repo.git_dir/"index").read_bytes()
                    keep=repo.work_tree/"untracked";keep.write_bytes(b"keep")
                    plan=repo.plan_merge(other);self.assertIn(kind,{c.kind for c in plan.conflicts})
                    repo.apply_merge(plan)
                    repo.abort_merge(repo.plan_abort_merge())
                    self.assertEqual(repo.refs.resolve().oid,head);self.assertEqual((repo.git_dir/"index").read_bytes(),index)
                    self.assertEqual(keep.read_bytes(),b"keep")
                    self.assertFalse([s for s in repo.status() if s.index!="?"])

    def test_abort_manual_resolution_requires_discard_and_preserves_unaffected_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base={b"f":b"base",b"untouched":b"original"}
            repo,_,_,other=fixture(Path(temporary)/"repo",ObjectFormat.SHA256,base,base|{b"f":b"ours"},base|{b"f":b"theirs"})
            repo.apply_merge(repo.plan_merge(other))
            (repo.work_tree/"f").write_bytes(b"manual resolution");repo.add("f")
            (repo.work_tree/"untouched").write_bytes(b"keep my edit")
            with self.assertRaises(CheckoutConflict):repo.plan_abort_merge()
            plan=repo.plan_abort_merge(discard_tracked=True)
            (repo.work_tree/"f").write_bytes(b"newer manual resolution")
            with self.assertRaises(CheckoutConflict):repo.abort_merge(plan)
            repo.abort_merge(repo.plan_abort_merge(discard_tracked=True))
            self.assertEqual((repo.work_tree/"f").read_bytes(),b"ours")
            self.assertEqual((repo.work_tree/"untouched").read_bytes(),b"keep my edit")

    def test_dirty_untracked_stale_plan_limits_and_multiple_bases_refuse_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo,root,left,right=fixture(Path(temporary)/"repo",ObjectFormat.SHA1,{b"f":b"base"},{b"f":b"ours"},{b"f":b"theirs",b"new":b"new"})
            (repo.work_tree/"f").write_bytes(b"dirty");before=snapshot(repo)
            with self.assertRaises(CheckoutConflict):repo.plan_merge(right)
            self.assertEqual(snapshot(repo),before);(repo.work_tree/"f").write_bytes(b"ours")
            (repo.work_tree/"new").write_bytes(b"untracked");before=snapshot(repo)
            with self.assertRaises(CheckoutConflict):repo.plan_merge(right)
            self.assertEqual(snapshot(repo),before);(repo.work_tree/"new").unlink()
            plan=repo.plan_merge(right);(repo.work_tree/"f").write_bytes(b"later");before=snapshot(repo)
            with self.assertRaises(CheckoutConflict):repo.apply_merge(plan)
            self.assertEqual(snapshot(repo),before);(repo.work_tree/"f").write_bytes(b"ours")
            with self.assertRaises(ObjectLimitExceeded):repo.plan_merge(right,limits=MergeLimits(checkout=CheckoutLimits(max_file_bytes=1)))
            a=commit(repo,{},(left,right),b"a");b=commit(repo,{},(right,left),b"b");checkout(repo,a)
            with self.assertRaises(UnsupportedFormat):repo.plan_merge(b)
            selected=repo.plan_merge(b,base=left);self.assertEqual(selected.base,left)

    def test_begin_interruption_rolls_back_and_blocks_other_writers(self):
        for boundary in range(1,5):
            with self.subTest(boundary=boundary),tempfile.TemporaryDirectory() as temporary:
                repo,_,left,right=fixture(Path(temporary)/"repo",ObjectFormat.SHA256,{b"f":b"base"},{b"f":b"ours"},{b"f":b"theirs"})
                index=(repo.git_dir/"index").read_bytes();put=transaction._put;calls=0
                def interrupted(*args):
                    nonlocal calls
                    calls+=1
                    if calls==boundary:raise OSError("injected publication fault")
                    return put(*args)
                with patch.object(transaction,"_put",side_effect=interrupted),self.assertRaises(CheckoutRecoveryRequired):repo.apply_merge(repo.plan_merge(right))
                with self.assertRaises(UnsupportedFormat):repo.add("f")
                with self.assertRaises(UnsupportedFormat):repo.refs.update("refs/heads/main",right,expected=left)
                result=repo.recover_merge();self.assertTrue(result.rolled_back)
                self.assertEqual(repo.refs.resolve().oid,left);self.assertEqual((repo.git_dir/"index").read_bytes(),index)
                self.assertEqual((repo.work_tree/"f").read_bytes(),b"ours")
                self.assertFalse((repo.git_dir/"operation-state").exists());self.assertFalse((repo.git_dir/"MERGE_HEAD").exists())

    def test_finish_interruption_restores_active_merge_even_after_ref_publication(self):
        for boundary in ("metadata","marker","cleanup"):
            with self.subTest(boundary=boundary),tempfile.TemporaryDirectory() as temporary:
                repo,_,left,right=fixture(Path(temporary)/"repo",ObjectFormat.SHA1,{},{b"ours":b"ours"},{b"theirs":b"theirs"})
                repo.apply_merge(repo.plan_merge(right));save=transaction._save;put=transaction._put
                def interrupted_save(root,receipt):
                    if receipt["phase"]=="committed":raise OSError("injected commit marker fault")
                    return save(root,receipt)
                def interrupted_put(repo,name,data):
                    if name=="MERGE_MSG":raise OSError("injected metadata fault")
                    return put(repo,name,data)
                target="_save" if boundary=="marker" else "_put" if boundary=="metadata" else "_finalize"
                effect=interrupted_save if boundary=="marker" else interrupted_put if boundary=="metadata" else OSError("injected cleanup fault")
                with patch.object(transaction,target,side_effect=effect),self.assertRaises(CheckoutRecoveryRequired):
                    repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b"merged",expected_head=left,expected_other=right,resolved=True)
                result=repo.recover_merge()
                if boundary=="cleanup":
                    self.assertFalse(result.rolled_back);self.assertNotEqual(repo.refs.resolve().oid,left)
                    self.assertFalse((repo.git_dir/"merge-state").exists())
                else:
                    self.assertTrue(result.rolled_back);self.assertEqual(repo.refs.resolve().oid,left)
                    self.assertEqual((repo.git_dir/"MERGE_HEAD").read_bytes(),right.hex.encode()+b"\n")
                    self.assertTrue((repo.git_dir/"merge-state").is_dir())

    def test_recovery_refuses_external_edits_and_corrupt_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo,_,_,other=fixture(Path(temporary)/"repo",ObjectFormat.SHA1,{b"f":b"base"},{b"f":b"ours"},{b"f":b"theirs"})
            with patch.object(transaction,"_put",side_effect=OSError("fault")),self.assertRaises(CheckoutRecoveryRequired):repo.apply_merge(repo.plan_merge(other))
            (repo.work_tree/"f").write_bytes(b"unrelated edit")
            with self.assertRaises(CheckoutConflict):repo.recover_merge()
            self.assertEqual((repo.work_tree/"f").read_bytes(),b"unrelated edit")
            (repo.git_dir/"operation-state"/"index-after").write_bytes(b"corrupt")
            with self.assertRaises(CheckoutRecoveryRequired):repo.recover_merge()

    def test_abort_interruption_restores_manual_resolution_or_finishes_committed_abort(self):
        for finished in (False,True):
            with self.subTest(finished=finished),tempfile.TemporaryDirectory() as temporary:
                repo,_,_,other=fixture(Path(temporary)/"repo",ObjectFormat.SHA256,{b"f":b"base"},{b"f":b"ours"},{b"f":b"theirs"})
                repo.apply_merge(repo.plan_merge(other));(repo.work_tree/"f").write_bytes(b"manual")
                plan=repo.plan_abort_merge(discard_tracked=True)
                save=transaction._save
                def interrupt(root,receipt):
                    if receipt["phase"]=="committed":raise OSError("marker failure")
                    return save(root,receipt)
                target="_finalize" if finished else "_save"
                effect=OSError("cleanup failure") if finished else interrupt
                with patch.object(transaction,target,side_effect=effect),self.assertRaises(CheckoutRecoveryRequired):repo.abort_merge(plan)
                result=repo.recover_merge();self.assertEqual(result.rolled_back,not finished)
                self.assertEqual((repo.work_tree/"f").read_bytes(),b"ours" if finished else b"manual")
                self.assertEqual((repo.git_dir/"merge-state").exists(),not finished)

    def test_completed_begin_recovers_to_active_state_and_empty_index_absence_is_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo=Repository.init(Path(temporary)/"repo")
            base=commit(repo,{});repo.refs.update("refs/heads/main",base,expected=None)
            other=commit(repo,{b"file":b"new"},(base,));repo.refs.update("refs/heads/other",other,expected=None)
            self.assertFalse((repo.git_dir/"index").exists())
            with patch.object(transaction,"_finalize",side_effect=OSError("rename failure")),self.assertRaises(CheckoutRecoveryRequired):
                repo.apply_merge(repo.plan_merge(other))
            self.assertFalse(repo.recover_merge().rolled_back)
            self.assertTrue((repo.git_dir/"merge-state").exists())
            repo.abort_merge(repo.plan_abort_merge())
            self.assertFalse((repo.git_dir/"index").exists());self.assertFalse((repo.work_tree/"file").exists())

    def test_mode_change_combines_with_text_change_and_no_driver_executes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo,_,left,right=fixture(Path(temporary)/"repo",ObjectFormat.SHA1,{b"f":b"one\ntwo\n"},
                                      {b"f":(b"one\ntwo\n",0o100755)},{b"f":b"one\nchanged\n"})
            with patch("subprocess.Popen",side_effect=AssertionError("external driver executed")):
                plan=repo.plan_merge(right);self.assertFalse(plan.conflicts)
                self.assertEqual(plan.index.entries[0].mode,0o100755)
                repo.apply_merge(plan)
                repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b"mode and content",expected_head=left,expected_other=right,resolved=True)
            self.assertEqual((repo.work_tree/"f").read_bytes(),b"one\nchanged\n")


class NativeMergeTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("git"):
            if os.environ.get("GITDX_REQUIRE_GIT")=="1":self.fail("native Git is required")
            self.skipTest("development Git oracle unavailable")

    def git(self,path,*args,check=True):
        env={k:v for k,v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1",GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git","-C",str(path),"-c","user.name=Merge Test","-c","user.email=merge@example.invalid",
                               "-c","commit.gpgsign=false",*args],capture_output=True,env=env,check=check)

    def test_native_clean_tree_results_and_conflict_stages_for_both_hashes(self):
        for format in ObjectFormat:
            for conflict in (False,True):
                with self.subTest(format=format,conflict=conflict),tempfile.TemporaryDirectory() as temporary:
                    root=Path(temporary);base=b"one\ntwo\nthree\nfour\nfive\n"
                    ours=base.replace(b"one",b"OURS");theirs=base.replace(b"one" if conflict else b"five",b"THEIRS")
                    repo,_,left,right=fixture(root/"source",format,{b"f":base},{b"f":ours},{b"f":theirs})
                    self.git(root,"clone","--no-local",str(repo.work_tree),str(root/"native"))
                    native=root/"native"
                    merged=self.git(native,"merge","--no-ff","--no-commit",right.hex,check=False)
                    self.assertEqual(merged.returncode,1 if conflict else 0,merged.stderr)
                    repo.apply_merge(repo.plan_merge(right))
                    if conflict:
                        native_stages=self.git(repo.work_tree,"ls-files","-u").stdout
                        oracle_stages=self.git(native,"ls-files","-u").stdout
                        self.assertEqual(native_stages,oracle_stages)
                    else:
                        expected=self.git(native,"write-tree").stdout.strip().decode()
                        oid=repo.finish_merge(author=IDENTITY,committer=IDENTITY,message=b"merged",expected_head=left,expected_other=right,resolved=True)
                        self.assertEqual(Commit.from_bytes(repo.objects.read(oid).data,format).tree.hex,expected)
                        self.git(repo.work_tree,"fsck","--no-reflogs")


if __name__=="__main__":unittest.main()
