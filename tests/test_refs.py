from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import Blob, Commit, DirectRef, GitdxError, InvalidRef, MissingObject, ObjectFormat, ObjectId, RefConflict, RefLocked, Repository, SymbolicRef, Tree, UnsupportedFormat, UnsafePath
import test_objects as oracle
from test_objects import IDENTITY, git


def commit(repo, message=b"initial", parents=()):
    tree = repo.objects.write(Tree(format=repo.object_format).to_raw())
    return repo.objects.write(Commit.create(tree, author=IDENTITY, committer=IDENTITY,
                                           message=message, parents=parents).to_raw())


class RefTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.repo = Repository.init(self.path)
        self.refs = self.repo.refs
        self.first = commit(self.repo)
        self.second = commit(self.repo, b"second", (self.first,))

    def test_unborn_symbolic_and_detached_head(self):
        head = self.refs.resolve()
        self.assertTrue(head.unborn)
        self.assertFalse(head.detached)
        self.assertEqual(head.name, "refs/heads/main")
        self.refs.update(head.name, self.first, expected=None)
        self.assertEqual(self.refs.resolve().oid, self.first)
        self.assertFalse(self.refs.resolve().unborn)
        with self.assertRaises(RefConflict):
            self.refs.update("HEAD", self.second, expected=self.first)
        self.refs.detach_head(self.first, expected=SymbolicRef("refs/heads/main"))
        self.assertTrue(self.refs.resolve().detached)
        self.refs.set_symbolic("HEAD", "refs/heads/main", expected=DirectRef(self.first))
        self.assertEqual(self.refs.resolve().symbolic_chain, ("HEAD",))

    def test_stale_create_and_update_preserve_value(self):
        self.refs.update("refs/heads/main", self.first, expected=None)
        for expected in (None, self.second):
            with self.assertRaises(RefConflict):
                self.refs.update("refs/heads/main", self.second, expected=expected)
        self.assertEqual(self.refs.read("refs/heads/main"), DirectRef(self.first))
        self.refs.update("refs/heads/main", self.second, expected=self.first)
        self.assertEqual(self.refs.resolve().oid, self.second)
        self.assertFalse(list(self.repo.git_dir.rglob("*.lock")))

    def test_existing_locks_are_preserved(self):
        lock = self.repo.git_dir / "refs/heads/main.lock"
        lock.write_bytes(b"another writer")
        with self.assertRaises(RefLocked):
            self.refs.update("refs/heads/main", self.first, expected=None)
        self.assertEqual(lock.read_bytes(), b"another writer")
        self.assertIsNone(self.refs.read("refs/heads/main"))

    def test_wrong_type_missing_and_wrong_format_targets(self):
        blob = self.repo.objects.write(Blob(b"data").to_raw())
        with self.assertRaises(InvalidRef):
            self.refs.update("refs/heads/main", blob, expected=None)
        self.refs.update("refs/tags/blob", blob, expected=None)
        with self.assertRaises(MissingObject):
            self.refs.update("refs/heads/main", ObjectId("1" * 40), expected=None)
        with self.assertRaises(GitdxError):
            self.refs.update("refs/heads/main", ObjectId("1" * 64, ObjectFormat.SHA256), expected=None)
        self.assertIsNone(self.refs.read("refs/heads/main"))

    def test_cycles_depth_and_dangling_refs(self):
        self.refs.set_symbolic("refs/alias/a", "refs/alias/b", expected=None)
        with self.assertRaises(InvalidRef):
            self.refs.set_symbolic("refs/alias/b", "refs/alias/a", expected=None)
        self.assertIsNone(self.refs.resolve("refs/alias/a").oid)
        directory = self.repo.git_dir / "refs/alias"
        (directory / "b").write_text("ref: refs/alias/a\n")
        with self.assertRaises(InvalidRef):
            self.refs.resolve("refs/alias/a")
        (directory / "b").unlink()
        for n in range(17):
            (directory / str(n)).write_text(f"ref: refs/alias/{n+1}\n")
        with self.assertRaises(InvalidRef):
            self.refs.resolve("refs/alias/0")

    def test_bad_names_and_case_aliases(self):
        for name in ("../escape", "refs/../escape", "refs/heads/x.lock/y", "refs/heads/a:b",
                     "refs/heads/.hidden", "refs/heads/CON", "refs/heads/é", "refs/heads/a//b"):
            with self.subTest(name=name), self.assertRaises(InvalidRef):
                self.refs.update(name, self.first, expected=None)
        self.refs.update("refs/heads/Main", self.first, expected=None)
        with self.assertRaises(InvalidRef):
            self.refs.update("refs/heads/main", self.second, expected=None)

    def test_packed_refs_override_and_changed_config_refuse_mutation(self):
        packed = self.repo.git_dir / "packed-refs"
        packed.write_bytes(f"# pack-refs with: peeled\n{self.first} refs/heads/main\n".encode("ascii"))
        self.refs.update("refs/heads/main", self.second, expected=self.first)
        self.assertEqual(self.refs.read("refs/heads/main"), DirectRef(self.second))
        packed.unlink()
        config = self.repo.git_dir / "config"
        config.write_text("[core]\nrepositoryformatversion=1\n[extensions]\nrefstorage=reftable\n")
        with self.assertRaises(UnsupportedFormat):
            self.refs.update("refs/heads/main", self.first, expected=None)

    def test_interrupted_writes_preserve_ref_and_clean_owned_lock(self):
        self.refs.update("refs/heads/main", self.first, expected=None)
        for operation in ("os.replace", "os.fsync"):
            with patch("gitdx.refs." + operation, side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    self.refs.update("refs/heads/main", self.second, expected=self.first)
            self.assertEqual(self.refs.resolve().oid, self.first)
            self.assertFalse(list(self.repo.git_dir.rglob("*.lock")))

    def test_concurrent_expected_old_writers(self):
        self.refs.update("refs/heads/main", self.first, expected=None)
        barrier = threading.Barrier(2)
        def write():
            barrier.wait()
            try:
                self.refs.update("refs/heads/main", self.second, expected=self.first)
                return "success"
            except (RefConflict, RefLocked):
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: write(), range(2)))
        self.assertCountEqual(results, ["success", "rejected"])
        self.assertEqual(self.refs.resolve().oid, self.second)

    def test_symlink_ref_parent_is_refused(self):
        outside = self.path / "outside"
        outside.mkdir()
        link = self.repo.git_dir / "refs/escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"platform does not permit symlink creation: {error}")
        with self.assertRaises(UnsafePath):
            self.refs.update("refs/escape/a", self.first, expected=None)
        self.assertEqual(list(outside.iterdir()), [])


class RefGitOracleTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_bidirectional_loose_refs_both_formats(self):
        for fmt in ObjectFormat:
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                repo = Repository.init(tmp, object_format=fmt)
                first = commit(repo)
                second = commit(repo, b"second", (first,))
                repo.refs.update("refs/heads/main", first, expected=None)
                self.assertEqual(git(repo.git_dir, "rev-parse", "HEAD").strip().decode(), str(first))
                self.assertIn(str(first).encode(), git(repo.git_dir, "show-ref"))
                git(repo.git_dir, "update-ref", "refs/heads/main", str(second), str(first))
                self.assertEqual(repo.refs.resolve().oid, second)
                repo.refs.detach_head(first, expected=SymbolicRef("refs/heads/main"))
                self.assertEqual(git(repo.git_dir, "rev-parse", "HEAD").strip().decode(), str(first))
                git(repo.git_dir, "fsck", "--strict")
