from dataclasses import replace
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from gitdx import Blob, Index, IndexEntry, IndexExtension, IndexFile, IndexLimits, IndexLocked, IndexStat, InvalidIndex, ObjectFormat, Repository, UnsupportedFormat, UnsafePath
import test_objects as oracle
from test_objects import git


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Repository.init(self.temporary.name)
        self.oid = self.repo.objects.write(Blob(b"example").to_raw())

    def checksum(self, data):
        return data + hashlib.sha1(data).digest()

    def test_stat_stages_flags_raw_paths_and_both_hashes(self):
        for fmt in ObjectFormat:
            oid = Blob(b"x").to_raw().oid(fmt)
            stat = IndexStat(1, 2, 3, 4, 5, 6, 7, 8, 9)
            entries = (IndexEntry(b"raw-\xff", oid, 0o100755, stat=stat, assume_valid=True),
                       *(IndexEntry(b"conflict", oid, stage=stage) for stage in (1, 2, 3)))
            index = Index(entries, fmt)
            self.assertEqual(Index.from_bytes(index.encode(), fmt), index)
            self.assertEqual(Index.from_bytes(Index(format=fmt).encode(), fmt).entries, ())

    def test_extension_roundtrip_and_invalidation(self):
        original = Index((IndexEntry(b"a", self.oid),), extensions=(IndexExtension(b"TEST", b"opaque"),))
        self.assertEqual(Index.from_bytes(original.encode()), original)
        self.assertIs(original.with_entries(original.entries), original)
        self.assertEqual(original.with_entries((IndexEntry(b"b", self.oid),)).extensions, ())
        for name in (b"link", b"sdir", b"abcd"):
            with self.assertRaises(UnsupportedFormat):
                Index.from_bytes(self.checksum(Index().encode()[:-20] + name + struct.pack(">I", 0)))

    def test_checksum_signature_versions_truncation_and_padding(self):
        encoded = Index((IndexEntry(b"a", self.oid),)).encode()
        for data in (encoded[:-1], b"wrong" + encoded[5:], encoded[:-20] + b"x" * 20,
                     self.checksum(encoded[:-21]), self.checksum(encoded[:75] + b"x" + encoded[76:-20])):
            with self.subTest(data=data), self.assertRaises(InvalidIndex):
                Index.from_bytes(data)
        for version in (1, 3, 4, 99):
            with self.assertRaises(UnsupportedFormat):
                Index.from_bytes(self.checksum(b"DIRC" + struct.pack(">II", version, 0)))
        bad_flags = bytearray(encoded[:-20]);bad_flags[72] |= 0x40
        with self.assertRaises(UnsupportedFormat):
            Index.from_bytes(self.checksum(bytes(bad_flags)))

    def test_order_duplicate_stages_and_limits(self):
        a, b = IndexEntry(b"a", self.oid), IndexEntry(b"b", self.oid)
        for entries in ((a, a), (a, replace(a, stage=2))):
            with self.assertRaises(InvalidIndex):
                Index(entries)
        encoded = Index((a, b)).encode()
        reversed_entries = encoded[:12] + encoded[76:140] + encoded[12:76]
        with self.assertRaises(InvalidIndex):
            Index.from_bytes(self.checksum(reversed_entries))
        for limits in (IndexLimits(max_entries=1), IndexLimits(max_bytes=40), IndexLimits(max_path_bytes=1)):
            with self.assertRaises(InvalidIndex):
                Index((IndexEntry(b"long", self.oid), b)).encode(limits)
        with self.assertRaises(InvalidIndex):
            Index.from_bytes(encoded, limits=IndexLimits(max_entries=1))

    def test_paths_modes_stat_and_length_sentinel(self):
        for path in (b"", b"/absolute", b"../outside", b"a/.git/index", b"a//b", b"a/", b"x\0y"):
            with self.assertRaises(InvalidIndex):
                IndexEntry(path, self.oid)
        with self.assertRaises(UnsupportedFormat):
            IndexEntry(b"sparse", self.oid, 0o40000)
        with self.assertRaises(InvalidIndex):
            IndexStat(mtime_nanoseconds=1_000_000_000)
        long = Index((IndexEntry(b"a" * 4096, self.oid),))
        self.assertEqual(Index.from_bytes(long.encode()), long)

    def test_transactions_commit_abort_and_competing_lock(self):
        self.assertEqual(self.repo.index.read().entries, ())
        with self.repo.index.transaction() as tx:
            tx.replace([IndexEntry(b"a", self.oid)])
        self.assertFalse((self.repo.git_dir / "index").exists())
        with self.repo.index.transaction() as tx:
            with self.assertRaises(IndexLocked):
                with self.repo.index.transaction():pass
            tx.replace([IndexEntry(b"a", self.oid)])
            tx.commit()
            # A successor's lock must survive the first context's exit.
            (self.repo.git_dir / "index.lock").write_bytes(b"successor")
        self.assertEqual((self.repo.git_dir / "index.lock").read_bytes(), b"successor")
        self.assertEqual(self.repo.index.read().entries, (IndexEntry(b"a", self.oid),))

    def test_failed_publication_preserves_index_and_owned_locks(self):
        with self.repo.index.transaction() as tx:
            tx.replace([IndexEntry(b"a", self.oid)]);tx.commit()
        original = self.repo.index.path.read_bytes()
        for operation in ("os.replace", "os.fsync"):
            with patch("gitdx.index." + operation, side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    with self.repo.index.transaction() as tx:
                        tx.replace([IndexEntry(b"b", self.oid)]);tx.commit()
            self.assertEqual(self.repo.index.path.read_bytes(), original)
            self.assertFalse((self.repo.git_dir / "index.lock").exists())

    def test_unsupported_existing_index_and_symlink_preserve_state(self):
        bad = self.checksum(b"DIRC" + struct.pack(">II", 4, 0))
        self.repo.index.path.write_bytes(bad)
        with self.assertRaises(UnsupportedFormat):
            with self.repo.index.transaction():pass
        self.assertEqual(self.repo.index.path.read_bytes(), bad)
        self.assertFalse((self.repo.git_dir / "index.lock").exists())
        self.repo.index.path.unlink()
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"keep")
        try:self.repo.index.path.symlink_to(outside)
        except OSError as error:self.skipTest(str(error))
        with self.assertRaises(UnsafePath):self.repo.index.read()
        self.assertEqual(outside.read_bytes(), b"keep")


class IndexGitOracleTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_bidirectional_index_and_merge_stages(self):
        for fmt in ObjectFormat:
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                repo = Repository.init(tmp, object_format=fmt)
                oid = repo.objects.write(Blob(b"content").to_raw())
                entries = (IndexEntry(b"normal", oid, 0o100755),
                           *(IndexEntry(b"conflict", oid, stage=s) for s in (1, 2, 3)))
                with repo.index.transaction() as tx:tx.replace(entries);tx.commit()
                expected = b"".join(f"{e.mode:06o} {oid} {e.stage}\t".encode() + e.path + b"\0" for e in sorted(entries, key=lambda e:(e.path,e.stage)))
                self.assertEqual(git(repo.git_dir, "ls-files", "--stage", "-z"), expected)
                git(repo.git_dir, "update-index", "--add", "--cacheinfo", f"100644,{oid},native")
                snapshot = repo.index.read()
                self.assertIn(b"native", [e.path for e in snapshot.entries])
                # Native optional extensions are preserved on an unchanged rewrite.
                original = repo.index.path.read_bytes()
                self.assertEqual(snapshot.encode(), original)
