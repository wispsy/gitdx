import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from gitdx import Blob, Commit, Header, InvalidObject, ObjectFormat, ObjectId, ObjectType, RawObject, Tag, Tree, TreeEntry, parse_object


IDENTITY = b"Example <example@example.invalid> 1700000000 +0000"


def git(path, *args, data=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    return subprocess.run(["git", "-C", str(path), *args], input=data,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=True).stdout


class ObjectTests(unittest.TestCase):
    def test_known_empty_vectors(self):
        self.assertEqual(Blob(b"").to_raw().oid().hex, "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertEqual(Tree().to_raw().oid().hex, "4b825dc642cb6eb9a060e54bf8d69288fbee4904")
        self.assertEqual(Tree(format=ObjectFormat.SHA256).to_raw().oid(ObjectFormat.SHA256).hex,
                         "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321")

    def test_id_is_format_aware_and_full(self):
        for value in ("0" * 39, "A" * 40, "../" + "0" * 37, "g" * 40, "0" * 64):
            with self.subTest(value=value), self.assertRaises(InvalidObject):
                ObjectId(value)
        oid = ObjectId("1" * 64, ObjectFormat.SHA256)
        self.assertEqual(ObjectId.from_bytes(oid.bytes, oid.format), oid)
        self.assertEqual(str(oid), "1" * 64)
        with self.assertRaises(InvalidObject):
            ObjectId.from_bytes(b"short")

    def test_immutable_binary_payload(self):
        raw = Blob(b"\0\xff\r\n").to_raw()
        self.assertEqual(raw.encode(), b"blob 4\0\0\xff\r\n")
        self.assertEqual(parse_object(raw), Blob(raw.data))
        with self.assertRaises(TypeError):
            Blob(bytearray(b"mutable"))
        with self.assertRaises(InvalidObject):
            RawObject("invalid", b"")

    def test_tree_directory_sorting_and_raw_names(self):
        oid = Blob(b"x").to_raw().oid()
        tree = Tree((TreeEntry(0o40000, b"foo", oid), TreeEntry(0o100644, b"foo.bar", oid),
                     TreeEntry(0o120000, b"\xff\nname", oid)))
        self.assertEqual([e.name for e in tree.entries], [b"foo.bar", b"foo", b"\xff\nname"])
        self.assertEqual(Tree.from_bytes(tree.to_raw().data), tree)
        self.assertEqual(parse_object(tree.to_raw()), tree)

    def test_tree_rejects_bad_names_modes_duplicates_and_format(self):
        oid = Blob(b"x").to_raw().oid()
        for name in (b"", b".", b"..", b"../a", b"a/b", b"a\0b"):
            with self.subTest(name=name), self.assertRaises(InvalidObject):
                TreeEntry(0o100644, name, oid)
        with self.assertRaises(InvalidObject):
            TreeEntry(0o100600, b"a", oid)
        with self.assertRaises(InvalidObject):
            Tree((TreeEntry(0o100644, b"a", oid), TreeEntry(0o100755, b"a", oid)))
        with self.assertRaises(InvalidObject):
            Tree((TreeEntry(0o100644, b"a", oid),), ObjectFormat.SHA256)

    def test_tree_rejects_truncation_and_noncanonical_order(self):
        oid = Blob(b"x").to_raw().oid()
        a = Tree((TreeEntry(0o100644, b"a", oid),)).to_raw().data
        b = Tree((TreeEntry(0o100644, b"b", oid),)).to_raw().data
        for data in (b"x a\0" + oid.bytes, a[:-1], a + b"junk", b + a, a + a):
            with self.subTest(data=data), self.assertRaises(InvalidObject):
                Tree.from_bytes(data)

    def test_commit_preserves_unknown_and_multiline_headers(self):
        tree = Tree().to_raw().oid()
        signature = b"-----BEGIN PGP SIGNATURE-----\n \n opaque\n -----END PGP SIGNATURE-----"
        c = Commit.create(tree, author=IDENTITY, committer=IDENTITY, message=b"raw \xff\nno final newline",
                          extra_headers=(Header(b"encoding", b"ISO-8859-1"), Header(b"gpgsig", signature),
                                         Header(b"custom", b"first"), Header(b"custom", b"second")))
        self.assertEqual(Commit.from_bytes(c.to_raw().data).to_raw(), c.to_raw())
        self.assertEqual(c.tree, tree)
        self.assertEqual(c.parents, ())
        self.assertEqual(parse_object(c.to_raw()), c)

    def test_parents_keep_order_and_reject_mixed_formats(self):
        for fmt in ObjectFormat:
            tree = Tree(format=fmt).to_raw().oid(fmt)
            parents = (ObjectId("1" * fmt.hex_size, fmt), ObjectId("2" * fmt.hex_size, fmt))
            c = Commit.create(tree, author=IDENTITY, committer=IDENTITY, message=b"", parents=parents)
            self.assertEqual(Commit.from_bytes(c.to_raw().data, fmt).parents, parents)
        with self.assertRaises(InvalidObject):
            Commit.create(Tree().to_raw().oid(), author=IDENTITY, committer=IDENTITY, message=b"",
                          parents=(ObjectId("1" * 64, ObjectFormat.SHA256),))

    def test_malformed_headers(self):
        tree = Tree().to_raw().oid()
        valid = Commit.create(tree, author=IDENTITY, committer=IDENTITY, message=b"").to_raw().data
        for data in (b" orphan\n\nmessage", b"tree x", valid.replace(b"tree ", b"tree\0 "),
                     valid.replace(tree.hex.encode(), b"z" * 40), valid.replace(b"author", b"other"),
                     valid.replace(b"\ncommitter", b"\nauthor")):
            with self.subTest(data=data), self.assertRaises(InvalidObject):
                Commit.from_bytes(data)
        for name, value in ((b"bad name", b"x"), (b"a", b"x\nbad"), (b"a", b"\0")):
            with self.assertRaises(InvalidObject):
                Header(name, value)

    def test_annotated_tag_bytes_and_signature_message(self):
        for fmt in ObjectFormat:
            target = Blob(b"x").to_raw().oid(fmt)
            tag = Tag.create(target, target_type=ObjectType.BLOB, name=b"v1", tagger=IDENTITY,
                             message=b"message\n-----BEGIN PGP SIGNATURE-----\nopaque\xff")
            self.assertEqual(Tag.from_bytes(tag.to_raw().data, fmt), tag)
            self.assertEqual(tag.target, target)
            self.assertEqual(parse_object(tag.to_raw(), fmt), tag)
            with self.assertRaises(InvalidObject):
                Tag.from_bytes(tag.to_raw().data.replace(b"type blob", b"type unknown"), fmt)


class GitOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            if os.environ.get("GITDX_REQUIRE_GIT") == "1":
                raise RuntimeError("Git compatibility tests require the Git executable")
            raise unittest.SkipTest("Git compatibility oracle is unavailable")

    def test_native_object_interoperability_both_formats(self):
        for fmt in ObjectFormat:
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                git(path, "init", "--bare", "--object-format=" + fmt.value)
                def write(obj):
                    raw = obj.to_raw()
                    actual = git(path, "hash-object", "-t", raw.kind.value, "-w", "--stdin", data=raw.data).strip().decode()
                    self.assertEqual(actual, raw.oid(fmt).hex)
                    self.assertEqual(git(path, "cat-file", raw.kind.value, actual), raw.data)
                    return ObjectId(actual, fmt)
                blob = write(Blob(b"hello\0world\xff\n"))
                subtree = write(Tree(format=fmt))
                tree = Tree((TreeEntry(0o40000, b"foo", subtree), TreeEntry(0o100644, b"foo.bar", blob)), fmt)
                tree_oid = write(tree)
                lines = f"100644 blob {blob}\tfoo.bar\n040000 tree {subtree}\tfoo\n".encode()
                self.assertEqual(git(path, "mktree", data=lines).strip().decode(), tree_oid.hex)
                commit_oid = write(Commit.create(tree_oid, author=IDENTITY, committer=IDENTITY, message=b"Initial\n"))
                write(Tag.create(commit_oid, target_type=ObjectType.COMMIT, name=b"v1", message=b"tag\n", tagger=IDENTITY))
                git(path, "fsck", "--strict", "--no-reflogs")


if __name__ == "__main__":
    unittest.main()
