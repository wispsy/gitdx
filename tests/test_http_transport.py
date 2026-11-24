from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import shutil
import socket
import ssl
import subprocess
import sys
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from gitdx import (CloneIncomplete, FLUSH, HTTPAuthorization, ObjectFormat, ObjectFormatMismatch, Packet,
                   ProtocolError, RemoteProtocolError, Repository, SmartHTTPTransport, TransferCancelled,
                   TransferControl, TransferError, TransferLimits, TransferTimeout, UnsupportedFormat,
                   encode_packet)
import test_objects as oracle
from test_checkout_plan import commit_tree


@contextmanager
def http_fixture(root,*,mode='native',authorization=None,tls=False):
    """Development-only Git CGI fixture; the library never invokes Git."""
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_PROJECT_ROOT=str(root),GIT_HTTP_EXPORT_ALL='1')
    executable=shutil.which('git');requests=[];stop=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.dispatch()
        def do_POST(self):self.dispatch()
        def dispatch(self):
            requests.append((self.command,self.path,self.headers.get('Git-Protocol'),self.headers.get('Authorization')))
            if authorization is not None and self.headers.get('Authorization')!=authorization.value:
                self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
            if mode=='redirect':
                self.send_response(302);self.send_header('Location','http://127.0.0.1:1/fixture-secret');self.send_header('Content-Length','0');self.end_headers();return
            if mode=='wrong-type':
                self.send_response(200);self.send_header('Content-Type','text/plain');self.send_header('Content-Length','0');self.end_headers();return
            if mode in ('stall','drip'):
                self.send_response(200);self.send_header('Content-Type','application/x-git-upload-pack-advertisement')
                if mode=='drip':
                    self.send_header('Content-Length','1000');self.end_headers()
                    for _ in range(100):
                        if stop.wait(.02):return
                        try:self.wfile.write(b'0');self.wfile.flush()
                        except OSError:return
                else:
                    self.wfile.flush();stop.wait(3)
                return
            length=int(self.headers.get('Content-Length','0'));body=self.rfile.read(length) if length else b''
            parts=urlsplit(self.path);current=dict(env,REQUEST_METHOD=self.command,PATH_INFO=parts.path,
                QUERY_STRING=parts.query,CONTENT_TYPE=self.headers.get('Content-Type',''),CONTENT_LENGTH=str(length),
                SERVER_PROTOCOL='HTTP/1.1',REMOTE_ADDR='127.0.0.1')
            if self.headers.get('Git-Protocol'):current['HTTP_GIT_PROTOCOL']=self.headers['Git-Protocol']
            output=subprocess.run([executable,'http-backend'],input=body,capture_output=True,env=current,check=True).stdout
            header,separator,data=output.partition(b'\r\n\r\n')
            if not separator:header,separator,data=output.partition(b'\n\n')
            if not separator:raise AssertionError('invalid Git CGI fixture response')
            fields=[line.decode().split(':',1) for line in header.splitlines()]
            status=next((int(value.strip().split()[0]) for key,value in fields if key.lower()=='status'),200)
            self.send_response(status)
            for key,value in fields:
                if key.lower() not in ('status','content-length'):self.send_header(key,value.strip())
            declared=len(data)
            if self.command=='POST' and mode=='truncated-pack':data=data[:len(data)//2]
            if self.command=='POST' and mode=='corrupt-pack' and b'PACK' in data:
                cursor=data.index(b'PACK')+10;data=data[:cursor]+bytes([data[cursor]^1])+data[cursor+1:]
            self.send_header('Content-Length',str(declared));self.end_headers()
            try:self.wfile.write(data)
            except OSError:pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    if tls:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root/'fixture-cert.pem',root/'fixture-key.pem')
        server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True);thread.start()
    try:yield ('https' if tls else 'http')+'://127.0.0.1:'+str(server.server_port),requests
    finally:stop.set();server.shutdown();server.server_close();thread.join(timeout=2)


