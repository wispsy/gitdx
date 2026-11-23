from dataclasses import replace
import struct
import tempfile
import unittest
import zlib

from gitdx import Blob,InvalidObject,MissingObject,ObjectFormat,ObjectLimitExceeded,PackFile,PackIndex,PackLimits,Repository,apply_delta
from test_pack import fixture
import test_objects as oracle
from test_objects import git


def vi(n):
    result=bytearray()
    while n>127:result.append((n&127)|128);n>>=7
    result.append(n);return bytes(result)


def install(repo,data,index):
    digest=PackIndex.from_bytes(index,repo.object_format).pack_checksum.hex()
    path=repo.git_dir/"objects/pack"/("pack-"+digest)
    path.with_suffix(".idx").write_bytes(index);path.with_suffix(".pack").write_bytes(data)


class DeltaTests(unittest.TestCase):
    def test_copy_insert_default_copy_size_and_invalid_ranges(self):
        self.assertEqual(apply_delta(b"abc",b"\x03\x04\x90\x03\x01d"),b"abcd")
        self.assertEqual(apply_delta(b"x"*65536,vi(65536)+vi(65536)+b"\x80"),b"x"*65536)
        for data in (b"",b"\x80"*11,b"\x02\x03\x03abc",b"\x03\x01\0",b"\x03\x01\x91\x04\x01",b"\x03\x01\x90",b"\x03\x01\x02ab"):
            with self.subTest(data=data),self.assertRaises(InvalidObject):apply_delta(b"abc",data)
        with self.assertRaises(ObjectLimitExceeded):apply_delta(b"",vi(0)+vi(10**9),limits=PackLimits(max_object_bytes=10))
        with self.assertRaises(ObjectLimitExceeded):apply_delta(b"",b"\0\x02\x01a\x01b",limits=PackLimits(max_delta_operations=1))

    def test_deep_chain_iterative_and_aggregate_limits(self):
        raws=[Blob(b"a").to_raw()];records=[b"\x31"+zlib.compress(b"a")]
        for n in range(1,1101):
            program=vi(n)+vi(n+1)+b"\xb0"+struct.pack("<H",n)+b"\x01b"
            record=bytes([0x60|len(program),len(records[-1])])+zlib.compress(program)
            records.append(record);raws.append(Blob(b"a"+b"b"*n).to_raw())
        data,index,oids=fixture(raws,records=records)
        parsed=PackIndex.from_bytes(index)
        with self.assertRaises(ObjectLimitExceeded):PackFile(data,parsed).read(oids[-1])
        reader=PackFile(data,parsed,limits=PackLimits(max_delta_depth=1200))
        self.assertEqual(reader.read(oids[-1]),raws[-1])
        with self.assertRaises(ObjectLimitExceeded):PackFile(data,parsed,limits=PackLimits(max_delta_depth=1200,max_delta_bytes=100)).read(oids[-1])

    def test_cross_pack_and_loose_bases_explicit_store_policy(self):
        for fmt in ObjectFormat:
            for location in ("pack","loose"):
                with self.subTest(format=fmt,base=location),tempfile.TemporaryDirectory() as tmp:
                    repo=Repository.init(tmp,object_format=fmt)
                    base,target=Blob(b"abc").to_raw(),Blob(b"abcd").to_raw()
                    record=b"\x76"+base.oid(fmt).bytes+zlib.compress(b"\x03\x04\x90\x03\x01d")
                    data,index,oids=fixture((target,),fmt,records=(record,));install(repo,data,index)
                    with self.assertRaises(MissingObject):PackFile(data,PackIndex.from_bytes(index,fmt)).read(oids[0])
                    with self.assertRaises(MissingObject):repo.objects.read(oids[0])
                    if location=="pack":
                        a,b,_=fixture((base,),fmt);install(repo,a,b)
                    else:repo.objects.write(base)
                    self.assertEqual(repo.objects.read(oids[0]),target)

    def test_cycle_and_intermediate_hash_mismatch(self):
        a,b=Blob(b"x").to_raw(),Blob(b"y").to_raw()
        program=zlib.compress(b"\x01\x01\x90\x01")
        data,index,oids=fixture((a,b),records=(b"\x74"+b.oid().bytes+program,b"\x74"+a.oid().bytes+program))
        with self.assertRaisesRegex(InvalidObject,"cyclic"):PackFile(data,PackIndex.from_bytes(index)).read(oids[0])
        base,target=Blob(b"abc").to_raw(),Blob(b"wrong").to_raw()
        data,index,oids=fixture((base,target),records=(b"\x33"+zlib.compress(base.data),b"\x76"+base.oid().bytes+zlib.compress(b"\x03\x04\x90\x03\x01d")))
        with self.assertRaisesRegex(InvalidObject,"hash"):PackFile(data,PackIndex.from_bytes(index)).read(oids[1])


class DeltaGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_ref_and_ofs_deltas_both_hashes(self):
        for fmt in ObjectFormat:
            for ofs in (False,True):
                with self.subTest(format=fmt,ofs=ofs),tempfile.TemporaryDirectory() as tmp:
                    repo=Repository.init(tmp,object_format=fmt)
                    raws=[Blob((b"shared contents\n"*1000)+str(n).encode()+(b"tail\n"*500)).to_raw() for n in range(20)]
                    oids=[repo.objects.write(raw) for raw in raws]
                    arguments=["pack-objects","--window=20","--depth=20"]
                    if ofs:arguments.append("--delta-base-offset")
                    digest=git(repo.git_dir,*arguments,str(repo.git_dir/"objects/pack/pack"),data=b"".join(str(o).encode()+b"\n" for o in oids)).strip().decode()
                    verified=git(repo.git_dir,"verify-pack","-v",str(repo.git_dir/f"objects/pack/pack-{digest}.idx"))
                    self.assertTrue(any(len(line.split())==7 for line in verified.splitlines()))
                    git(repo.git_dir,"prune-packed")
                    for oid,raw in zip(oids,raws):self.assertEqual(repo.objects.read(oid),raw)
