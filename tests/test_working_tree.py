from pathlib import Path
import os
import tempfile
import unittest
import subprocess
import sys

from gitdx import Blob,Commit,IgnoredPath,ObjectFormat,Repository,Tree,TreeEntry,UnsafePath,UnsupportedFormat
import test_objects as oracle
from test_objects import IDENTITY,git


def head(repo):
    entries = tuple(TreeEntry(e.mode,e.path,e.oid) for e in repo.index.read().entries)
    tree = repo.objects.write(Tree(entries,repo.object_format).to_raw())
    oid = repo.objects.write(Commit.create(tree,author=IDENTITY,committer=IDENTITY,message=b"initial\n").to_raw())
    repo.refs.update("refs/heads/main",oid,expected=None)


class WorkingTreeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository.init(self.root)

    def test_add_modified_removed_and_index_only_remove(self):
        path=self.root/"file";path.write_bytes(b"one\0\xff")
        self.repo.add("file");self.assertEqual(self.repo.status()[0].xy,"A ")
        head(self.repo);self.assertEqual(self.repo.status(),())
        path.write_bytes(b"two");self.assertEqual(self.repo.status()[0].xy," M")
        self.repo.add("file");self.assertEqual(self.repo.status()[0].xy,"M ")
        self.repo.remove("file")
        self.assertTrue(path.exists());self.assertEqual([e.xy for e in self.repo.status()],["D ","??"])
        self.repo.add("file");path.unlink();self.repo.add("file")
        self.assertEqual(self.repo.status()[0].xy,"D ")

    def test_ignore_scopes_negation_escapes_and_tracked_files(self):
        (self.root/".gitignore").write_bytes(b"*.log\n!keep.log\nbuild/\n**/cache/\n\\#literal\nspace\\ \n")
        for name in ("drop.log","keep.log","#literal"):(self.root/name).write_bytes(b"x")
        with self.assertRaises(UnsafePath):self.repo.add("space ")
        # Win32 normalizes trailing spaces before enumeration; POSIX can store
        # the literal name, which the portable scanning profile also rejects.
        if os.name!="nt":
            (self.root/"space ").write_bytes(b"x")
            with self.assertRaises(UnsafePath):self.repo.add(".")
            (self.root/"space ").unlink()
        (self.root/"build").mkdir();(self.root/"build/keep").write_bytes(b"x")
        (self.root/"nested").mkdir();(self.root/"nested/.gitignore").write_bytes(b"!local.log\n")
        (self.root/"nested/local.log").write_bytes(b"x")
        (self.root/"nested/cache").mkdir();(self.root/"nested/cache/file").write_bytes(b"x")
        self.repo.add(".")
        paths={e.path for e in self.repo.index.read().entries}
        self.assertIn(b"keep.log",paths);self.assertIn(b"nested/local.log",paths)
        self.assertNotIn(b"drop.log",paths);self.assertNotIn(b"build/keep",paths)
        with self.assertRaises(IgnoredPath):self.repo.add("drop.log")
        self.repo.add("drop.log",force=True);(self.root/"drop.log").write_bytes(b"changed")
        self.repo.add("drop.log");self.assertEqual(self.repo.objects.read(next(e.oid for e in self.repo.index.read().entries if e.path==b"drop.log")).data,b"changed")

    def test_parent_exclusion_cannot_be_undone_by_child(self):
        (self.root/".gitignore").write_bytes(b"dir/\n!dir/file\n")
        (self.root/"dir").mkdir();(self.root/"dir/file").write_bytes(b"x")
        self.repo.add(".");self.assertNotIn(b"dir/file",{e.path for e in self.repo.index.read().entries})

    def test_metadata_traversal_case_alias_and_filters_preserve_index(self):
        (self.root/"file").write_bytes(b"keep");self.repo.add("file")
        before=self.repo.index.path.read_bytes()
        for path in ("../outside",".git/config",".GIT/config","file/../file"):
            with self.assertRaises(UnsafePath):self.repo.add(path)
        (self.root/".gitattributes").write_bytes(b"* filter=external\n")
        with self.assertRaises(UnsupportedFormat):self.repo.add("file")
        self.assertEqual(self.repo.index.path.read_bytes(),before)
        (self.root/".gitattributes").unlink()
        if os.name != "nt":
            (self.root/"File").write_bytes(b"alias")
            if len([p for p in self.root.iterdir() if p.name.lower()=="file"])==2:
                with self.assertRaises(UnsafePath):self.repo.add(".")

    def test_symlink_payload_is_not_followed(self):
        target=self.root/"target";target.write_bytes(b"private bytes")
        link=self.root/"link"
        try:link.symlink_to("../outside")
        except OSError as error:self.skipTest(str(error))
        self.repo.add("link")
        entry=self.repo.index.read().entries[0]
        self.assertEqual(entry.mode,0o120000);self.assertEqual(self.repo.objects.read(entry.oid).data,b"../outside")
        link.unlink();link.symlink_to(target,target_is_directory=False)
        with self.assertRaises(UnsafePath):self.repo.add("link/child")

    def test_exclude_precedence_and_cli_without_git(self):
        info=self.repo.git_dir/"info";info.mkdir(exist_ok=True)
        (info/"exclude").write_bytes(b"*.tmp\n")
        (self.root/".gitignore").write_bytes(b"!keep.tmp\n")
        for name in ("keep.tmp","drop.tmp"):(self.root/name).write_bytes(b"x")
        result=subprocess.run([sys.executable,"-I","-m","gitdx","-C",str(self.root),"add","."],capture_output=True,env=dict(os.environ,PATH=""))
        self.assertEqual(result.returncode,0,result.stderr)
        result=subprocess.run([sys.executable,"-I","-m","gitdx","-C",str(self.root),"status","--porcelain","-z"],capture_output=True,env=dict(os.environ,PATH=""))
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn(b"A  keep.tmp\0",result.stdout);self.assertNotIn(b"drop.tmp",result.stdout)


class WorkingTreeGitOracleTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_add_status_ignores_and_native_index_both_formats(self):
        for fmt in ObjectFormat:
            with self.subTest(format=fmt),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);repo=Repository.init(root,object_format=fmt)
                (root/"file").write_bytes(b"one\0\xff");repo.add("file");head(repo)
                (root/"file").write_bytes(b"two")
                (root/".gitignore").write_bytes(b"*.log\n!keep.log\n")
                (root/"drop.log").write_bytes(b"hidden");(root/"keep.log").write_bytes(b"kept")
                (root/"nested").mkdir();(root/"nested/.gitignore").write_bytes(b"!ok.log\n")
                (root/"nested/ok.log").write_bytes(b"nested")
                actual=b"".join(e.xy.encode()+b" "+e.path+b"\0" for e in repo.status())
                self.assertEqual(actual,git(root,"status","--porcelain=v1","-uall","-z"))
                if os.name != "nt":
                    (root/"keep.log").chmod(0o755);repo.add("keep.log")
                    self.assertEqual(next(e.mode for e in repo.index.read().entries if e.path==b"keep.log"),0o100755)
                repo.add(".")
                before=git(root,"ls-files","--stage","-z")
                git(root,"add","--all")
                self.assertEqual(git(root,"ls-files","--stage","-z"),before)
                (root/"file").unlink();repo.add("file")
                actual=b"".join(e.xy.encode()+b" "+e.path+b"\0" for e in repo.status())
                self.assertEqual(actual,git(root,"status","--porcelain=v1","-uall","-z"))
