from dataclasses import replace
import hashlib
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from gitdx import Blob, Commit, InvalidObject, InvalidPack, MissingObject, ObjectFormat, ObjectId, ObjectLimitExceeded, PackFile, PackIndex, PackIndexEntry, PackLimits, Repository, Tree, TreeEntry, UnsupportedFormat, UnsafePath
import test_objects as oracle
from test_objects import IDENTITY, git


def fixture(raws=None, fmt=ObjectFormat.SHA1, *, version=2, pack_version=2, large=False, records=None):
    raws = tuple((Blob(b"hello").to_raw(),) if raws is None else raws)
    if records is None:
        records = []
        for raw in raws:
            size = len(raw.data)
            byte = ({"commit":1, "tree":2, "blob":3, "tag":4}[raw.kind.value] << 4) | (size & 15)
            size >>= 4;header = bytearray()
            while size:
                header.append(byte | 0x80);byte = size & 0x7f;size >>= 7
            header.append(byte);records.append(bytes(header) + zlib.compress(raw.data))
    pack = bytearray(struct.pack(">4sII", b"PACK", pack_version, len(raws)))
    entries = []
    for raw, record in zip(raws, records):
        entries.append((raw.oid(fmt), len(pack), zlib.crc32(record)))
        pack.extend(record)
    digest = hashlib.new(fmt.value, pack).digest();pack.extend(digest)
    entries.sort(key=lambda e:e[0].bytes)
    fanout = [sum(e[0].bytes[0] <= i for e in entries) for i in range(256)]
    index = bytearray(b"\xfftOc" + struct.pack(">I", 2) if version == 2 else b"")
    index.extend(struct.pack(">256I", *fanout))
    if version == 1:
        for oid, offset, crc in entries:index.extend(struct.pack(">I", offset) + oid.bytes)
    else:
        index.extend(b"".join(e[0].bytes for e in entries))
        index.extend(b"".join(struct.pack(">I", e[2]) for e in entries))
        index.extend(b"".join(struct.pack(">I", 0x80000000 | i if large else e[1]) for i,e in enumerate(entries)))
        if large:index.extend(b"".join(struct.pack(">Q", e[1]) for e in entries))
    index.extend(digest);index.extend(hashlib.new(fmt.value, index).digest())
    return bytes(pack), bytes(index), tuple(raw.oid(fmt) for raw in raws)


