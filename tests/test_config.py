from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from gitdx import Repository, UnsupportedFormat, parse_config, parse_values, read_config
import test_objects as oracle


class ConfigTests(unittest.TestCase):
    def test_order_values_boolean_case_and_redacted_representation(self):
        doc=parse_values(b'[Core]\nBaRe\nfileMode = 0\n[x "Case"] k=first\nk=" secret # ; " # comment\nk=\n[x.case]\nk=last\n')
        self.assertTrue(doc.boolean('CORE','BARE'));self.assertFalse(doc.boolean('core','filemode',True))
        self.assertEqual(doc.get_all('x','k',subsection='Case'),('first',' secret # ; ',''))
        self.assertEqual(doc.get('x','k',subsection='case'),'last')
        self.assertNotIn('secret',repr(doc))
        self.assertTrue(parse_config(b'[core]\nbare=-2\n').bare)
        self.assertFalse(parse_config(b'[core]\nbare=\n').bare)

    def test_rejects_malformed_required_semantics_and_bounds(self):
        bad=(b'[x]\na="unterminated',b'[x]\na=\\q',b'[x]\na=unfinished\\',b'[x]\na=\0',
             b'[include]\npath=../../secret',b'[includeIf "gitdir:*"]\npath=x',
             b'[core]\nsparsecheckout=yes',b'[core]\nworktree=elsewhere',
             b'[core]\nrepositoryformatversion=1\n[extensions]\npartialclone=origin')
        for data in bad:
            with self.subTest(data=data),self.assertRaises(UnsupportedFormat):parse_config(data)
        with self.assertRaises(UnsupportedFormat):parse_values(b'[x]\na=1\nb=2',max_entries=1)
        with self.assertRaises(UnsupportedFormat):parse_values(b'x'* (1024*1024+1))

    def test_explicit_confined_include_order_cycles_escapes_and_no_ambient_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'config';child=root/'child'
            config.write_bytes(b'[x]\nk=before\n[include]\npath=child\n[x]\nk=after\n')
            child.write_bytes(b'[x]\nk=included\n')
            with self.assertRaises(UnsupportedFormat):read_config(config,root=root)
            doc=read_config(config,root=root,includes=True)
            self.assertEqual(doc.get_all('x','k'),('before','included','after'))
            for value in ('../outside','/absolute','~/personal','C:/secret','child'):
                child.write_text('[include]\npath='+value+'\n')
                with self.subTest(value=value),self.assertRaises(UnsupportedFormat):read_config(config,root=root,includes=True)
            child.unlink()
            if os.name!='nt':
                child.symlink_to(config)
                with self.assertRaises(ValueError):read_config(config,root=root,includes=True)
            with patch.dict(os.environ,{'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.bare','GIT_CONFIG_VALUE_0':'true'}):
                self.assertEqual(parse_config(b'[core]\nbare=false').bare,False)


class ConfigGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_git_quoted_escaped_continued_and_multivar_values(self):
        data=(b'\xef\xbb\xbf[Example "Case"]\nkey= one  two # tail\n'
              b'key=" leading \\t;#\\ntrailing "\nkey=hel\\\nlo\n'
              b'key="quote\\\" and slash\\\\"\n[example.case]\nkey=lower\n')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'config';config.write_bytes(data)
            actual=oracle.git(root,'config','--file',str(config),'--null','--get-all','example.Case.key')
            self.assertEqual(tuple(actual.decode().split('\0')[:-1]),parse_values(data).get_all('example','key',subsection='Case'))
            self.assertEqual(oracle.git(root,'config','--file',str(config),'--get','example.case.key').strip().decode(),'lower')
