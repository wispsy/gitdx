from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (Blob, Capability, FLUSH, ObjectFormat, ObjectFormatMismatch, Packet, PacketDecoder,
                   ProtocolCancelled, ProtocolError, ProtocolLimitExceeded, ProtocolLimits, Repository,
                   Tag, UnsupportedFormat, UploadPack, UploadPackLimits, encode_legacy_request,
                   encode_packet, encode_v2_command, negotiate, parse_advertisement, parse_ls_refs,
                   unpack_objects)
from test_checkout_plan import commit_tree
import test_objects as oracle


def frames(data):
    decoder=PacketDecoder();result=decoder.feed(data);decoder.finish();return result


@contextmanager
def upload_fixture(repo):
    """Development HTTP adapter; runtime serving belongs to Gitcx."""
    requests=[];errors=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.dispatch()
        def do_POST(self):self.dispatch()
        def dispatch(self):
            version=int((self.headers.get('Git-Protocol') or 'version=0').split('=')[-1]);service=UploadPack(repo)
            body=self.rfile.read(int(self.headers.get('Content-Length','0')))
            requests.append((self.command,version,body))
            try:result=service.advertise(version,http=True) if self.command=='GET' else service.respond(body,version)
            except Exception as error:
                errors.append(error);self.send_response(400);self.send_header('Content-Length','0');self.end_headers();return
            self.send_response(200);self.send_header('Cache-Control','no-cache')
            self.send_header('Content-Type','application/x-git-upload-pack-'+('advertisement' if self.command=='GET' else 'result'))
            self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port)+'/source.git',requests,errors
    finally:server.shutdown();server.server_close();worker.join()


class UploadPackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repo=Repository.init(self.temp.name,bare=True)
        self.tip=commit_tree(self.repo,{b'file':b'data'});self.repo.refs.update('refs/heads/main',self.tip,expected=None)

    def v2(self,command,args,*,service=None):
        ad=parse_advertisement(frames(UploadPack(self.repo).advertise(2)))
        negotiated=negotiate(ad,self.repo.object_format,command=command)
        return (service or UploadPack(self.repo)).respond(encode_v2_command(negotiated,args),2)

    def test_all_discovery_versions_and_ls_refs_without_runtime_helpers(self):
        with patch('subprocess.Popen',side_effect=AssertionError('runtime helper')):
            for version in (0,1,2):
                ad=parse_advertisement(frames(UploadPack(self.repo).advertise(version)))
                self.assertEqual(ad.version,version);self.assertEqual(ad.format,self.repo.object_format)
            records=parse_ls_refs(frames(self.v2(b'ls-refs',(b'peel',b'symrefs',b'ref-prefix HEAD'))),self.repo.object_format)
            self.assertEqual([(r.name,r.oid,r.symref) for r in records],[(b'HEAD',self.tip,b'refs/heads/main')])
            self.assertEqual(self.v2(b'ls-refs',(b'ref-prefix refs/tags/',)),b'0000')

    def test_v2_acknowledgments_and_complete_pack_including_requested_tags(self):
        tag=self.repo.objects.write(Tag.create(self.tip,target_type='commit',name=b'tag',tagger=b'Fixture <fixture@example.invalid> 1700000000 +0000',message=b'tag').to_raw())
        self.repo.refs.update('refs/tags/tag',tag,expected=None)
        response=self.v2(b'fetch',(b'want '+str(self.tip).encode(),b'have '+str(self.tip).encode()))
        self.assertEqual([p.data for p in frames(response)[:-1]],[b'acknowledgments\n',b'ACK '+str(self.tip).encode()+b'\n'])
        response=frames(self.v2(b'fetch',(b'want '+str(self.tip).encode(),b'include-tag',b'done')))
        self.assertEqual(response[0].data,b'packfile\n');pack=b''.join(p.data[1:] for p in response[1:-1])
        ids={oid for oid,_ in unpack_objects(pack,self.repo.object_format)}
        self.assertIn(self.tip,ids);self.assertIn(tag,ids)

    def test_legacy_rounds_raw_and_sideband_pack_and_no_selection(self):
        for version in (0,1):
            ad=parse_advertisement(frames(UploadPack(self.repo).advertise(version)))
            for caps in ((),(Capability(b'side-band-64k'),)):
                wire=encode_legacy_request(ad,self.repo.object_format,(self.tip,),capabilities=caps)
                response=UploadPack(self.repo).respond(wire,version)
                self.assertTrue(response.startswith(b'0008NAK\n'))
                pack=(b''.join(p.data[1:] for p in frames(response[8:])[:-1]) if caps else response[8:])
                self.assertIn(self.tip,dict(unpack_objects(pack,self.repo.object_format)))
            wire=encode_legacy_request(ad,self.repo.object_format,(self.tip,),haves=(self.tip,),done=False,
                                       capabilities=(Capability(b'multi_ack_detailed'),))
            self.assertIn(b' common\n',UploadPack(self.repo).respond(wire,version))
            self.assertEqual(UploadPack(self.repo).respond(b'0000',version),b'')

    def test_hidden_wants_unsupported_arguments_framing_format_and_limits(self):
        hidden=self.repo.objects.write(Blob(b'unreferenced secret').to_raw())
        for args in ((b'want '+str(hidden).encode(),b'done'),(b'want '+str(self.tip).encode(),b'deepen 1'),
                     (b'want '+str(self.tip).encode(),b'filter blob:none'),(b'want '+str(self.tip).encode(),b'done',b'done')):
            with self.subTest(args=args),self.assertRaises((ProtocolError,UnsupportedFormat)):self.v2(b'fetch',args)
        with self.assertRaises(ProtocolError):UploadPack(self.repo).respond(b'bad framing',2)
        with self.assertRaises(ProtocolLimitExceeded):UploadPack(self.repo,limits=UploadPackLimits(max_response_bytes=4)).advertise()
        tiny=UploadPackLimits(protocol=ProtocolLimits(max_bytes=4))
        with self.assertRaises(ProtocolLimitExceeded):UploadPack(self.repo,limits=tiny).respond(b'0008done',2)
        limited=UploadPack(self.repo,limits=UploadPackLimits(max_wants=1))
        with self.assertRaises(ProtocolLimitExceeded):self.v2(b'fetch',(b'want '+str(self.tip).encode(),)*2+(b'done',),service=limited)

    def test_cancellation_deadline_and_response_iterator_close(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(ProtocolCancelled):UploadPack(self.repo,cancel=cancel).advertise()
        cancel.clear();service=UploadPack(self.repo,cancel=cancel);parts=service.chunks(b'abcdef',size=2)
        self.assertEqual(next(parts),b'ab');cancel.set()
        with self.assertRaises(ProtocolCancelled):next(parts)
        parts.close()


class UploadPackNativeTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_stock_git_clone_fetch_tags_empty_and_all_protocol_versions(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);repo=Repository.init(root/'source.git',bare=True,object_format=format)
                tip=commit_tree(repo,{b'file':b'data'});repo.refs.update('refs/heads/main',tip,expected=None)
                repo.refs.update('refs/heads/side',tip,expected=None)
                oracle.git(repo.git_dir,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','tag','-am','tag','tag')
                with upload_fixture(repo) as (url,requests,errors):
                    for version in (0,1,2):
                        client=root/('client-'+str(version))
                        oracle.git(root,'-c','protocol.version='+str(version),'clone','--bare',url,str(client))
                        self.assertEqual(oracle.git(client,'rev-parse','HEAD').strip().decode(),str(tip))
                        new=commit_tree(repo,{b'file':str(version).encode()},parents=(tip,))
                        repo.refs.update('refs/heads/main',new,expected=tip)
                        oracle.git(client,'-c','protocol.version='+str(version),'fetch','--tags','origin','refs/heads/*:refs/heads/*')
                        self.assertEqual(oracle.git(client,'rev-parse','refs/heads/main').strip().decode(),str(new))
                        oracle.git(client,'fsck','--strict','--no-dangling')
                        repo.refs.update('refs/heads/main',tip,expected=new)
                    self.assertEqual(errors,[])
                    self.assertTrue(any(b'have ' in body for method,v,body in requests if method=='POST'))
                empty=Repository.init(root/'empty.git',bare=True,object_format=format,initial_branch='future')
                with upload_fixture(empty) as (url,requests,errors):
                    for version in (0,1,2):
                        client=root/('empty-'+str(version))
                        oracle.git(root,'-c','protocol.version='+str(version),'clone','--bare',url,str(client))
                        self.assertEqual(oracle.git(client,'rev-parse','--show-object-format').strip().decode(),format.value)
                    self.assertEqual(errors,[])
