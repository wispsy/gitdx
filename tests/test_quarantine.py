from dataclasses import replace
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from gitdx import (Blob, Commit, InvalidObject, InvalidPack, MissingObject, ObjectFormat, ObjectLimitExceeded,
                   ObjectQuarantine, PackLimits, Repository, Tree, TreeEntry, unpack_objects)
from test_pack import fixture
from test_checkout_plan import commit_tree
from test_objects import IDENTITY
import test_objects as oracle


class QuarantineTests(unittest.TestCase):
    def test_validate_promote_and_cleanup_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                source=Repository.init(Path(tmp)/'source',object_format=format)
                target=Repository.init(Path(tmp)/'target',object_format=format)
                tip=commit_tree(source,{b'file':b'data'});bundle=source.pack((tip,))
                with ObjectQuarantine(target) as incoming:
                    incoming.receive(bundle.pack)
                    with self.assertRaises(MissingObject):target.objects.read(tip)
                    with self.assertRaises(ValueError):incoming.promote()
                    incoming.validate((tip,));incoming.promote()
                    self.assertEqual(target.objects.read(tip),source.objects.read(tip));path=incoming.path
                self.assertFalse(path.exists());self.assertTrue(target.refs.resolve().unborn)

    def test_forward_ref_and_offset_deltas_and_thin_refusal(self):
        for format in ObjectFormat:
            base=Blob(b'abc').to_raw();target=Blob(b'abcd').to_raw();program=b'\x03\x04\x90\x03\x01d'
            delta=b'\x76'+base.oid(format).bytes+zlib.compress(program)
            data,_,_=fixture((target,base),format,records=(delta,b'\x33'+zlib.compress(base.data)))
            self.assertEqual(dict(unpack_objects(data,format)),{base.oid(format):base,target.oid(format):target})
            first=b'\x33'+zlib.compress(base.data)
            data,_,_=fixture((base,target),format,records=(first,b'\x66'+bytes([len(first)])+zlib.compress(program)))
            self.assertEqual(dict(unpack_objects(data,format))[target.oid(format)],target)
            thin,_,_=fixture((target,),format,records=(delta,))
            with self.assertRaises(InvalidPack):unpack_objects(thin,format)

    def test_corrupt_truncated_duplicate_and_bounded_intake(self):
        raw=Blob(b'a'*500).to_raw();data,_,_=fixture((raw,))
        for malformed in (data[:-1],data+b'extra',data[:16],data[:-1]+bytes([data[-1]^1])):
            with self.subTest(size=len(malformed)),self.assertRaises(InvalidPack):unpack_objects(malformed,ObjectFormat.SHA1)
        duplicate,_,_=fixture((raw,raw))
        with self.assertRaises(InvalidPack):unpack_objects(duplicate,ObjectFormat.SHA1)
        for limits in (PackLimits(max_object_bytes=20),PackLimits(max_delta_bytes=20),PackLimits(max_pack_bytes=20)):
            with self.subTest(limits=limits),self.assertRaises(ObjectLimitExceeded):unpack_objects(data,ObjectFormat.SHA1,limits=limits)
        malformed=data[:12]+b'\x31'+zlib.compress(b'too long');malformed+=hashlib.sha1(malformed).digest()
        with self.assertRaises(InvalidPack):unpack_objects(malformed,ObjectFormat.SHA1)

    def test_missing_graph_wrong_type_and_cancel_leave_live_objects_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Repository.init(Path(tmp)/'source');target=Repository.init(Path(tmp)/'target')
            blob=source.objects.write(Blob(b'not a tree').to_raw())
            commit=source.objects.write(Commit.create(blob,author=IDENTITY,committer=IDENTITY,message=b'bad').to_raw())
            # A complete physical pack can still contain an invalid reachable graph.
            data,_,_=fixture((source.objects.read(blob),source.objects.read(commit)))
            with ObjectQuarantine(target) as incoming:
                incoming.receive(data)
                with self.assertRaises(InvalidObject):incoming.validate((commit,))
                with self.assertRaises(ValueError):incoming.promote()
            with self.assertRaises(MissingObject):target.objects.read(commit)
            tip=commit_tree(source,{b'file':b'data'});data=source.pack((tip,)).pack;calls=0
            def cancelled():
                nonlocal calls
                calls+=1
                if calls==4:raise InterruptedError('cancelled')
            with self.assertRaises(InterruptedError):
                with ObjectQuarantine(target,check=cancelled) as incoming:incoming.receive(data)
            self.assertEqual(list(target.objects.path.glob('incoming-*')),[])
            self.assertTrue(target.refs.resolve().unborn)


class QuarantineGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_ref_ofs_pack_intake_and_reverse_git_reads_both_formats(self):
        for format in ObjectFormat:
            for ofs in (False,True):
                with self.subTest(format=format,ofs=ofs),tempfile.TemporaryDirectory() as tmp:
                    repo=Repository.init(tmp,object_format=format)
                    raws=[Blob(b'shared\n'*500+str(n).encode()+b'tail\n'*100).to_raw() for n in range(12)]
                    oids=[repo.objects.write(raw) for raw in raws]
                    args=['pack-objects','--stdout','--window=20','--depth=20']
                    if ofs:args.append('--delta-base-offset')
                    data=oracle.git(repo.git_dir,*args,data=b''.join(str(oid).encode()+b'\n' for oid in oids))
                    self.assertEqual(dict(unpack_objects(data,format)),dict(zip(oids,raws)))
                    target=Repository.init(Path(tmp)/'target',bare=True,object_format=format)
                    with ObjectQuarantine(target) as incoming:
                        incoming.receive(data);incoming.validate(oids);incoming.promote()
                    for oid,raw in zip(oids,raws):self.assertEqual(oracle.git(target.git_dir,'cat-file','blob',str(oid)),raw.data)