class PackTests(unittest.TestCase):
    def test_versions_hashes_crc_and_large_offset_tables(self):
        for fmt in ObjectFormat:
            for version in (1, 2):
                for pack_version in (2, 3):
                    data, raw_index, oids = fixture((Blob(b"a" * 10000).to_raw(), Blob(b"b").to_raw()), fmt,
                                                    version=version, pack_version=pack_version, large=version == 2)
                    index = PackIndex.from_bytes(raw_index, fmt)
                    pack = PackFile(data, index)
                    self.assertEqual(pack.read(oids[0]).data, b"a" * 10000)
                    self.assertEqual(pack.read(oids[1]).data, b"b")
            empty, idx, _ = fixture((), fmt)
            index = PackIndex.from_bytes(idx, fmt)
            self.assertEqual(index.entries, ())
            with self.assertRaises(MissingObject):PackFile(empty, index).read(ObjectId("1" * fmt.hex_size, fmt))

    def test_index_checksum_fanout_offsets_order_and_truncation(self):
        data, raw, oids = fixture()
        def checksum(body):return body + hashlib.sha1(body).digest()
        for bad in (raw[:-1], raw[:-20] + b"x" * 20, checksum(raw[:-21])):
            with self.assertRaises(InvalidPack):PackIndex.from_bytes(bad)
        fanout = bytearray(raw[:-20]);struct.pack_into(">I", fanout, 8, 1)
        with self.assertRaises(InvalidPack):PackIndex.from_bytes(checksum(fanout))
        unsupported = bytearray(raw);struct.pack_into(">I", unsupported, 4, 3)
        with self.assertRaises(UnsupportedFormat):PackIndex.from_bytes(bytes(unsupported))
        index = PackIndex.from_bytes(raw)
        for offset in (13, 10000):
            corrupt = replace(index, entries=(replace(index.entries[0], offset=offset),))
            with self.assertRaises(InvalidPack):PackFile(data, corrupt)
        with self.assertRaises(ObjectLimitExceeded):PackIndex.from_bytes(raw, limits=PackLimits(max_index_bytes=1))
        _, two, _ = fixture((Blob(b"a").to_raw(), Blob(b"b").to_raw()))
        with self.assertRaises(ObjectLimitExceeded):PackIndex.from_bytes(two, limits=PackLimits(max_objects=1))
        # A large-offset table must not allocate an unbounded set for unused rows.
        excess = raw[:-40] + b"\0" * 16 + raw[-40:-20]
        with self.assertRaises(InvalidPack):PackIndex.from_bytes(checksum(excess))

    def test_pack_trailer_count_crc_and_requested_hash(self):
        data, raw_index, oids = fixture()
        index = PackIndex.from_bytes(raw_index)
        for bad in (data[:-1], data[:-20] + b"x" * 20, b"JUNK" + data[4:]):
            with self.assertRaises(InvalidPack):PackFile(bad, index)
        crc = replace(index, entries=(replace(index.entries[0], crc32=0),))
        with self.assertRaises(InvalidPack):PackFile(data, crc)
        wrong_id = ObjectId("1" * 40)
        bad = replace(index, entries=(replace(index.entries[0], oid=wrong_id),))
        with self.assertRaises(InvalidPack):PackFile(data, bad).read(wrong_id)
        count = bytearray(data[:-20]);struct.pack_into(">I", count, 8, 2)
        digest = hashlib.sha1(count).digest()
        with self.assertRaises(InvalidPack):PackFile(bytes(count) + digest, replace(index, pack_checksum=digest))

    def test_object_headers_compression_frames_and_bounded_inflation(self):
        raw = Blob(b"x" * 10000).to_raw()
        for record in (b"\x50" + zlib.compress(b""), b"\x80" * 11, b"\x31" + zlib.compress(raw.data),
                       b"\x31not zlib", b"\x31" + zlib.compress(b"x") + b"trailing",
                       b"\x31" + zlib.compress(b"x")[:-1]):
            data, idx, oids = fixture((raw,), records=(record,))
            with self.subTest(record=record[:12]), self.assertRaises(InvalidPack):
                PackFile(data, PackIndex.from_bytes(idx)).read(oids[0])
        data, idx, oids = fixture((raw,))
        with self.assertRaises(ObjectLimitExceeded):PackFile(data, PackIndex.from_bytes(idx), limits=PackLimits(max_object_bytes=10))
        with self.assertRaises(ObjectLimitExceeded):PackFile(data, PackIndex.from_bytes(idx), limits=PackLimits(max_compressed_object_bytes=1))

    def test_delta_boundaries_and_reconstruction(self):
        base, target = Blob(b"abc").to_raw(), Blob(b"abcd").to_raw()
        first = b"\x33" + zlib.compress(base.data)
        instructions = zlib.compress(b"\x03\x04\x90\x03\x01d")
        for second in (b"\x76" + base.oid().bytes + instructions, b"\x66" + bytes([len(first)]) + instructions):
            data, idx, oids = fixture((base, target), records=(first, second))
            reader = PackFile(data, PackIndex.from_bytes(idx))
            self.assertEqual(reader.read(oids[0]), base)
            self.assertEqual(reader.read(oids[1]),target)
        for second in (b"\x66\0" + instructions, b"\x76short"):
            data, idx, _ = fixture((base, target), records=(first, second))
            with self.assertRaises(InvalidPack):PackFile(data, PackIndex.from_bytes(idx))


class PackGitOracleTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_pack_indexes_and_repository_fallback_both_hashes(self):
        for fmt in ObjectFormat:
            for index_version in ("1", "2"):
                with self.subTest(format=fmt, index=index_version), tempfile.TemporaryDirectory() as tmp:
                    repo = Repository.init(tmp, object_format=fmt)
                    blob_raw = Blob(b"native packed bytes\0\xff").to_raw()
                    blob = repo.objects.write(blob_raw)
                    tree = repo.objects.write(Tree((TreeEntry(0o100644, b"file", blob),), fmt).to_raw())
                    tip = repo.objects.write(Commit.create(tree, author=IDENTITY, committer=IDENTITY, message=b"initial\n").to_raw())
                    repo.refs.update("refs/heads/main", tip, expected=None)
                    digest = git(repo.git_dir, "pack-objects", "--all", "--window=0", "--depth=0", "--index-version=" + index_version,
                                 str(repo.git_dir / "objects/pack/pack")).strip().decode()
                    path = repo.git_dir / f"objects/pack/pack-{digest}.idx"
                    git(repo.git_dir, "verify-pack", "-v", str(path))
                    git(repo.git_dir, "prune-packed")
                    self.assertFalse(repo.objects.object_path(blob).exists())
                    index = PackIndex.from_bytes(path.read_bytes(), fmt)
                    self.assertEqual(index.version, int(index_version[0]))
                    self.assertEqual(repo.objects.read(blob), blob_raw)
                    self.assertEqual(repo.objects.read(tip).data, git(repo.git_dir, "cat-file", "commit", str(tip)))
                    (path.parent / "multi-pack-index").write_bytes(b"optional unsupported accelerator")
                    (path.parent / "pack-unused.rev").write_bytes(b"optional reverse index")
                    self.assertEqual(Repository.open(tmp).objects.read(blob), blob_raw)
                    bounded = Repository.open(tmp, pack_limits=PackLimits(max_scan_bytes=1))
                    with self.assertRaises(ObjectLimitExceeded):bounded.objects.read(blob)
                    repo.objects.object_path(blob).parent.mkdir(exist_ok=True)
                    repo.objects.object_path(blob).write_bytes(b"corrupt loose shadow")
                    with self.assertRaises(InvalidObject):repo.objects.read(blob)

    def test_native_git_accepts_generated_pack_v3(self):
        for fmt in ObjectFormat:
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                repo = Repository.init(tmp, bare=True, object_format=fmt)
                raw = Blob(b"pack version three\n").to_raw()
                data, _, oids = fixture((raw,), fmt, pack_version=3)
                git(repo.git_dir, "index-pack", "--stdin", "--strict", data=data)
                self.assertEqual(repo.objects.read(oids[0]), raw)


class PackStoreTests(unittest.TestCase):
    def test_pack_fallback_without_git_and_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            data, idx, oids = fixture()
            folder = repo.git_dir / "objects/pack"
            checksum = PackIndex.from_bytes(idx).pack_checksum.hex()
            (folder / f"pack-{checksum}.idx").write_bytes(idx)
            (folder / f"pack-{checksum}.pack").write_bytes(data)
            with patch.dict(os.environ, {"PATH": ""}):
                self.assertEqual(repo.objects.read(oids[0]).data, b"hello")
            with self.assertRaises(ObjectLimitExceeded):
                Repository.open(tmp, pack_limits=PackLimits(max_pack_bytes=1)).objects.read(oids[0])
            with self.assertRaises(ObjectLimitExceeded):
                Repository.open(tmp, pack_limits=PackLimits(max_scan_bytes=1)).objects.read(oids[0])

    def test_index_and_pack_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            data, idx, oids = fixture()
            digest = PackIndex.from_bytes(idx).pack_checksum.hex()
            folder = repo.git_dir / "objects/pack"
            outside = Path(tmp) / "outside";outside.write_bytes(idx)
            index = folder / f"pack-{digest}.idx"
            try:index.symlink_to(outside)
            except OSError as error:self.skipTest(str(error))
            with self.assertRaises(UnsafePath):repo.objects.read(oids[0])
            index.unlink();index.write_bytes(idx);outside.write_bytes(data)
            (folder / f"pack-{digest}.pack").symlink_to(outside)
            with self.assertRaises(UnsafePath):repo.objects.read(oids[0])
