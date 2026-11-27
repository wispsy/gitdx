from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from gitdx import (Commit, GraphTranslator, Header, InvalidObject, ObjectFormat, ObjectId,
                   ObjectLimitExceeded, ObjectType, PackWriteLimits, Repository, Tag, Tree, TreeEntry,
                   UnsupportedFormat)
from test_checkout_plan import commit_tree
from test_objects import IDENTITY
import test_objects as oracle


class TranslationTests(unittest.TestCase):
    def test_roundtrip_shared_graph_metadata_and_restart(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = Repository.init(root/'source', bare=True, object_format=format)
                other = ObjectFormat.SHA256 if format is ObjectFormat.SHA1 else ObjectFormat.SHA1
                target = Repository.init(root/'target', bare=True, object_format=other)
                back = Repository.init(root/'back', bare=True, object_format=format)
                first = commit_tree(source, {b'file\xff': b'binary\0\xff'})
                tree = Commit.from_bytes(source.objects.read(first).data, format).tree
                tip = source.objects.write(Commit.create(tree, author=IDENTITY, committer=IDENTITY,
                    message=b'\xff\nmessage\n', parents=(first,), extra_headers=(Header(b'encoding', b'ISO-8859-1'),
                    Header(b'x-opaque', b'\xff\n continuation'))).to_raw())
                tag = source.objects.write(Tag.create(tip, target_type=ObjectType.COMMIT, name=b'release', message=b'\xfftag\n').to_raw())
                with patch('subprocess.Popen', side_effect=AssertionError('runtime helper')):
                    result = GraphTranslator(source, target).translate((tag, tip, tag))
                    self.assertEqual(GraphTranslator(target, back).translate(result.roots).roots, (tag, tip, tag))
                    self.assertEqual(GraphTranslator(source, target).translate((tag, tip, tag)), result)
                for old, new in result.mappings:
                    self.assertEqual(back.objects.read(old), source.objects.read(old))
                self.assertEqual(target.refs.snapshot(), ())
                mapped = dict(result.mappings)
                target.objects.object_path(mapped[tip]).unlink()
                GraphTranslator(source, target).translate((tip,))
                self.assertEqual(target.objects.read(mapped[tip]).kind, ObjectType.COMMIT)
                with closing(sqlite3.connect(target.git_dir/'translation.sqlite3')) as db:
                    db.execute('UPDATE mappings SET target=? WHERE source=?', ('1'*other.hex_size, tip.hex))
                    db.commit()
                with self.assertRaises(InvalidObject): GraphTranslator(source, target).translate((tip,))

    def test_deep_histories_budgets_cancellation_and_ref_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);source = Repository.init(root/'source', bare=True)
            target = Repository.init(root/'target', bare=True, object_format=ObjectFormat.SHA256)
            tree = source.objects.write(Tree().to_raw());tip = None
            for i in range(1050):
                tip = source.objects.write(Commit.create(tree, author=IDENTITY, committer=IDENTITY,
                    message=str(i).encode(), parents=(() if tip is None else (tip,))).to_raw())
            with self.assertRaises(ObjectLimitExceeded):
                GraphTranslator(source, target, limits=PackWriteLimits(max_objects=10)).translate((tip,))
            with self.assertRaisesRegex(RuntimeError, 'cancel'):
                GraphTranslator(source, target).translate((tip,), check=lambda: (_ for _ in ()).throw(RuntimeError('cancel')))
            result = GraphTranslator(source, target).translate((tip,))
            self.assertEqual(len(result.mappings), 1051)
            target.refs.update('refs/heads/main', result.roots[0], expected=None)
            with self.assertRaises(UnsupportedFormat): GraphTranslator(source, target)
            with self.assertRaises(ValueError): GraphTranslator(source, source)

    def test_unsupported_objects_are_refused_before_any_target_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);source = Repository.init(root/'source', bare=True)
            target = Repository.init(root/'target', bare=True, object_format=ObjectFormat.SHA256)
            tree = source.objects.write(Tree().to_raw())
            bad = [source.objects.write(Commit.create(tree, author=IDENTITY, committer=IDENTITY, message=b'body',
                   extra_headers=(Header(name, b'opaque\n continuation'),)).to_raw()) for name in (b'gpgsig', b'gpgsig-sha256', b'mergetag')]
            bad.append(source.objects.write(Tag.create(tree, target_type=ObjectType.TREE, name=b'signed',
                       message=b'body\n-----BEGIN SSH SIGNATURE-----\nopaque').to_raw()))
            bad.append(source.objects.write(Tree((TreeEntry(0o160000, b'sub', ObjectId('1'*40)),)).to_raw()))
            before = tuple(target.git_dir.rglob('*'))
            for oid in bad:
                with self.subTest(oid=oid), self.assertRaises(UnsupportedFormat): GraphTranslator(source, target).translate((oid,))
            self.assertEqual(tuple(target.git_dir.rglob('*')), before)


class TranslationNativeTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_fsck_and_reverse_translation_both_directions(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);source = Repository.init(root/'source', bare=True, object_format=format)
                target = Repository.init(root/'target', bare=True,
                    object_format=ObjectFormat.SHA256 if format is ObjectFormat.SHA1 else ObjectFormat.SHA1)
                tip = commit_tree(source, {b'file': b'contents'})
                second = commit_tree(source, {b'file': b'next'}, parents=(tip,))
                result = GraphTranslator(source, target).translate((second,))
                target.refs.update('refs/heads/main', result.roots[0], expected=None)
                oracle.git(target.git_dir, 'fsck', '--strict', '--no-dangling')
                self.assertEqual(oracle.git(target.git_dir, 'show', 'main:file'), b'next')
