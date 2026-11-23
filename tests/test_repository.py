from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitdx import Blob, GitdxError, InvalidRef, ObjectFormat, Repository, RepositoryNotFound, UnsupportedFormat, UnsafePath
from gitdx.config import parse_config
import test_objects as oracle
from test_objects import git


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)

    def test_init_open_discover_and_bare(self):
        for fmt in ObjectFormat:
            for bare in (False, True):
                path = self.path / f"{fmt}-{bare}"
                repo = Repository.init(path, bare=bare, object_format=fmt, initial_branch="feature/start")
                self.assertEqual(repo.bare, bare)
                self.assertEqual(repo.object_format, fmt)
                self.assertEqual(repo.work_tree, None if bare else path)
                self.assertEqual(Repository.open(path).git_dir, repo.git_dir)
                oid = repo.objects.write(Blob(b"hello").to_raw())
                self.assertEqual(repo.objects.read(oid).data, b"hello")
                child = path / "nested" / "deep"
                child.mkdir(parents=True)
                self.assertEqual(Repository.discover(child).git_dir, repo.git_dir)
                with self.assertRaises(RepositoryNotFound):
                    Repository.open(child)

    def test_never_reinitializes_or_changes_user_files(self):
        (self.path / "keep").write_bytes(b"user work")
        repo = Repository.init(self.path)
        before = (repo.git_dir / "config").read_bytes()
        with self.assertRaises(FileExistsError):
            Repository.init(self.path, object_format=ObjectFormat.SHA256)
        self.assertEqual((repo.git_dir / "config").read_bytes(), before)
        self.assertEqual((self.path / "keep").read_bytes(), b"user work")
        with self.assertRaises(FileExistsError):
            Repository.init(self.path, bare=True)

    def test_incomplete_nested_metadata_stops_discovery(self):
        outer = Repository.init(self.path)
        for layout in ("marker", "bare"):
            nested = self.path / layout
            nested.mkdir()
            if layout == "marker":
                (nested / ".git").mkdir()
            else:
                (nested / "objects").mkdir()
                (nested / "HEAD").write_bytes(b"ref: refs/heads/main\n")
            with self.subTest(layout=layout), self.assertRaises(UnsupportedFormat):
                Repository.discover(nested).objects.write(Blob(b"wrong repository").to_raw())
        oid = Blob(b"wrong repository").to_raw().oid()
        self.assertFalse(outer.objects.object_path(oid).exists())

    def test_invalid_branch_has_no_filesystem_effect(self):
        for branch in ("../escape", "foo.lock/bar", "bad branch", "", "-main", "CON"):
            path = self.path / "new"
            with self.assertRaises(InvalidRef):
                Repository.init(path, initial_branch=branch)
            self.assertFalse(path.exists())

    def test_failed_init_removes_only_its_metadata(self):
        (self.path / "keep").write_text("user work")
        with patch("gitdx.repository.Repository.open", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                Repository.init(self.path)
        self.assertEqual([p.name for p in self.path.iterdir()], ["keep"])

    def test_unsupported_repository_features_refused(self):
        repo = Repository.init(self.path)
        config = repo.git_dir / "config"
        original = config.read_bytes()
        variants = [b"[core]\nrepositoryformatversion=2\n", b"[core]\nrepositoryformatversion=1\n[extensions]\nunknown=true\n",
                    b"[core]\nrepositoryformatversion=1\n[extensions]\nrefstorage=reftable\n",
                    b"[core]\nrepositoryformatversion=1\n[extensions]\nobjectformat=unknown\n",
                    original + b'[include]\npath=other.conf\n', original + b'[core]\nworktree=/elsewhere\n']
        for data in variants:
            config.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(UnsupportedFormat):
                Repository.open(self.path)
            self.assertEqual(config.read_bytes(), data)
        config.write_bytes(original)
        for feature in ("commondir", "shallow", "objects/info/alternates"):
            target = repo.git_dir / feature
            target.write_text("elsewhere\n")
            with self.assertRaises(UnsupportedFormat):
                Repository.open(self.path)
            target.unlink()

    def test_gitfile_and_symlink_metadata_refused(self):
        marker = self.path / ".git"
        marker.write_text("gitdir: elsewhere\n")
        with self.assertRaises(UnsupportedFormat):
            Repository.open(self.path)
        marker.unlink()
        outside = self.path / "outside"
        outside.mkdir()
        try:
            marker.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"platform does not permit symlink creation: {error}")
        with self.assertRaises(UnsafePath):
            Repository.open(self.path)
        with self.assertRaises(UnsafePath):
            Repository.init(self.path)
        self.assertEqual(list(outside.iterdir()), [])

    def test_local_config_subset(self):
        config = parse_config(b'[Core]\nRepositoryFormatVersion = 1 # comment\nBARE=yes\n[extensions]\nObjectFormat=sha256\n[remote "origin"]\nurl=https://example.invalid/a\n')
        self.assertTrue(config.bare)
        self.assertEqual(config.object_format, ObjectFormat.SHA256)
        for data in (b'[core]\nbare="true"\n', b'[core]\nbare=tru\\\ne\n',
                     b'[core]\nbare=unknown\n', b'key=value\n', b'[core "x"]\nbare=true\n'):
            with self.subTest(data=data), self.assertRaises(UnsupportedFormat):
                parse_config(data)

    def test_malformed_head_is_rejected(self):
        repo = Repository.init(self.path)
        for value in (b"", b"ref: ../escape\n", b"ref: refs/heads/a\n\n", b"not-an-id\n"):
            (repo.git_dir / "HEAD").write_bytes(value)
            with self.subTest(value=value), self.assertRaises(GitdxError):
                Repository.open(self.path)


class RepositoryGitOracleTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_git_opens_python_repositories_and_reverse(self):
        for fmt in ObjectFormat:
            for bare in (False, True):
                with self.subTest(format=fmt, bare=bare), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo = Repository.init(root / "python", bare=bare, object_format=fmt)
                    self.assertEqual(git(repo.git_dir, "rev-parse", "--show-object-format").strip().decode(), fmt.value)
                    self.assertEqual(git(repo.git_dir, "symbolic-ref", "HEAD").strip(), b"refs/heads/main")
                    native = root / "native"
                    native.mkdir()
                    git(native, "init", *(('--bare',) if bare else ()), "--object-format=" + fmt.value)
                    self.assertEqual(Repository.open(native).object_format, fmt)


class CLITests(unittest.TestCase):
    def test_roundtrip_and_expected_errors_without_git_runtime(self):
        for fmt in ObjectFormat:
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                env = dict(os.environ, PATH="")
                def cli(*args, data=None):
                    return subprocess.run([sys.executable, "-I", "-m", "gitdx", "-C", str(path), *args],
                                          input=data, capture_output=True, env=env)
                self.assertEqual(cli("init", "--object-format", fmt.value).returncode, 0)
                content = b"hello\0world\xff"
                result = cli("hash-object", "-w", "--stdin", data=content)
                self.assertEqual(result.returncode, 0, result.stderr)
                oid = result.stdout.strip().decode()
                self.assertEqual(oid, Blob(content).to_raw().oid(fmt).hex)
                self.assertEqual(result.stdout, oid.encode("ascii") + b"\n")
                self.assertEqual(cli("cat-file", oid).stdout, content)
                self.assertEqual(cli("cat-file", "-t", oid).stdout, b"blob\n")
                self.assertEqual(cli("cat-file", "-s", oid).stdout, str(len(content)).encode() + b"\n")
                missing = cli("cat-file", "f" * fmt.hex_size)
                self.assertEqual(missing.returncode, 1)
                self.assertNotIn(b"Traceback", missing.stderr)
                self.assertEqual(cli("init").returncode, 1)
                self.assertEqual(cli("hash-object", "--stdin", "some-file").returncode, 2)
