from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from gitdx import Blob, IgnoredPath, Repository, UnsafePath, UnsupportedFormat
from gitdx.working_tree import WorkingTree
from gitdx.ignore import parse_ignore
import test_objects as oracle
from test_checkout_plan import commit_tree


class WorktreePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=Repository.init(self.root)
        (self.repo.git_dir/'info').mkdir()
        self.original=(self.repo.git_dir/'config').read_bytes()

    def configure(self,data):
        (self.repo.git_dir/'config').write_bytes(self.original+data)

    def test_parsed_last_value_policy_and_helpers_never_execute(self):
        (self.root/'file').write_bytes(b'CRLF\r\n$Id$\0')
        self.configure(b'[core]\nautocrlf = true\nautocrlf = "false"\n[alias]\nx=!malicious\n[credential]\nhelper=!malicious\n[gpg]\nprogram=malicious\n')
        with patch('subprocess.Popen',side_effect=AssertionError('helper executed')):
            self.repo.add('file');self.repo.status()
        entry=self.repo.index.read().entries[0]
        self.assertEqual(self.repo.objects.read(entry.oid),Blob(b'CRLF\r\n$Id$\0').to_raw())
        for config in (b'[core]\nautocrlf="true"',b'[core]\nautocrlf=tru\\\ne',b'[core]\nautocrlf=input',b'[filter "x"]\nprocess=malicious',b'[core]\nattributesfile=elsewhere'):
            self.configure(config)
            with self.subTest(config=config),self.assertRaises(UnsupportedFormat):self.repo.add('file')
            self.assertEqual(self.repo.index.read().entries[0],entry)

    def test_attribute_no_conversion_profile_and_target_validation(self):
        (self.root/'.gitattributes').write_bytes(b'* -text -ident -filter -working-tree-encoding\n*.bin binary\n')
        (self.root/'file').write_bytes(b'a\r\nb\r\n$Id$')
        self.repo.add('file','.gitattributes')
        entry=next(e for e in self.repo.index.read().entries if e.path==b'file')
        self.assertEqual(self.repo.objects.read(entry.oid).data,b'a\r\nb\r\n$Id$')
        first=commit_tree(self.repo,{b'.gitattributes':b'* -text\n',b'file':b'next\r\n'})
        self.repo.apply_checkout(self.repo.plan_checkout(first,overwrite_tracked=True))
        for source in (self.root/'.gitattributes',self.repo.git_dir/'info/attributes'):
            source.write_bytes(b'*.txt text eol=crlf\n')
            with self.subTest(source=source),self.assertRaises(UnsupportedFormat):self.repo.status()
            source.write_bytes(b'# safe\n')
        target=commit_tree(self.repo,{b'.gitattributes':b'* filter=run\n'})
        with self.assertRaises(UnsupportedFormat):self.repo.plan_checkout(target,overwrite_tracked=True)

    def test_confined_excludes_precedence_tracked_preservation_and_path_refusal(self):
        self.configure(b'[core]\nexcludesfile="custom.ignore"\n')
        (self.root/'custom.ignore').write_bytes(b'*.tmp\n!info.tmp\n')
        (self.repo.git_dir/'info/exclude').write_bytes(b'info.tmp\n!root.tmp\n')
        (self.root/'.gitignore').write_bytes(b'!info.tmp\nroot.tmp\n')
        tree=WorkingTree(self.repo)
        self.assertTrue(tree.ignored(b'x.tmp'));self.assertFalse(tree.ignored(b'info.tmp'));self.assertTrue(tree.ignored(b'root.tmp'))
        (self.root/'x.tmp').write_bytes(b'tracked')
        with self.assertRaises(IgnoredPath):self.repo.add('x.tmp')
        self.repo.add('x.tmp',force=True)
        (self.root/'x.tmp').write_bytes(b'updated');self.repo.add('x.tmp')
        for path in ('../outside','~/ignore','/absolute','C:/ignore'):
            self.configure(('[core]\nexcludesfile='+path+'\n').encode())
            with self.subTest(path=path),self.assertRaises(UnsupportedFormat):self.repo.status()
        self.configure(b'[core]\nexcludesfile=custom.ignore\n')
        if os.name!='nt':
            (self.root/'custom.ignore').unlink();(self.root/'custom.ignore').symlink_to(self.repo.git_dir/'config')
            with self.assertRaises(UnsafePath):self.repo.status()

    def test_ignore_bom_trailing_globstar_and_unsupported_classes(self):
        rules=parse_ignore(b'\xef\xbb\xbfdir/**\n')
        self.assertFalse(rules[0].matches(b'dir',True))
        self.assertTrue(rules[0].matches(b'dir/file',False))
        for pattern in (b'[[:digit:]]',b'[^a]'):
            with self.assertRaises(UnsupportedFormat):parse_ignore(pattern)


class WorktreePolicyGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_ignore_precedence_and_attribute_byte_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);repo=Repository.init(root)
            (repo.git_dir/'info').mkdir()
            with (repo.git_dir/'config').open('ab') as stream:stream.write(b'[core]\nexcludesfile=custom.ignore\n')
            (root/'custom.ignore').write_bytes(b'*.tmp\n!info.tmp\n')
            (repo.git_dir/'info/exclude').write_bytes(b'info.tmp\n!root.tmp\n')
            (root/'.gitignore').write_bytes(b'!info.tmp\nroot.tmp\n')
            native=oracle.git(root,'check-ignore','--no-index','-z','--stdin',data=b'x.tmp\0info.tmp\0root.tmp\0')
            tree=WorkingTree(repo)
            self.assertEqual(set(native.split(b'\0')[:-1]),{p for p in (b'x.tmp',b'info.tmp',b'root.tmp') if tree.ignored(p)})
            (root/'.gitattributes').write_bytes(b'* -text -ident -filter -working-tree-encoding\n')
            actual=oracle.git(root,'check-attr','-z','text','ident','filter','--','data')
            self.assertEqual(actual,b'data\0text\0unset\0data\0ident\0unset\0data\0filter\0unset\0')
            (root/'data').write_bytes(b'a\r\nb\r\n$Id$')
            repo.add('data');oid=repo.index.read().entries[0].oid
            self.assertEqual(oracle.git(root,'hash-object','--path=data','data').strip().decode(),str(oid))
            (root/'.gitignore').write_bytes(b'\xef\xbb\xbfdir/**\n!dir/keep\n')
            native=oracle.git(root,'check-ignore','--no-index','-z','--stdin',data=b'dir\0dir/file\0dir/keep\0')
            tree=WorkingTree(repo)
            self.assertEqual(set(native.split(b'\0')[:-1]),{p for p in (b'dir',b'dir/file',b'dir/keep') if tree.ignored(p,p==b'dir')})
