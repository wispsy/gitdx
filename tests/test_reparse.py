from pathlib import Path
from types import SimpleNamespace
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gitdx import Blob, CheckoutLimits, Repository, UnsafePath
from gitdx.checkout import _Budget, _capture
from gitdx.filesystem import check_path
from test_checkout_plan import commit_tree


class ReparseTests(unittest.TestCase):
    def test_storage_root_and_component_checks_use_reparse_attributes(self):
        directory=SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_file_attributes=0x400)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(Path,"lstat",return_value=directory):
                with self.assertRaises(UnsafePath):check_path(root,root)
                with self.assertRaises(UnsafePath):check_path(root,root/"component/file")

    def test_worktree_scan_and_checkout_capture_refuse_reparse_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);repo=Repository.init(root);redirect=root/"redirect";redirect.mkdir()
            target=commit_tree(repo,{b"redirect/file":b"target"})
            directory=SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_file_attributes=0x400)
            original=Path.lstat
            def attributes(path,*args,**kwargs):
                return directory if path==redirect else original(path,*args,**kwargs)
            with patch.object(Path,"lstat",attributes):
                with self.assertRaises(UnsafePath):repo.plan_checkout(target)
                with self.assertRaises(UnsafePath):_capture(root,b"redirect/file",_Budget(CheckoutLimits()))
                with self.assertRaises(UnsafePath):_capture(root,b"redirect",_Budget(CheckoutLimits()))


@unittest.skipUnless(os.name=="nt","native Windows junction boundary fixture")
class WindowsJunctionTests(unittest.TestCase):
    def junction(self,link,target):
        env=dict(os.environ,GITDX_TEST_LINK=str(link),GITDX_TEST_TARGET=str(target))
        subprocess.run('cmd /d /c mklink /J "%GITDX_TEST_LINK%" "%GITDX_TEST_TARGET%"',env=env,capture_output=True,check=True)
        self.addCleanup(lambda:link.rmdir() if os.path.isjunction(link) else None)

    def test_real_junctions_preserve_outside_worktree_objects_and_metadata(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        base=Path(temporary.name);outside=base/"outside";outside.mkdir();sentinel=outside/"keep";sentinel.write_bytes(b"outside data")
        repo=Repository.init(base/"repo");self.junction(repo.work_tree/"redirect",outside)
        target=commit_tree(repo,{b"redirect/keep":b"overwrite"})
        with self.assertRaises(UnsafePath):repo.plan_checkout(target,overwrite_tracked=True)
        for i in range(1000):
            blob=Blob(f"candidate-{i}".encode()).to_raw();oid=blob.oid()
            bucket=repo.objects.path/oid.hex[:2]
            if not bucket.exists():break
        else:self.fail("could not select an unused object bucket")
        self.junction(bucket,outside)
        before={p.name for p in outside.iterdir()}
        with self.assertRaises(UnsafePath):repo.objects.write(blob)
        with self.assertRaises(UnsafePath):repo.objects.read(oid)
        self.assertEqual({p.name for p in outside.iterdir()},before)
        second=Repository.init(base/"second");metadata=outside/"metadata";second.git_dir.rename(metadata)
        self.junction(second.git_dir,metadata)
        with self.assertRaises(UnsafePath):Repository.open(second.work_tree)
        self.assertEqual(sentinel.read_bytes(),b"outside data")
