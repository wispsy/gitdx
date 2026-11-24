import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitdx import (AmbiguousRevision, Blob, Commit, InvalidObject, MissingObject, ObjectFormat, ObjectId,
                   ObjectLimitExceeded, Repository, RevisionLimits, RevisionNotFound, Tag, Tree,
                   UnsupportedFormat, UnsupportedRevision)
import test_objects as oracle
from test_objects import IDENTITY, git


def commit(repo, tree=None, parents=(), message=b'commit'):
    tree = tree or repo.objects.write(Tree(format=repo.object_format).to_raw())
    return repo.objects.write(Commit.create(tree,author=IDENTITY,committer=IDENTITY,message=message,parents=parents).to_raw())


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.repo = Repository.init(self.root)

    def test_refs_parents_tags_and_typed_peeling(self):
        root = commit(self.repo);left = commit(self.repo,parents=(root,),message=b'left')
        right = commit(self.repo,parents=(root,),message=b'right')
        tip = commit(self.repo,parents=(left,right));self.repo.refs.update('refs/heads/main',tip,expected=None)
        inner = self.repo.objects.write(Tag.create(tip,target_type='commit',name=b'inner',message=b'tag').to_raw())
        outer = self.repo.objects.write(Tag.create(inner,target_type='tag',name=b'outer',message=b'tag').to_raw())
        self.repo.refs.update('refs/tags/release',outer,expected=None)
        for expression, expected in {'HEAD':tip,'main':tip,'HEAD^':left,'HEAD^2':right,'HEAD^^':root,'HEAD~2':root,
                                     'release':outer,'release^{}':tip,'release^0':tip,'release~0':tip,'release^{tag}':outer,
                                     'release^{object}':outer,'release^{commit}':tip,'HEAD^2~1':root,
                                     str(tip).upper():tip}.items():
            self.assertEqual(self.repo.resolve(expression),expected,expression)
        expected_tree = next(self.repo.walk_history((tip,))).commit.tree
        self.assertEqual(self.repo.resolve('release^{tree}'),expected_tree)
        self.assertEqual(self.repo.resolve(tip),tip)
        with self.assertRaises(RevisionNotFound):self.repo.resolve('HEAD^3')
        with self.assertRaises(RevisionNotFound):self.repo.resolve('HEAD~3')
        with self.assertRaises(InvalidObject):self.repo.resolve('HEAD^{blob}')

    def test_abbreviation_ambiguity_including_unreachable_and_ref_collisions(self):
        seen = {};pair = None
        for n in range(3000):
            raw = Blob(str(n).encode()).to_raw();oid = self.repo.objects.write(raw)
            if oid.hex[:4] in seen:pair = (seen[oid.hex[:4]],oid);break
            seen[oid.hex[:4]] = oid
        self.assertIsNotNone(pair)
        with self.assertRaises(AmbiguousRevision):self.repo.resolve(pair[0].hex[:4])
        self.assertEqual(self.repo.resolve(pair[0].hex[:12]),pair[0])
        first = commit(self.repo);self.repo.refs.update('refs/heads/main',first,expected=None)
        self.repo.refs.update('refs/tags/main',first,expected=None)
        with self.assertRaises(AmbiguousRevision):self.repo.resolve('main')
        self.assertEqual(self.repo.resolve('refs/heads/main'),first)
        self.repo.refs.update('refs/heads/'+first.hex[:12],first,expected=None)
        with self.assertRaises(AmbiguousRevision):self.repo.resolve(first.hex[:12])

    def test_explicit_refusals_budgets_and_unchanged_metadata(self):
        with self.assertRaises(RevisionNotFound):self.repo.resolve('HEAD')
        first = commit(self.repo);tip = commit(self.repo,parents=(first,))
        self.repo.refs.update('refs/heads/main',tip,expected=None)
        (self.repo.objects.path/tip.hex[:2]/'temporary').write_bytes(b'bounded scan fixture')
        before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for expression in ('HEAD..main','HEAD...main','HEAD@{0}','@','@~1','HEAD:path',':0:file','HEAD^{/search}',
                           'HEAD^@','HEAD^!','HEAD^-1','HEAD~x','^HEAD','HEAD\\x','HEAD\n','HEAD^{unknown}'):
            with self.subTest(expression=expression),self.assertRaises(UnsupportedRevision):self.repo.resolve(expression)
        for limits, expression in ((RevisionLimits(max_steps=1),'HEAD~1'),(RevisionLimits(max_objects=1),'HEAD^'),
                                   (RevisionLimits(max_bytes=1),'HEAD'),(RevisionLimits(max_syntax_bytes=2),'HEAD'),
                                   (RevisionLimits(max_scan_entries=1),tip.hex[:4])):
            with self.subTest(expression=expression,limits=limits),self.assertRaises(ObjectLimitExceeded):self.repo.resolve(expression,limits=limits)
        with self.assertRaises(ObjectLimitExceeded):self.repo.resolve('HEAD~99999999999999999999')
        with self.assertRaises(RevisionNotFound):self.repo.resolve('absent')
        with self.assertRaises(MissingObject):self.repo.resolve('0'*40)
        with self.assertRaises(InvalidObject):self.repo.resolve(ObjectId('0'*64,ObjectFormat.SHA256))
        self.assertEqual(before,{p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_tag_type_depth_parent_corruption_and_replacement_refusal(self):
        first = commit(self.repo)
        bad = self.repo.objects.write(Tag.create(first,target_type='blob',name=b'bad',message=b'').to_raw())
        with self.assertRaises(InvalidObject):self.repo.resolve(str(bad)+'^{}')
        tag = first
        for n in range(3):tag = self.repo.objects.write(Tag.create(tag,target_type='commit' if n==0 else 'tag',name=b'tag',message=b'').to_raw())
        with self.assertRaises(ObjectLimitExceeded):self.repo.resolve(str(tag)+'^{}',limits=RevisionLimits(max_tag_depth=2))
        malformed = commit(self.repo,parents=(tag,))
        with self.assertRaises(InvalidObject):self.repo.resolve(str(malformed)+'^')
        merge = commit(self.repo,parents=(first,malformed))
        with self.assertRaises(ObjectLimitExceeded):self.repo.resolve(str(merge)+'^0',limits=RevisionLimits(max_parents=1))
        (self.repo.git_dir/'info').mkdir(exist_ok=True);(self.repo.git_dir/'info/grafts').write_bytes(b'')
        with self.assertRaises(UnsupportedFormat):self.repo.resolve(first)
        (self.repo.git_dir/'info/grafts').unlink()
        self.repo.refs.update('refs/replace/'+str(first),malformed,expected=None)
        with self.assertRaises(UnsupportedFormat):self.repo.resolve(first)

    def test_no_git_cli_and_no_subprocess_api(self):
        first = commit(self.repo);self.repo.refs.update('refs/heads/main',first,expected=None)
        with patch('subprocess.run',side_effect=AssertionError('runtime helper executed')):
            self.assertEqual(self.repo.resolve('HEAD^0'),first)
        result = subprocess.run([sys.executable,'-I','-m','gitdx','-C',str(self.root),'rev-parse','HEAD^{tree}'],
                                env=dict(os.environ,PATH=''),capture_output=True,check=True)
        self.assertEqual(result.stdout.strip().decode(),str(self.repo.resolve('HEAD^{tree}')))


class RevisionGitTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_rev_parse_native_annotated_merges_and_packed_prefixes_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);repo = Repository.init(root,object_format=format)
                first = commit(repo);left = commit(repo,parents=(first,),message=b'left');right = commit(repo,parents=(first,),message=b'right')
                tip = commit(repo,parents=(left,right));repo.refs.update('refs/heads/main',tip,expected=None)
                tag = repo.objects.write(Tag.create(tip,target_type='commit',name=b'release',message=b'tag').to_raw())
                outer = repo.objects.write(Tag.create(tag,target_type='tag',name=b'outer',message=b'tag').to_raw())
                repo.refs.update('refs/tags/release',outer,expected=None)
                git(root,'gc','--prune=now');git(root,'pack-refs','--all')
                for expr in ('HEAD','main','refs/tags/release','release^{}','release^0','release^{commit}',
                             'release^{tree}','release^{tag}','release^{object}','HEAD^2','HEAD~2','HEAD^2^0',tip.hex[:12],outer.hex[:12]):
                    self.assertEqual(str(repo.resolve(expr)),git(root,'rev-parse','--verify',expr).strip().decode(),expr)
                self.assertTrue(list((repo.git_dir/'objects/pack').glob('*.pack')))
                # Real loose and packed objects sharing a prefix remain ambiguous.
                seen = {}
                for n in range(4000):
                    raw = Blob(b'collision'+str(n).encode()).to_raw();oid = raw.oid(format)
                    if oid.hex[:4] in seen:
                        one = repo.objects.write(seen[oid.hex[:4]]);two = repo.objects.write(raw);break
                    seen[oid.hex[:4]] = raw
                else:self.fail('could not create abbreviation collision fixture')
                bundle = repo.pack((one,));folder = repo.git_dir/'objects/pack'
                (folder/('pack-'+bundle.checksum.hex()+'.pack')).write_bytes(bundle.pack)
                (folder/('pack-'+bundle.checksum.hex()+'.idx')).write_bytes(bundle.index)
                repo.objects.object_path(one).unlink()
                with self.assertRaises(AmbiguousRevision):repo.resolve(one.hex[:4])
                native = git(root,'rev-parse','--disambiguate='+one.hex[:4]).splitlines()
                self.assertEqual(set(native),{str(one).encode(),str(two).encode()})
