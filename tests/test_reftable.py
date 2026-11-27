from dataclasses import replace
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from gitdx import (Blob, DirectRef, InvalidRef, ObjectFormat, ObjectId, ObjectLimitExceeded,
                   Repository, SymbolicRef, UnsupportedFormat)
from gitdx.reftable import InvalidReftable, Reftable, ReftableLimits, ReftableRefStore
from test_checkout_plan import commit_tree
import test_objects as oracle


def varint(value):
    result=bytearray((value&127,))
    while value>>7:
        value=(value>>7)-1;result.insert(0,128|(value&127))
    return bytes(result)


def table(records=(),format=ObjectFormat.SHA1,minimum=1,maximum=None,aligned=0):
    format=ObjectFormat(format);version=1 if format is ObjectFormat.SHA1 else 2
    header=b'REFT'+bytes((version,))+aligned.to_bytes(3,'big')+struct.pack('>QQ',minimum,minimum if maximum is None else maximum)
    if version==2:header+=b's256'
    payload=b'';restarts=[]
    for name,value in records:
        key=name.encode();restarts.append(len(header)+4+len(payload))
        kind=0 if value is None else 3 if isinstance(value,SymbolicRef) else 1
        payload+=b'\0'+varint((len(key)<<3)|kind)+key+b'\0'
        if kind==3:
            target=value.target.encode();payload+=varint(len(target))+target
        elif kind:payload+=value.oid.bytes
    block=b''
    if records:
        payload+=b''.join(p.to_bytes(3,'big') for p in restarts)+len(restarts).to_bytes(2,'big')
        block=b'r'+(len(header)+4+len(payload)).to_bytes(3,'big')+payload
    footer=header+bytes(40);footer+=zlib.crc32(footer).to_bytes(4,'big')
    return header+block+footer


def footer_changed(data,position,value):
    output=bytearray(data);header=24 if data[4]==1 else 28;start=len(data)-header-44
    struct.pack_into('>Q',output,start+header+8*position,value)
    output[-4:]=zlib.crc32(output[start:-4]).to_bytes(4,'big')
    return bytes(output)


class ReftableTests(unittest.TestCase):
    def test_empty_native_versions_and_typed_exact_names(self):
        for format in ObjectFormat:
            self.assertEqual(Reftable.from_bytes(table(format=format),format).refs,())
            oid=ObjectId('1'*format.hex_size,format)
            records=(('HEAD',SymbolicRef('refs/heads/Foo')),('refs/heads/Foo',DirectRef(oid)),
                     ('refs/heads/foo',DirectRef(oid)),('refs/tags/CON',DirectRef(oid)),('refs/tags/é',DirectRef(oid)))
            self.assertEqual(Reftable.from_bytes(table(records,format),format).refs,records)

    def test_corruption_header_footer_hash_offsets_restart_and_key_order(self):
        oid=ObjectId('1'*40);data=table((('refs/heads/a',DirectRef(oid)),('refs/heads/b',DirectRef(oid))))
        mutations=[b'bad',bytes((data[0]^1,))+data[1:],data[:-1]+bytes((data[-1]^1,)),
                   footer_changed(data,0,2),footer_changed(data,0,28)]
        # First restart must point at an actual complete key, not at its value.
        end=len(data)-68;bad=bytearray(data);bad[end-8:end-5]=(29).to_bytes(3,'big');mutations.append(bytes(bad))
        # Table blocks themselves have no checksum: key ordering is checked.
        bad=data.replace(b'refs/heads/b',b'refs/heads/a');mutations.append(bad)
        for bad in mutations:
            with self.subTest(data=bad[:32]),self.assertRaises((InvalidReftable,InvalidRef,UnsupportedFormat)):
                Reftable.from_bytes(bad)
        with self.assertRaises(InvalidReftable):Reftable.from_bytes(data,ObjectFormat.SHA256)
        with self.assertRaises(ObjectLimitExceeded):Reftable.from_bytes(data,limits=ReftableLimits(max_records=1))
        with self.assertRaises(ObjectLimitExceeded):Reftable.from_bytes(data,limits=ReftableLimits(max_bytes=8))

    def test_stack_overlay_tombstones_order_bounds_and_filename_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Repository.init(tmp,bare=True);root=repo.git_dir/'reftable';root.mkdir()
            config=repo.git_dir/'config';config.write_bytes(config.read_bytes().replace(b'repositoryformatversion = 0',b'repositoryformatversion = 1')+b'[extensions]\nrefStorage=reftable\n')
            (repo.git_dir/'HEAD').write_bytes(b'ref: refs/heads/.invalid\n')
            oid=ObjectId('1'*40)
            (root/'a.ref').write_bytes(table((('HEAD',SymbolicRef('refs/heads/a')),('refs/heads/a',DirectRef(oid)))))
            (root/'b.ref').write_bytes(table((('refs/heads/a',None),('refs/heads/b',DirectRef(oid))),minimum=2))
            listing=root/'tables.list';listing.write_bytes(b'a.ref\nb.ref\n')
            selected=Repository.open(tmp);self.assertTrue(selected.refs.resolve().unborn)
            self.assertEqual(selected.branches(),(('refs/heads/b',oid),))
            self.assertIsNone(selected.refs.read('refs/heads/B'))
            for names in (b'b.ref\na.ref\n',b'a.ref\na.ref\n',b'../a.ref\n',b'a.ref',b'missing.ref\n'):
                listing.write_bytes(names)
                with self.subTest(names=names),self.assertRaises((InvalidReftable,FileNotFoundError)):selected.refs.resolve()
            listing.write_bytes(b'a.ref\nb.ref\n')
            with self.assertRaises(ObjectLimitExceeded):ReftableRefStore(repo.git_dir,repo.objects,limits=ReftableLimits(max_tables=1)).read('HEAD')
            original=selected.refs._values
            with patch.object(ReftableRefStore,'_values',return_value={'HEAD':SymbolicRef('refs/heads/a'),'refs/heads/a':SymbolicRef('refs/heads/b'),'refs/heads/b':SymbolicRef('refs/heads/a')}):
                with self.assertRaises(InvalidRef):selected.refs.resolve()


class ReftableNativeTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_readonly_upload_pack_preserves_distinct_case_refs_with_native_reftable_client(self):
        from test_upload_pack import upload_fixture
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);repo=Repository.init(root/'source.git',bare=True,object_format=format)
                tip=commit_tree(repo,{b'file':b'old'});other=commit_tree(repo,{b'file':b'new'},parents=(tip,))
                repo.refs.update('refs/heads/main',tip,expected=None)
                oracle.git(repo.git_dir,'refs','migrate','--ref-format=reftable')
                oracle.git(repo.git_dir,'update-ref','refs/heads/Foo',str(tip))
                oracle.git(repo.git_dir,'update-ref','refs/heads/foo',str(other))
                source=Repository.open(repo.git_dir)
                with upload_fixture(source) as (url,requests,errors):
                    for version in (0,2):
                        client=root/('client-'+str(version))
                        oracle.git(root,'-c','protocol.version='+str(version),'clone','--bare','--ref-format=reftable',url,str(client))
                        self.assertEqual(Repository.open(client).refs.snapshot(),source.refs.snapshot())
                        self.assertEqual(Repository.open(client).refs.resolve('refs/heads/foo').oid,other)
                        oracle.git(client,'fsck','--strict','--no-dangling')
                    self.assertEqual(errors,[])

    def test_unaligned_indexed_and_log_only_fixtures_match_native(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                repo=Repository.init(tmp,bare=True,object_format=format);tip=commit_tree(repo,{b'file':b'data'})
                oracle.git(repo.git_dir,'refs','migrate','--ref-format=reftable')
                header=table(format=format)[:24 if format is ObjectFormat.SHA1 else 28]
                def block(kind,items,skip=0):
                    payload=b'';restarts=[]
                    for key,value in items:
                        restarts.append(skip+4+len(payload));payload+=b'\0'+varint(len(key)<<3|(1 if kind==b'r' else 0))+key
                        payload+=(b'\0'+tip.bytes if kind==b'r' else varint(value) if kind==b'i' else b'')
                    payload+=b''.join(p.to_bytes(3,'big') for p in restarts)+len(restarts).to_bytes(2,'big')
                    size=skip+4+len(payload)
                    return kind+size.to_bytes(3,'big')+(zlib.compress(payload) if kind==b'g' else payload)
                first=block(b'r',((b'refs/heads/Foo',None),),len(header));second_pos=len(header)+len(first)
                second=block(b'r',((b'refs/heads/foo',None),));index_pos=second_pos+len(second)
                index=block(b'i',((b'refs/heads/Foo',0),(b'refs/heads/foo',second_pos)))
                footer=header+struct.pack('>5Q',index_pos,0,0,0,0);footer+=zlib.crc32(footer).to_bytes(4,'big')
                data=header+first+second+index+footer
                self.assertEqual(len(Reftable.from_bytes(data,format).refs),2)
                root=repo.git_dir/'reftable';(root/'unaligned.ref').write_bytes(data);(root/'tables.list').write_bytes(b'unaligned.ref\n')
                expected=oracle.git(repo.git_dir,'for-each-ref','--format=%(refname) %(objectname)').splitlines()
                self.assertEqual([n.encode()+b' '+str(v.oid).encode() for n,v in Repository.open(tmp).refs.snapshot()],expected)
                with self.assertRaises(InvalidReftable):Reftable.from_bytes(footer_changed(data,0,second_pos),format)
                # A tombstone log-only table: the initial compressed block also
                # counts the file header in its declared inflated size.
                key=b'HEAD\0'+(0xffffffffffffffff-1).to_bytes(8,'big')
                log=block(b'g',((key,0),),len(header));footer=header+bytes(40);footer+=zlib.crc32(footer).to_bytes(4,'big')
                self.assertEqual(Reftable.from_bytes(header+log+footer,format).refs,())

    def test_native_migration_mixed_case_tags_history_and_write_refusal(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                repo=Repository.init(tmp,object_format=format);tip=commit_tree(repo,{b'file':b'data'});repo.refs.update('refs/heads/main',tip,expected=None)
                oracle.git(repo.git_dir,'refs','migrate','--ref-format=reftable')
                for name in ('Foo','foo','CON','é'):
                    oracle.git(repo.git_dir,'update-ref','refs/heads/'+name,str(tip))
                oracle.git(repo.git_dir,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','tag','-am','tag','tag')
                selected=Repository.open(tmp)
                before={p.relative_to(selected.git_dir):p.read_bytes() for p in selected.git_dir.rglob('*') if p.is_file()}
                with patch('subprocess.Popen',side_effect=AssertionError('runtime helper')):
                    self.assertEqual(selected.refs.resolve().oid,tip)
                    self.assertEqual(selected.resolve('refs/heads/Foo'),tip)
                    self.assertEqual(selected.resolve('refs/heads/foo'),tip)
                    self.assertEqual(selected.resolve('refs/heads/CON'),tip)
                    self.assertEqual(selected.resolve('refs/heads/é'),tip)
                    self.assertEqual([n for n,_ in selected.branches()],['refs/heads/CON','refs/heads/Foo','refs/heads/foo','refs/heads/main','refs/heads/é'])
                    self.assertEqual(selected.log()[0].oid,tip)
                    for call in (lambda:selected.refs.update('refs/heads/main',tip,expected=tip),lambda:selected.refs.delete('refs/heads/Foo',expected=tip),
                                 lambda:selected.refs.set_symbolic('HEAD','refs/heads/main',expected=None),lambda:selected.objects.write(Blob(b'new').to_raw()),
                                 lambda:selected.add('file'),lambda:selected.plan_checkout('Foo'),lambda:selected.index.transaction()):
                        with self.assertRaises(UnsupportedFormat):call()
                self.assertEqual(before,{p.relative_to(selected.git_dir):p.read_bytes() for p in selected.git_dir.rglob('*') if p.is_file()})
                self.assertEqual(str(selected.resolve('tag^{}')),oracle.git(repo.git_dir,'rev-parse','tag^{}').strip().decode())
                oracle.git(repo.git_dir,'fsck','--strict','--no-dangling')

    def test_native_multiblock_indexes_objects_and_logs(self):
        for format in ObjectFormat:
            for alignment in (256,512):
                with self.subTest(format=format,alignment=alignment),tempfile.TemporaryDirectory() as tmp:
                    repo=Repository.init(tmp,bare=True,object_format=format);tip=commit_tree(repo,{b'file':b'data'})
                    oracle.git(repo.git_dir,'refs','migrate','--ref-format=reftable')
                    commands=b''.join(b'update refs/heads/branch-%04d '%i+str(tip).encode()+b'\n' for i in range(160))
                    oracle.git(repo.git_dir,'-c','reftable.blockSize='+str(alignment),'-c','reftable.indexObjects=true',
                               '-c','core.logAllRefUpdates=always','update-ref','--stdin','-m','fixture',data=commands)
                    selected=Repository.open(tmp);self.assertEqual(len(selected.branches()),160)
                    expected=oracle.git(repo.git_dir,'for-each-ref','--format=%(refname) %(objectname)').splitlines()
                    actual=[n.encode()+b' '+str(v.oid).encode() for n,v in selected.refs.snapshot()]
                    self.assertEqual(actual,expected)
                    files=[(repo.git_dir/'reftable'/n).read_bytes() for n in (repo.git_dir/'reftable/tables.list').read_text().splitlines()]
                    self.assertTrue(any(struct.unpack('>5Q',data[-44:-4])[0] for data in files))
                    for data in files:Reftable.from_bytes(data,format)
                    with self.assertRaises(ObjectLimitExceeded):ReftableRefStore(repo.git_dir,repo.objects,limits=ReftableLimits(max_log_bytes=1)).read('HEAD')
