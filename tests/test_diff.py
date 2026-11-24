import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gitdx import (Blob, Commit, DiffLimits, InvalidObject, MissingObject, ObjectFormat, ObjectId,
                   ObjectLimitExceeded, Repository, Tree, TreeEntry, diff_bytes, quote_path, render_patch)
import test_objects as oracle
from test_objects import IDENTITY, git


def tree(repo, files):
    entries = []
    for path, (mode, data) in files.items():
        oid = data if isinstance(data, ObjectId) else repo.objects.write(Blob(data).to_raw())
        entries.append(TreeEntry(mode, path, oid))
    return repo.objects.write(Tree(tuple(sorted(entries, key=lambda e:e.sort_key)), repo.object_format).to_raw())


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.repo = Repository.init(self.root)

    def test_lf_only_bytes_and_no_final_newline(self):
        old, new = b'first\r\n\xff\nold', b'first\r\n\xff\nnew\n'
        result = diff_bytes(old, new)
        self.assertFalse(result.binary)
        self.assertEqual([(l.kind, l.data) for l in result.hunks[0].lines],
                         [(' ', b'first\r\n'), (' ', b'\xff\n'), ('-', b'old'), ('+', b'new\n')])
        a = tree(self.repo, {b'file':(0o100644, old)});b = tree(self.repo, {b'file':(0o100644, new)})
        patch = self.repo.diff(a, b)[0].patch()
        self.assertIn(b'-old\n\\ No newline at end of file\n+new\n', patch)
        self.assertEqual(diff_bytes(b'', b'x').hunks[0].old_count, 0)
        self.assertFalse(diff_bytes(b'x\0', b'y').hunks)
        self.assertTrue(diff_bytes(b'x\0', b'y').binary)

    def test_binary_modes_types_gitlinks_and_read_only_raw_paths(self):
        a = tree(self.repo, {b'\xff\n\x1b':(0o100644,b'a\0\xff'), b'mode':(0o100644,b'unchanged'),
                             b'type':(0o120000,b'link'),b'gone':(0o100644,b'')})
        opaque = ObjectId('1'*40)
        b = tree(self.repo, {b'\xff\n\x1b':(0o100644,b'b\0\xff'),b'mode':(0o100755,b'unchanged'),
                             b'type':(0o100644,b'link'),b'new':(0o100644,b''),b'sub':(0o160000,opaque)})
        before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        changes = self.repo.diff(a,b);indexed = {c.new_path or c.old_path:c for c in changes}
        self.assertEqual(indexed[b'type'].status,'T');self.assertFalse(indexed[b'mode'].hunks)
        self.assertIn(b'old mode 100644\nnew mode 100755',indexed[b'mode'].patch())
        self.assertTrue(indexed[b'sub'].binary);self.assertEqual(indexed[b'sub'].new_oid,opaque)
        change = indexed[b'\xff\n\x1b'];self.assertEqual(change.old_data,b'a\0\xff')
        self.assertIn(b'"a/\\377\\n\\033"',change.patch())
        self.assertEqual(before,{p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(render_patch(b'\x1b[31m\xff\r\t\n'),'\\x1b[31m\\xff\\x0d\\x09\n')
        self.assertEqual(quote_path(b'plain'),b'plain')

    def test_rename_threshold_binary_exact_matching_and_tie_breaks(self):
        a = tree(self.repo,{b'a':(0o100644,b'one\ntwo\n'),b'b':(0o100644,b'one\ntwo\n'),b'bin':(0o100644,b'\0x')})
        b = tree(self.repo,{b'x':(0o100644,b'one\ntwo\n'),b'y':(0o100644,b'one\nnew\n'),b'bin2':(0o100644,b'\0x')})
        self.assertEqual(len(self.repo.diff(a,b)),6)
        renames = self.repo.diff(a,b,detect_renames=True,rename_threshold=50)
        self.assertEqual([(c.old_path,c.new_path,c.similarity) for c in renames],[(b'bin',b'bin2',100),(b'a',b'x',100),(b'b',b'y',50)])
        exact = self.repo.diff(a,b,detect_renames=True,rename_threshold=100)
        self.assertEqual(sum(c.status=='R' for c in exact),2)
        with self.assertRaises(ObjectLimitExceeded):self.repo.diff(a,b,detect_renames=True,limits=DiffLimits(max_rename_pairs=8))
        with self.assertRaises(ValueError):self.repo.diff(a,b,rename_threshold=0)

    def test_limits_missing_and_wrong_types_fail_before_partial_results(self):
        for limits in (DiffLimits(max_bytes=2),DiffLimits(max_lines=1),DiffLimits(max_work=1)):
            with self.assertRaises(ObjectLimitExceeded):diff_bytes(b'a\nb\n',b'c\nd\n',limits=limits)
        with self.assertRaises(ObjectLimitExceeded):diff_bytes(b'a\nb\nc\n',b'x\nb\ny\n',context=0,limits=DiffLimits(max_hunks=1))
        with self.assertRaises(ValueError):diff_bytes(b'a',b'b',context=-1)
        a = tree(self.repo,{b'a':(0o100644,b'a'),b'b':(0o100644,b'b')})
        with self.assertRaises(ObjectLimitExceeded):self.repo.diff(None,a,limits=DiffLimits(max_paths=1))
        with self.assertRaises(ObjectLimitExceeded):self.repo.diff(None,a)[0].patch(limits=DiffLimits(max_output=1))
        with self.assertRaises(InvalidObject):self.repo.diff(None,ObjectId('0'*64,ObjectFormat.SHA256))
        blob = self.repo.objects.write(Blob(b'not a tree').to_raw())
        with self.assertRaises(InvalidObject):self.repo.diff(None,blob)
        with self.assertRaises(MissingObject):self.repo.diff(None,ObjectId('0'*40))
        malformed = tree(self.repo,{b'wrong':(0o100644,a)})
        with self.assertRaises(InvalidObject):self.repo.diff(None,malformed)
        nested = self.repo.objects.write(Tree((TreeEntry(0o40000,b'dir',a),)).to_raw())
        self.assertEqual([c.new_path for c in self.repo.diff(None,nested)],[b'dir/a',b'dir/b'])
        deeper = self.repo.objects.write(Tree((TreeEntry(0o40000,b'dir',nested),)).to_raw())
        with self.assertRaises(ObjectLimitExceeded):self.repo.diff(None,deeper,limits=DiffLimits(max_depth=1))

    def test_cli_without_git_emits_hex_and_safe_patch(self):
        a = tree(self.repo,{b'control':(0o100644,b'\x1b[0m\n')})
        command = [sys.executable,'-I','-m','gitdx','-C',str(self.root),'diff','empty',str(a)]
        env = dict(os.environ,PATH='')
        data = json.loads(subprocess.run(command,env=env,capture_output=True,check=True).stdout)
        self.assertEqual(data['new_path_hex'],b'control'.hex())
        self.assertEqual(data['hunks'][0]['lines'][0]['hex'],b'\x1b[0m\n'.hex())
        shown = subprocess.run(command+['--patch'],env=env,capture_output=True,check=True).stdout
        self.assertNotIn(b'\x1b',shown);self.assertIn(b'\\x1b[0m',shown)


class DiffGitTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_raw_results_and_simple_patch_both_formats_after_gc(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);repo = Repository.init(root,object_format=format)
                old = tree(repo,{b'file':(0o100644,b'one\nold'),b'mode':(0o100644,b'x'),b'binary':(0o100644,b'x\0')})
                new = tree(repo,{b'file':(0o100644,b'one\nnew\n'),b'mode':(0o100755,b'x'),b'binary':(0o100644,b'y\0'),b'new':(0o100644,b'new')})
                # Native raw records preserve modes, full IDs and statuses.
                native = git(root,'diff-tree','-r','--raw','-z','--no-abbrev','--no-renames',str(old),str(new)).split(b'\0')
                records = {}
                for i in range(0,len(native)-1,2):
                    am,bm,ao,bo,status = native[i][1:].split();records[native[i+1]] = (int(am,8),int(bm,8),ao.decode(),bo.decode(),status.decode())
                changes = repo.diff(old,new)
                for change in changes:
                    self.assertEqual(records[change.new_path or change.old_path],(change.old_mode or 0,change.new_mode or 0,
                        str(change.old_oid) if change.old_oid else '0'*format.hex_size,str(change.new_oid) if change.new_oid else '0'*format.hex_size,change.status))
                ours = next(c for c in changes if c.new_path==b'file').patch()
                native_patch = git(root,'diff-tree','-p','--no-renames','--full-index','--no-ext-diff','--no-textconv',str(old),str(new),'--','file')
                self.assertEqual(ours,native_patch)
                # Exact rename metadata agrees with native -M100%.
                renamed = tree(repo,{b'renamed':(0o100644,b'one\nold')})
                single = tree(repo,{b'file':(0o100644,b'one\nold')})
                self.assertIn(b'R100\0file\0renamed\0',git(root,'diff-tree','-r','--name-status','-z','-M100%',str(single),str(renamed)))
                self.assertEqual(repo.diff(single,renamed,detect_renames=True)[0].status,'R')
                first = repo.objects.write(Commit.create(old,author=IDENTITY,committer=IDENTITY,message=b'old').to_raw())
                second = repo.objects.write(Commit.create(new,author=IDENTITY,committer=IDENTITY,message=b'new',parents=(first,)).to_raw())
                repo.refs.update('refs/heads/main',second,expected=None)
                git(root,'gc','--prune=now')
                self.assertTrue(list((repo.git_dir/'objects/pack').glob('*.pack')))
                self.assertEqual(repo.diff(old,new),changes)