class HTTPPolicyTests(unittest.TestCase):
    def test_url_and_tls_policy_and_value_redaction(self):
        authorization=HTTPAuthorization.basic('user','fixture-secret')
        self.assertNotIn('fixture-secret',repr(authorization));self.assertNotIn(authorization.value,repr(SmartHTTPTransport('https://example.invalid/repo',authorization=authorization)))
        for url in ('file:///repo','ssh://example.invalid/a','https://user:fixture-secret@example.invalid/a',
                    'https://example.invalid/a?query=1','https://example.invalid/a#fragment','https://example.invalid/a/../b',
                    'https://example.invalid/a/%2e%2e/b','https://example.invalid/a%2fb','https://example.invalid/a%00',
                    'https://example.invalid/a%252e','https://example.invalid/a\r\nHeader:x','http://example.invalid/a'):
            with self.subTest(url=url),self.assertRaises(ValueError):SmartHTTPTransport(url)
        context=ssl._create_unverified_context()
        with self.assertRaises(ValueError):SmartHTTPTransport('https://example.invalid/a',ssl_context=context)
        for value in ('Bearer fixture\r\nHeader:x','unselected header'):
            with self.assertRaises(ValueError):HTTPAuthorization(value)

    def test_redirect_wrong_type_deadline_and_cancellation_without_credential_forwarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            for mode,expected in (('redirect',TransferError),('wrong-type',UnsupportedFormat),('stall',TransferTimeout),('drip',TransferTimeout)):
                with self.subTest(mode=mode),http_fixture(Path(tmp),mode=mode) as (url,requests):
                    transport=SmartHTTPTransport(url+'/repo',allow_http=True,authorization=HTTPAuthorization.bearer('fixture-secret'))
                    started=time.monotonic()
                    with self.assertRaises(expected) as caught:transport.discover(TransferControl(TransferLimits(max_seconds=.15)))
                    self.assertNotIn('fixture-secret',str(caught.exception));self.assertLess(time.monotonic()-started,2)
                    self.assertEqual(len(requests),1)
            with http_fixture(Path(tmp),mode='stall') as (url,requests):
                event=threading.Event();timer=threading.Timer(.1,event.set);timer.start()
                try:
                    with self.assertRaises(TransferCancelled):SmartHTTPTransport(url,allow_http=True).discover(TransferControl(cancel=event))
                finally:timer.cancel();timer.join()

    def test_sideband_remote_errors_and_unsupported_sections_are_bounded(self):
        transport=SmartHTTPTransport('https://example.invalid/repo')
        with self.assertRaises(RemoteProtocolError) as caught:
            transport._sidebands((Packet(data=b'\x03fixture-secret'),FLUSH),TransferControl())
        self.assertNotIn('fixture-secret',str(caught.exception))
        with self.assertRaises(ProtocolError):transport._sidebands((Packet(data=b'\x01PACK'),),TransferControl())

    def test_dns_wait_is_deadline_bounded_without_late_mutations(self):
        stop=threading.Event()
        def stalled(*args,**kwargs):stop.wait(2);return []
        transport=SmartHTTPTransport('http://example.invalid/repo',allow_http=True)
        try:
            with patch('gitdx.http_transport.socket.getaddrinfo',stalled):
                with self.assertRaises(TransferTimeout):transport.discover(TransferControl(TransferLimits(max_seconds=.1)))
        finally:stop.set()


class HTTPGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_smart_http_versions_auth_clone_fetch_and_reverse_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=Repository.init(root/'source.git',bare=True,object_format=format)
                first=commit_tree(source,{b'file':b'initial'});source.refs.update('refs/heads/main',first,expected=None)
                authorization=HTTPAuthorization.basic('fixture','fixture-secret')
                with http_fixture(root,authorization=authorization) as (url,requests):
                    endpoint=url+'/source.git'
                    with self.assertRaises(TransferError):Repository.clone(SmartHTTPTransport(endpoint,allow_http=True),root/'unauthorized')
                    self.assertFalse((root/'unauthorized').exists())
                    for version in (0,1,2):
                        transport=SmartHTTPTransport(endpoint,allow_http=True,authorization=authorization,protocol_version=version)
                        with patch.dict(os.environ,{'PATH':'','HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1','GIT_ASKPASS':'must-not-execute'}):
                            repo=Repository.clone(transport,root/('python-'+str(version)))
                        self.assertEqual(repo.object_format,format);self.assertEqual(repo.refs.resolve().oid,first)
                        self.assertEqual((repo.work_tree/'file').read_bytes(),b'initial')
                        self.assertEqual(oracle.git(repo.work_tree,'status','--porcelain'),b'')
                        second=commit_tree(source,{b'file':b'new'},parents=(first,));source.refs.update('refs/heads/main',second,expected=first)
                        result=repo.fetch(transport);self.assertTrue(result.complete)
                        self.assertEqual(repo.refs.resolve('refs/remotes/origin/main').oid,second)
                        self.assertEqual(repo.refs.resolve().oid,first)
                        source.refs.update('refs/heads/main',first,expected=second)
                    self.assertTrue(any(row[2]=='version=2' and row[0]=='POST' for row in requests))
                    # The development CGI fixture exposes Gitdx-written storage to a real Git client.
                    native=root/'native'
                    oracle.git(root,'-c','http.extraHeader=Authorization: '+authorization.value,'clone',endpoint,str(native))
                    self.assertEqual(oracle.git(native,'rev-parse','HEAD').strip().decode(),str(first))
                    other=Repository.init(root/'wrong-format',object_format=ObjectFormat.SHA256 if format is ObjectFormat.SHA1 else ObjectFormat.SHA1)
                    with self.assertRaises(ObjectFormatMismatch):other.fetch(SmartHTTPTransport(endpoint,allow_http=True,authorization=authorization))

    def test_v2_empty_unborn_sha256_discovery_and_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'empty.git',bare=True,object_format=ObjectFormat.SHA256,initial_branch='new')
            with http_fixture(root) as (url,requests):
                repo=Repository.clone(SmartHTTPTransport(url+'/empty.git',allow_http=True),root/'clone',object_format=ObjectFormat.SHA256)
                self.assertEqual(repo.object_format,ObjectFormat.SHA256);self.assertTrue(repo.refs.resolve().unborn)
                self.assertEqual(repo.refs.resolve().name,'refs/heads/new')

    def test_cli_http_authorization_is_selected_and_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source.git',bare=True)
            tip=commit_tree(source,{b'file':b'data'});source.refs.update('refs/heads/main',tip,expected=None)
            authorization=HTTPAuthorization.bearer('fixture-secret')
            with http_fixture(root,authorization=authorization) as (url,requests):
                args=[sys.executable,'-m','gitdx','clone',url+'/source.git',str(root/'clone'),'--transport','http','--allow-http','--authorization-env','GITDX_FIXTURE_AUTH']
                result=subprocess.run(args,capture_output=True,env=dict(os.environ,PATH='',GITDX_FIXTURE_AUTH=authorization.value),check=True)
                self.assertEqual(json.loads(result.stdout)['head'],str(tip))
                self.assertNotIn(authorization.value.encode(),result.stdout+result.stderr)
                self.assertNotIn(b'fixture-secret',(root/'clone/.git/config').read_bytes())
                result=subprocess.run(args,capture_output=True,env=dict(os.environ,PATH=''))
                self.assertEqual(result.returncode,1);self.assertNotIn(b'fixture-secret',result.stderr)

    def test_shallow_http_source_is_refused_before_refs_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source.git',bare=True)
            first=commit_tree(source,{b'file':b'old'});tip=commit_tree(source,{b'file':b'new'},parents=(first,))
            source.refs.update('refs/heads/main',tip,expected=None);(source.git_dir/'shallow').write_text(str(tip)+'\n')
            with http_fixture(root) as (url,requests):
                path=root/'clone'
                with self.assertRaises(CloneIncomplete) as caught:Repository.clone(SmartHTTPTransport(url+'/source.git',allow_http=True),path)
                self.assertIsInstance(caught.exception.__cause__,UnsupportedFormat)
                self.assertTrue(Repository.open(path).refs.resolve().unborn)

    def test_interrupted_and_corrupt_http_pack_leave_no_accepted_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source.git',bare=True)
            tip=commit_tree(source,{b'file':b'data'});source.refs.update('refs/heads/main',tip,expected=None)
            for mode in ('truncated-pack','corrupt-pack'):
                with self.subTest(mode=mode),http_fixture(root,mode=mode) as (url,requests):
                    path=root/mode
                    with self.assertRaises(CloneIncomplete):Repository.clone(SmartHTTPTransport(url+'/source.git',allow_http=True,protocol_version=0),path)
                    repo=Repository.open(path);self.assertTrue(repo.refs.resolve().unborn)
                    self.assertIsNone(repo.refs.read('refs/remotes/origin/main'))
                    self.assertEqual(list(repo.objects.path.glob('incoming-*')),[])

    @unittest.skipUnless(shutil.which('openssl'),'optional OpenSSL development certificate fixture')
    def test_https_trusted_certificate_and_default_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=Repository.init(root/'source.git',bare=True)
            tip=commit_tree(source,{b'file':b'data'});source.refs.update('refs/heads/main',tip,expected=None)
            # Ephemeral development keys never enter source control or tool output.
            (root/'fixture-tls.cnf').write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=server\n[dn]\nCN=localhost\n[server]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\nkeyUsage=digitalSignature,keyEncipherment,keyCertSign\n')
            subprocess.run([shutil.which('openssl'),'req','-x509','-newkey','rsa:2048','-nodes','-days','1','-sha256',
                            '-config',str(root/'fixture-tls.cnf'),
                            '-keyout',str(root/'fixture-key.pem'),'-out',str(root/'fixture-cert.pem')],capture_output=True,check=True)
            with http_fixture(root,tls=True) as (url,requests):
                with self.assertRaises(TransferError):SmartHTTPTransport(url+'/source.git').discover(TransferControl())
                context=ssl.create_default_context(cafile=root/'fixture-cert.pem')
                repo=Repository.clone(SmartHTTPTransport(url+'/source.git',ssl_context=context),root/'clone')
                self.assertEqual(repo.refs.resolve().oid,tip)
