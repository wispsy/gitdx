from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gitdx import DirectRef, GitdxError, InvalidRef, ObjectFormat, PackedRef, PackedRefs, RefConflict, RefLocked, ReflogUpdate, Repository, Tag, ObjectType
from gitdx.reflog import ReflogStore
import test_objects as oracle
from test_objects import IDENTITY, git
from test_refs import commit


class PackedRefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repo = Repository.init(self.temp.name)
        self.first = commit(self.repo);self.second = commit(self.repo, b"second", (self.first,))
        self.path = self.repo.git_dir / "packed-refs"
        self.path.write_bytes(PackedRefs((PackedRef("refs/heads/main", self.first),)).encode())

    def test_parse_peeling_order_and_invalid_inputs(self):
        for fmt in ObjectFormat:
            a = self.first if fmt is ObjectFormat.SHA1 else self.first.__class__("1" * 64, fmt)
            b = self.second if fmt is ObjectFormat.SHA1 else self.first.__class__("2" * 64, fmt)
            snapshot = PackedRefs((PackedRef("refs/tags/v1", a, b),), fmt)
            self.assertEqual(PackedRefs.from_bytes(snapshot.encode(), fmt), snapshot)
        oid = str(self.first).encode()
        for data in (b"^" + oid + b"\n", oid + b" refs/heads/z\n" + oid + b" refs/heads/a\n",
                     oid + b" refs/../escape\n", oid + b" refs/heads/main\n^bad\n",
                     oid + b" refs/heads/main\n" + oid + b" refs/heads/Main\n", b"broken", b"# header\n\n"):
            with self.subTest(data=data), self.assertRaises(GitdxError):PackedRefs.from_bytes(data)
        with self.assertRaises(InvalidRef):PackedRefs((PackedRef("refs/heads/a", self.first), PackedRef("refs/heads/a/b", self.first)))

    def test_loose_override_expected_old_and_safe_deletion(self):
        refs = self.repo.refs
        self.assertEqual(refs.resolve().oid, self.first)
        with self.assertRaises(RefConflict):refs.update("refs/heads/main", self.second, expected=None)
        refs.update("refs/heads/main", self.second, expected=self.first)
        self.assertEqual(refs.resolve().oid, self.second)
        self.assertEqual(refs.packed().entries[0].oid, self.first)
        result = refs.delete("refs/heads/main", expected=self.second)
        self.assertTrue(result.committed)
        self.assertIsNone(refs.read("refs/heads/main"))
        self.assertEqual(refs.packed().entries, ())
        self.assertFalse(list(self.repo.git_dir.rglob("*.lock")))

    def test_packed_lock_and_namespace_conflicts_are_preserved(self):
        lock = self.repo.git_dir / "packed-refs.lock";lock.write_bytes(b"other owner")
        with self.assertRaises(RefLocked):self.repo.refs.delete("refs/heads/main", expected=self.first)
        self.assertEqual(lock.read_bytes(), b"other owner");lock.unlink()
        for name in ("refs/heads/main/sub", "refs/heads/Main/sub", "refs/heads/Main"):
            with self.assertRaises(InvalidRef):self.repo.refs.update(name, self.second, expected=None)
        self.repo.refs.update("refs/heads/main", self.second, expected=self.first)
        self.assertEqual(self.repo.refs.resolve().oid, self.second)

    def test_deletion_holds_packed_lock_until_loose_unlink(self):
        self.repo.refs.update("refs/heads/main", self.second, expected=self.first)
        target = self.repo.git_dir / "refs/heads/main"
        original = Path.unlink
        seen = []
        def observe(path, *args, **kwargs):
            if path == target:
                seen.append(True)
                self.assertTrue((self.repo.git_dir / "packed-refs.lock").exists())
                self.assertEqual(self.repo.refs.packed().entries, ())
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", observe):self.repo.refs.delete("refs/heads/main", expected=self.second)
        self.assertTrue(seen)

    def test_failed_delete_preserves_visible_ref_and_retry(self):
        refs = self.repo.refs;refs.update("refs/heads/main", self.second, expected=self.first)
        target = self.repo.git_dir / "refs/heads/main";original = Path.unlink
        def fail(path, *args, **kwargs):
            if path == target:raise OSError("injected loose deletion failure")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail):
            with self.assertRaises(OSError):refs.delete("refs/heads/main", expected=self.second)
        self.assertEqual(refs.resolve().oid, self.second)
        refs.delete("refs/heads/main", expected=self.second)
        self.assertIsNone(refs.read("refs/heads/main"))

    def test_reflog_success_failure_partial_repair_and_stale_receipt(self):
        refs = self.repo.refs;update = ReflogUpdate(IDENTITY, b"advance")
        def partial(store, receipt):
            with store.path(receipt.name).open("ab") as stream:stream.write(receipt.line[:23])
            raise OSError("injected partial log append")
        with patch.object(ReflogStore, "append", partial):
            result = refs.update("refs/heads/main", self.second, expected=self.first, reflog=update)
        self.assertTrue(result.committed);self.assertEqual(result.reflog_error, "OSError")
        self.assertEqual(refs.resolve().oid, self.second)
        refs.repair_reflog(result);refs.repair_reflog(result)
        log = self.repo.git_dir / "logs/refs/heads/main"
        self.assertEqual(log.read_bytes(), result.reflog_receipt.line)
        refs.update("refs/heads/main", self.first, expected=self.second, reflog=update)
        with self.assertRaises(RefConflict):refs.repair_reflog(result)
        self.assertEqual(len(log.read_bytes().splitlines()), 2)

    def test_repair_refuses_unrelated_log_changes(self):
        refs = self.repo.refs
        with patch.object(ReflogStore, "append", side_effect=OSError("injected")):
            result = refs.update("refs/heads/main", self.second, expected=self.first, reflog=ReflogUpdate(IDENTITY))
        log = self.repo.git_dir / "logs/refs/heads/main";log.write_bytes(b"unrelated history\n")
        with self.assertRaises(InvalidRef):refs.repair_reflog(result)
        self.assertEqual(log.read_bytes(), b"unrelated history\n")


class PackedRefGitOracleTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_pack_refs_peeling_overrides_delete_and_reflog(self):
        for fmt in ObjectFormat:
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                repo = Repository.init(tmp, object_format=fmt)
                first, second = commit(repo), commit(repo, b"second")
                tag = repo.objects.write(Tag.create(first, target_type=ObjectType.COMMIT, name=b"v1", tagger=IDENTITY, message=b"tag\n").to_raw())
                repo.refs.update("refs/heads/main", first, expected=None)
                repo.refs.update("refs/tags/v1", tag, expected=None)
                git(repo.git_dir, "pack-refs", "--all", "--prune")
                self.assertEqual(next(e.peeled for e in repo.refs.packed().entries if e.name == "refs/tags/v1"), first)
                repo.refs.update("refs/heads/main", second, expected=first, reflog=ReflogUpdate(IDENTITY, b"advance"))
                self.assertEqual(git(repo.git_dir, "rev-parse", "main@{0}").strip().decode(), str(second))
                git(repo.git_dir, "pack-refs", "--all", "--prune")
                git(repo.git_dir, "-c", "gc.autoDetach=false", "gc", "--prune=now")
                self.assertEqual(repo.refs.resolve().oid, second)
                repo.refs.delete("refs/heads/main", expected=second)
                self.assertIsNone(repo.refs.read("refs/heads/main"))
                self.assertNotIn(b"refs/heads/main", git(repo.git_dir, "show-ref"))
                git(repo.git_dir, "fsck", "--strict")
