from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitdx import QualificationEvidence, Repository, UnsupportedFormat, capability_report, implementation_digest, runtime_toolchain
from test_checkout_plan import commit_tree


def evidence():
    return QualificationEvidence(implementation_digest(),'source',dict(runtime_toolchain(),git='git version fixture',uv='uv fixture'),
                                 ('native-objects',),('objects.native',))


class CapabilityTests(unittest.TestCase):
    def test_default_has_no_test_claims_and_never_invokes_tools(self):
        with patch('subprocess.Popen',side_effect=AssertionError('runtime helper invoked')):
            report=capability_report()
        states={r['id']:r['state'] for r in report['capabilities']}
        self.assertEqual(states['objects.native'],'supported')
        self.assertEqual(states['repository.promisor'],'unsupported')
        self.assertEqual(states['config.includes-inspection'],'experimental')
        self.assertNotIn('tested',states.values())

    def test_matching_evidence_roundtrip_and_stale_or_false_claim_refusal(self):
        selected=QualificationEvidence.from_json(evidence().to_json())
        report=capability_report(selected)
        self.assertEqual(next(r for r in report['capabilities'] if r['id']=='objects.native')['state'],'tested')
        with self.assertRaises(ValueError):capability_report(replace(selected,implementation_digest='0'*64))
        with self.assertRaises(ValueError):capability_report(replace(selected,toolchain=dict(selected.toolchain,python='other')))
        for change in ({'oracle_required':False},{'capabilities':('repository.promisor',)},
                       {'capabilities':('storage.delta-ofs-ref',)},{'profile':'wheel'},{'formats':('sha1',)}):
            with self.subTest(change=change),self.assertRaises(ValueError):replace(selected,**change)
        data=json.loads(selected.to_json());data['extra']='unsupported'
        with self.assertRaises(ValueError):QualificationEvidence.from_json(json.dumps(data))
        with self.assertRaises(ValueError):QualificationEvidence.from_json('{"profile":"source","profile":"wheel"}')

    def test_promisor_and_other_required_metadata_refuse_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);repo=Repository.init(root);tip=commit_tree(repo,{b'file':b'data'})
            config=repo.git_dir/'config';original=config.read_bytes()
            for extra in (b'[remote "origin"]\npromisor=true\n',b'[remote "origin"]\npartialclonefilter=blob:none\n'):
                config.write_bytes(original+extra);before={p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
                with self.assertRaises(UnsupportedFormat):Repository.open(root)
                with self.assertRaises(UnsupportedFormat):repo.refs.update('refs/heads/main',tip,expected=None)
                with self.assertRaises(UnsupportedFormat):repo.add('file')
                self.assertEqual(before,{p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()})
            config.write_bytes(original)
            for name in ('objects/pack/pack-fixture.promisor','shallow','objects/info/alternates'):
                marker=repo.git_dir/name;marker.write_bytes(b'unsupported')
                with self.subTest(name=name),self.assertRaises(UnsupportedFormat):Repository.open(root)
                with self.assertRaises(UnsupportedFormat):repo.refs.update('refs/heads/main',tip,expected=None)
                marker.unlink()
            self.assertEqual(repo.refs.resolve().oid,None)

    def test_cli_json_without_git_or_a_repository(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run([sys.executable,'-m','gitdx','-C',tmp,'capabilities'],capture_output=True,env=dict(os.environ,PATH=''),check=True)
            self.assertEqual(json.loads(result.stdout)['schema_version'],1)
