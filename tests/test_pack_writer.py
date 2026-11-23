from pathlib import Path
import tempfile
import unittest

from gitdx import (Blob, Commit, InvalidObject, MissingObject, ObjectFormat, ObjectId,
                   ObjectLimitExceeded, ObjectType, PackFile, PackIndex, PackWriteLimits,
                   Repository, Tag, Tree, TreeEntry)
from test_objects import IDENTITY, GitOracleTests, git


class PackWriterTests(unittest.TestCase):
    def test_empty_deterministic_bundle_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp)
            for roots in ((), []):
                bundle = repo.pack(roots)
                self.assertEqual(PackIndex.from_bytes(bundle.index).entries, ())
                PackFile(bundle.pack, PackIndex.from_bytes(bundle.index))
            a = repo.objects.write(Blob(b"a").to_raw());b = repo.objects.write(Blob(b"b").to_raw())
            self.assertEqual(repo.pack((a,b,a)), repo.pack((b,a)))
            for limits in (PackWriteLimits(max_objects=1), PackWriteLimits(max_bytes=1), PackWriteLimits(max_pack_bytes=1), PackWriteLimits(max_index_bytes=1)):
                with self.assertRaises(ObjectLimitExceeded):repo.pack((a,b), limits=limits)
            with self.assertRaises(InvalidObject):repo.pack((ObjectId("1"*64,ObjectFormat.SHA256),))

    def test_missing_corrupt_and_wrong_dependency_types_never_return_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp);missing = ObjectId("1"*40)
            tree = repo.objects.write(Tree((TreeEntry(0o100644,b"absent",missing),)).to_raw())
            with self.assertRaises(MissingObject):repo.pack((tree,))
            wrong = repo.objects.write(Commit.create(tree,author=IDENTITY,committer=IDENTITY,message=b"x",parents=(tree,)).to_raw())
            with self.assertRaises(InvalidObject):repo.pack((wrong,))
            blob = repo.objects.write(Blob(b"x").to_raw());repo.objects.object_path(blob).write_bytes(b"corrupt")
            with self.assertRaises(InvalidObject):repo.pack((blob,))

    def test_deep_tag_chain_and_external_gitlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository.init(tmp);root = repo.objects.write(Blob(b"base").to_raw());kind = ObjectType.BLOB
            for i in range(1100):
                root = repo.objects.write(Tag.create(root,target_type=kind,name=str(i).encode(),message=b"tag").to_raw());kind = ObjectType.TAG
            self.assertEqual(len(repo.pack((root,)).object_ids),1101)
            with self.assertRaises(ObjectLimitExceeded):repo.pack((root,),limits=PackWriteLimits(max_edges=10))
            tree = repo.objects.write(Tree((TreeEntry(0o160000,b"submodule",ObjectId("1"*40)),)).to_raw())
            self.assertEqual(repo.pack((tree,)).object_ids,(tree,))


class PackWriterGitTests(unittest.TestCase):
    setUpClass = classmethod(GitOracleTests.setUpClass.__func__)

    def test_git_index_unpack_and_fsck_for_empty_merge_tag_graphs_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);source = Repository.init(root/"source",object_format=format)
                (source.work_tree/"file").write_bytes(b"binary\0\xff");source.add("file")
                first = source.commit(author=IDENTITY,committer=IDENTITY,message=b"root\n",expected_head=None)
                second = source.commit(author=IDENTITY,committer=IDENTITY,message=b"second\n",expected_head=first)
                side = source.objects.write(Commit.create(source.log()[0].commit.tree,author=IDENTITY,committer=IDENTITY,message=b"side\n",parents=(first,)).to_raw())
                merge = source.commit(author=IDENTITY,committer=IDENTITY,message=b"merge\n",expected_head=second,parents=(second,side))
                tag = source.objects.write(Tag.create(merge,target_type=ObjectType.COMMIT,name=b"release",message=b"tag\n",tagger=IDENTITY).to_raw())
                for roots in ((),(tag,)):
                    bundle = source.pack(roots)
                    target = Repository.init(root/("target-"+str(len(roots))),object_format=format)
                    path = target.git_dir/"objects/pack"/("pack-"+bundle.checksum.hex()+".pack");path.write_bytes(bundle.pack)
                    self.assertEqual(git(target.work_tree,"index-pack","--strict",str(path)).strip().decode(),bundle.checksum.hex())
                    self.assertEqual(path.with_suffix(".idx").read_bytes(),bundle.index)
                    git(target.work_tree,"verify-pack","-v",str(path.with_suffix(".idx")))
                    for oid in bundle.object_ids:self.assertEqual(target.objects.read(oid),source.objects.read(oid))
                    unpacked = Repository.init(root/("unpacked-"+str(len(roots))),object_format=format)
                    git(unpacked.work_tree,"unpack-objects","--strict",data=bundle.pack)
                    if roots:
                        unpacked.refs.update("refs/heads/main",merge,expected=None)
                        unpacked.refs.update("refs/tags/release",tag,expected=None)
                    git(unpacked.work_tree,"fsck","--strict","--no-reflogs")
