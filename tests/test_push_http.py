from dataclasses import replace
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitdx import (CommandStatus, HTTPAuthorization, ObjectFormat, ObjectFormatMismatch, ProtocolError, PushState, PushUncertain,
                   PushUpdate, ReportStatus, Repository, SmartHTTPTransport, TransferError, encode_report_status,
                   parse_push_request)
import test_objects as oracle
from test_checkout_plan import commit_tree
from test_http_transport import http_fixture
from test_push_local import snapshot


def enable(target,policy=b''):
    with (target.git_dir/'config').open('ab') as stream:stream.write(b'[http]\nreceivepack=true\n'+policy)


class HTTPPushTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_http_push_create_update_delete_auth_and_reverse_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=Repository.init(root/'source',bare=True,object_format=format)
                target=Repository.init(root/'target.git',bare=True,object_format=format);enable(target)
                first=commit_tree(source,{b'file':b'initial'});second=commit_tree(source,{b'file':b'next'},parents=(first,))
                source.refs.update('refs/heads/main',first,expected=None);before=snapshot(source.git_dir)
                authorization=HTTPAuthorization.basic('fixture','fixture-secret')
                with http_fixture(root,authorization=authorization) as (url,requests):
                    endpoint=url+'/target.git';transport=SmartHTTPTransport(endpoint,allow_http=True,authorization=authorization)
                    with patch.dict(os.environ,{'PATH':'','HTTPS_PROXY':'http://127.0.0.1:1','GIT_ASKPASS':'never-execute'}):
                        result=source.push(transport,(PushUpdate('refs/heads/main',first,expected_old=None),))
                    self.assertTrue(result.complete);self.assertEqual(result.report_version,2);self.assertFalse(result.atomic)
                    self.assertTrue(source.push(transport,(PushUpdate('refs/heads/main',second,expected_old=first),)).complete)
                    self.assertTrue(source.push(transport,(PushUpdate('refs/tags/copy',second,expected_old=None),)).complete)
                    self.assertTrue(source.push(transport,(PushUpdate('refs/tags/copy',None,expected_old=second),)).complete)
                    # Force legacy report v1 while preserving the peer's real capabilities.
                    class LegacyReport:
                        def discover_push(self,control):
                            ad=transport.discover_push(control)
                            return replace(ad,capabilities=tuple(c for c in ad.capabilities if c.name!=b'report-status-v2'))
                        send_pack=transport.send_pack
                    result=source.push(LegacyReport(),(PushUpdate('refs/heads/report-one',second,expected_old=None),))
                    self.assertEqual(result.report_version,1);self.assertTrue(result.complete)
                    native=root/'native'
                    oracle.git(root,'-c','http.extraHeader=Authorization: '+authorization.value,'clone',endpoint,str(native))
                    (native/'file').write_bytes(b'native update');oracle.git(native,'add','file')
                    oracle.git(native,'-c','user.name=Qualification','-c','user.email=qualification@example.invalid','commit','-m','native')
                    native_tip=oracle.git(native,'rev-parse','HEAD').strip().decode()
                    oracle.git(native,'-c','http.extraHeader=Authorization: '+authorization.value,'push','origin','main')
                    self.assertEqual(str(target.refs.resolve().oid),native_tip)
                    self.assertTrue(any(row[1].endswith('/git-receive-pack') for row in requests))
                    self.assertTrue(all(row[2]!='version=2' for row in requests if 'receive-pack' in row[1]))
                self.assertEqual(snapshot(source.git_dir),before)

    def test_partial_and_advertised_atomic_results_match_native_refs(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=Repository.init(root/'source',bare=True,object_format=format)
                target=Repository.init(root/'target.git',bare=True,object_format=format);enable(target,b'[receive]\ndenyNonFastForwards=true\n')
                first=commit_tree(source,{b'file':b'initial'});other=commit_tree(source,{b'other':b'data'})
                with http_fixture(root) as (url,requests):
                    transport=SmartHTTPTransport(url+'/target.git',allow_http=True)
                    source.push(transport,(PushUpdate('refs/heads/main',first,expected_old=None),))
                    before=snapshot(source.git_dir)
                    proposals=(PushUpdate('refs/heads/main',other,expected_old=first,force=True),PushUpdate('refs/heads/accepted',other,expected_old=None))
                    result=source.push(transport,proposals)
                    self.assertEqual([o.state for o in result.outcomes],[PushState.REJECTED,PushState.ACCEPTED]);self.assertFalse(result.atomic)
                    proposals=(proposals[0],PushUpdate('refs/heads/atomic-child',other,expected_old=None))
                    result=source.push(transport,proposals,atomic=True)
                    self.assertTrue(result.atomic);self.assertTrue(result.peer_atomic);self.assertTrue(result.atomic_requested)
                    self.assertTrue(all(o.state is PushState.REJECTED for o in result.outcomes))
                    self.assertEqual(target.refs.resolve().oid,first);self.assertIsNone(target.refs.read('refs/heads/atomic-child'))
                    self.assertEqual(snapshot(source.git_dir),before)

    def test_stale_native_ref_after_discovery_is_reported_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source',bare=True);target=Repository.init(root/'target.git',bare=True);enable(target)
            first=commit_tree(source,{b'file':b'initial'});second=commit_tree(source,{b'file':b'next'},parents=(first,))
            competing=commit_tree(target,{b'file':b'competing'})
            with http_fixture(root) as (url,requests):
                transport=SmartHTTPTransport(url+'/target.git',allow_http=True)
                source.push(transport,(PushUpdate('refs/heads/main',first,expected_old=None),))
                class Racing:
                    discover_push=transport.discover_push
                    def send_pack(self,ad,request,control):
                        target.refs.update('refs/heads/main',competing,expected=first)
                        return transport.send_pack(ad,request,control)
                before=snapshot(source.git_dir);result=source.push(Racing(),(PushUpdate('refs/heads/main',second,expected_old=first),))
                self.assertEqual(result.outcomes[0].state,PushState.REJECTED)
                self.assertEqual(target.refs.resolve().oid,competing);self.assertEqual(snapshot(source.git_dir),before)

    def test_lost_report_after_native_acceptance_retains_unknown_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source',bare=True);target=Repository.init(root/'target.git',bare=True);enable(target)
            tip=commit_tree(source,{b'file':b'data'});before=snapshot(source.git_dir)
            with http_fixture(root,mode='lost-push-report') as (url,requests):
                transport=SmartHTTPTransport(url+'/target.git',allow_http=True)
                with self.assertRaises(PushUncertain) as caught:source.push(transport,(PushUpdate('refs/heads/main',tip,expected_old=None),))
                self.assertTrue(all(o.state is PushState.UNKNOWN for o in caught.exception.result.outcomes))
                self.assertFalse(caught.exception.result.atomic);self.assertEqual(target.refs.resolve().oid,tip)
            self.assertEqual(snapshot(source.git_dir),before)

    def test_auth_format_and_unsupported_atomic_refuse_without_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source',bare=True);target=Repository.init(root/'target.git',bare=True);enable(target,b'[receive]\nadvertiseAtomic=false\n')
            tip=commit_tree(source,{b'file':b'data'});authorization=HTTPAuthorization.bearer('fixture-secret');before=snapshot(source.git_dir)
            with http_fixture(root,authorization=authorization) as (url,requests):
                endpoint=url+'/target.git'
                with self.assertRaises(TransferError):source.push(SmartHTTPTransport(endpoint,allow_http=True),(PushUpdate('refs/heads/main',tip,expected_old=None),))
                transport=SmartHTTPTransport(endpoint,allow_http=True,authorization=authorization)
                with self.assertRaises(ProtocolError):source.push(transport,(PushUpdate('refs/heads/main',tip,expected_old=None),),atomic=True)
                other=Repository.init(root/'other',bare=True,object_format=ObjectFormat.SHA256);other_tip=commit_tree(other,{b'file':b'data'})
                with self.assertRaises(ObjectFormatMismatch):other.push(transport,(PushUpdate('refs/heads/main',other_tip,expected_old=None),))
                self.assertFalse(any(row[0]=='POST' for row in requests));self.assertTrue(target.refs.resolve().unborn)
            self.assertEqual(snapshot(source.git_dir),before)

    def test_inconsistent_atomic_report_cannot_claim_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source',bare=True);target=Repository.init(root/'target.git',bare=True);enable(target)
            tip=commit_tree(source,{b'file':b'data'})
            with http_fixture(root) as (url,requests):
                transport=SmartHTTPTransport(url+'/target.git',allow_http=True)
                class BrokenReport:
                    discover_push=transport.discover_push
                    def send_pack(self,ad,request,control):
                        parsed=parse_push_request(request,ad,ad.format)
                        report=ReportStatus(True,(CommandStatus(parsed.commands[0].name,True),CommandStatus(parsed.commands[1].name,False,b'fixture-secret')))
                        return encode_report_status(report,parsed.commands,ad.format,version=2)
                updates=tuple(PushUpdate('refs/heads/'+n,tip,expected_old=None) for n in ('one','two'))
                with self.assertRaises(PushUncertain) as caught:source.push(BrokenReport(),updates,atomic=True)
                self.assertTrue(caught.exception.result.atomic_requested);self.assertFalse(caught.exception.result.atomic)
                self.assertNotIn('fixture-secret',repr(caught.exception.result))

    def test_cli_explicit_credentials_and_unknown_result_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source',bare=True);target=Repository.init(root/'target.git',bare=True);enable(target)
            tip=commit_tree(source,{b'file':b'data'});authorization=HTTPAuthorization.bearer('fixture-secret')
            env=dict(os.environ,PATH='',GITDX_FIXTURE_AUTH=authorization.value)
            common=[sys.executable,'-m','gitdx','-C',str(source.git_dir),'push']
            with http_fixture(root,authorization=authorization) as (url,requests):
                args=common+[url+'/target.git','--transport','http','--allow-http','--authorization-env','GITDX_FIXTURE_AUTH','--ref','refs/heads/main','--new',str(tip),'--expected-old','absent']
                result=subprocess.run(args,capture_output=True,env=env,check=True)
                self.assertTrue(json.loads(result.stdout)['complete']);self.assertNotIn(b'fixture-secret',result.stdout+result.stderr)
                result=subprocess.run(args,capture_output=True,env=env)
                self.assertEqual(result.returncode,2);self.assertEqual(json.loads(result.stdout)['outcomes'][0]['state'],'rejected')
            with http_fixture(root,mode='lost-push-report') as (url,requests):
                args=common+[url+'/target.git','--transport','http','--allow-http','--ref','refs/heads/new','--new',str(tip),'--expected-old','absent']
                result=subprocess.run(args,capture_output=True,env=env)
                self.assertEqual(result.returncode,3);self.assertEqual(json.loads(result.stdout)['outcomes'][0]['state'],'unknown')
