from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from gitdx import Blob, InvalidObject, LooseObjectStore, MissingObject, ObjectFormat, ObjectId, ObjectLimitExceeded, RawObject, StoreLimits, UnsafePath
import test_objects as oracle
from test_objects import git


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "objects"
        self.path.mkdir()
        self.store = LooseObjectStore(self.path)

    def inject(self, oid, compressed):
        path = self.store.object_path(oid)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(compressed)
        return path

    def test_both_formats_roundtrip_and_idempotent_publication(self):
        for fmt in ObjectFormat:
            store = LooseObjectStore(self.path, fmt)
            raw = Blob(b"hello\0\xff\n").to_raw()
            oid = store.write(raw)
            before = store.object_path(oid).stat().st_mtime_ns
            self.assertEqual(store.write(raw), oid)
            self.assertEqual(store.read(oid), raw)
            self.assertEqual(store.object_path(oid).stat().st_mtime_ns, before)
            self.assertFalse(list(self.path.rglob("tmp_obj_*")))

    def test_missing_and_wrong_format(self):
        with self.assertRaises(MissingObject):
            self.store.read(ObjectId("1" * 40))
        with self.assertRaises(InvalidObject):
            self.store.read(ObjectId("1" * 64, ObjectFormat.SHA256))

    def test_corrupt_headers_compression_and_hash(self):
        oid = Blob(b"x").to_raw().oid()
        for data in (b"blob 2\0x", b"blob 01\0x", b"wrong 1\0x", b"blob -1\0x",
                     b"blob 1x", b"blob 1\0y"):
            self.inject(oid, zlib.compress(data))
            with self.subTest(data=data), self.assertRaises(InvalidObject):
                self.store.read(oid)
        compressed = zlib.compress(b"blob 1\0x")
        for data in (compressed[:-1], compressed + b"extra", compressed + compressed, b"not zlib"):
            self.inject(oid, data)
            with self.subTest(data=data), self.assertRaises(InvalidObject):
                self.store.read(oid)

    def test_limits_bound_read_and_write(self):
        raw = Blob(b"x" * 10000).to_raw()
        oid = raw.oid()
        self.inject(oid, zlib.compress(raw.encode()))
        bounded = LooseObjectStore(self.path, limits=StoreLimits(max_object_bytes=10))
        for action in (lambda: bounded.read(oid), lambda: bounded.write(raw)):
            with self.assertRaises(ObjectLimitExceeded):
                action()
        bounded = LooseObjectStore(self.path, limits=StoreLimits(max_compressed_bytes=1))
        for action in (lambda: bounded.read(oid), lambda: bounded.write(Blob(b"x").to_raw())):
            with self.assertRaises(ObjectLimitExceeded):
                action()
        for limit in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                StoreLimits(max_object_bytes=limit)

    def test_header_limits_and_exact_payload_boundary(self):
        raw = Blob(b"x" * 10).to_raw()
        oid = self.store.write(raw)
        self.assertEqual(LooseObjectStore(self.path, limits=StoreLimits(max_object_bytes=10)).read(oid), raw)
        bounded = LooseObjectStore(self.path, limits=StoreLimits(max_header_bytes=6))
        with self.assertRaises(ObjectLimitExceeded):
            bounded.write(raw)
        with self.assertRaises(InvalidObject):
            bounded.read(oid)

    def test_failed_publication_or_flush_cleans_temporaries(self):
        raw = Blob(b"data").to_raw()
        for name in ("os.link", "os.fsync"):
            with patch("gitdx.object_store." + name, side_effect=OSError("injected failure")):
                with self.assertRaises(OSError):
                    self.store.write(raw)
            self.assertFalse(self.store.object_path(raw.oid()).exists())
            self.assertFalse(list(self.path.rglob("tmp_obj_*")))

    def test_existing_corrupt_object_is_never_overwritten(self):
        raw = Blob(b"data").to_raw()
        path = self.inject(raw.oid(), b"corrupt")
        with self.assertRaises(InvalidObject):
            self.store.write(raw)
        self.assertEqual(path.read_bytes(), b"corrupt")
        self.assertFalse(list(self.path.rglob("tmp_obj_*")))

    def test_concurrent_duplicate_writers(self):
        raw = Blob(b"same object" * 100).to_raw()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.write(raw), range(12)))
        self.assertEqual(set(results), {raw.oid()})
        self.assertEqual(self.store.read(raw.oid()), raw)
        self.assertFalse(list(self.path.rglob("tmp_obj_*")))

    def test_symlink_fanout_and_object_are_refused(self):
        raw = Blob(b"data").to_raw()
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        fanout = self.store.object_path(raw.oid()).parent
        try:
            fanout.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"platform does not permit symlink creation: {error}")
        for action in (lambda: self.store.read(raw.oid()), lambda: self.store.write(raw)):
            with self.assertRaises(UnsafePath):
                action()
        self.assertEqual(list(outside.iterdir()), [])
        fanout.unlink()
        fanout.mkdir()
        (outside / "data").write_bytes(zlib.compress(raw.encode()))
        self.store.object_path(raw.oid()).symlink_to(outside / "data")
        with self.assertRaises(UnsafePath):
            self.store.read(raw.oid())


class StoreGitOracleTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_bidirectional_native_loose_objects(self):
        for fmt in ObjectFormat:
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                git(path, "init", "--bare", "--object-format=" + fmt.value)
                store = LooseObjectStore(path / "objects", fmt)
                raw = RawObject("blob", b"native and Python\0\xff")
                oid = store.write(raw)
                self.assertEqual(git(path, "cat-file", "blob", str(oid)), raw.data)
                content = b"written by Git\n"
                native_oid = git(path, "hash-object", "-w", "--stdin", data=content).strip().decode()
                self.assertEqual(store.read(ObjectId(native_oid, fmt)), Blob(content).to_raw())
                git(path, "fsck", "--strict", "--no-reflogs")
