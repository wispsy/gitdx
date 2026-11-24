from dataclasses import replace
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from gitdx import (Advertisement, AdvertisedRef, Capability, CommandStatus, FLUSH, ObjectFormat, ObjectFormatMismatch,
                   ObjectId, Packet, PacketDecoder, ProtocolError, ProtocolLimitExceeded, ProtocolLimits, PushCommand,
                   ReportOption, ReportStatus, Repository, encode_push_request, encode_report_status, negotiate_push,
                   parse_advertisement, parse_push_request, parse_report_status)
import test_objects as oracle
from test_checkout_plan import commit_tree


def frames(data):
    decoder=PacketDecoder();result=decoder.feed(data);decoder.finish();return result


def advertisement(format=ObjectFormat.SHA1):
    return Advertisement(0,format,(Capability(b'object-format',format.value.encode()),Capability(b'report-status'),
                                  Capability(b'report-status-v2'),Capability(b'delete-refs'),Capability(b'atomic')))


class PushProtocolTests(unittest.TestCase):
    def test_command_create_update_delete_and_raw_pack_boundaries_both_formats(self):
        for format in ObjectFormat:
            oid=ObjectId('a'*format.hex_size,format);other=ObjectId('b'*format.hex_size,format);ad=advertisement(format)
            commands=(PushCommand(b'refs/heads/new',None,oid),PushCommand(b'refs/heads/main',oid,other),PushCommand(b'refs/tags/old',other,None))
            n=negotiate_push(ad,format,atomic=True,sideband=False)
            wire=encode_push_request(n,commands,b'PACK\0raw\xff')
            parsed=parse_push_request(wire,ad,format)
            self.assertEqual(parsed.commands,commands);self.assertEqual(parsed.pack,b'PACK\0raw\xff')
            self.assertIn(b'atomic',{c.name for c in parsed.negotiated.capabilities})
            deletion=(commands[-1],)
            parsed=parse_push_request(encode_push_request(n,deletion),ad,format)
            self.assertEqual(parsed.pack,b'')

    def test_capability_format_and_required_command_refusals(self):
        ad=advertisement();oid=ObjectId('a'*40);command=PushCommand(b'refs/heads/main',None,oid)
        for peer in (replace(ad,version=2),replace(ad,capabilities=(Capability(b'object-format',b'sha1'),))):
            with self.assertRaises(ProtocolError):negotiate_push(peer,ObjectFormat.SHA1)
        with self.assertRaises(ObjectFormatMismatch):negotiate_push(ad,ObjectFormat.SHA256)
        without=replace(ad,capabilities=tuple(c for c in ad.capabilities if c.name!=b'atomic'))
        with self.assertRaises(ProtocolError):negotiate_push(without,ObjectFormat.SHA1,atomic=True)
        n=negotiate_push(ad,ObjectFormat.SHA1)
        for commands,pack in (((command,),b''),((PushCommand(command.name,oid,None),),b'PACK'),((command,command),b'PACK')):
            with self.assertRaises(ProtocolError):encode_push_request(n,commands,pack)
        with self.assertRaises(ProtocolError):PushCommand(command.name,None,None)
        with self.assertRaises(ProtocolLimitExceeded):encode_push_request(n,(command,),b'PACK',limits=ProtocolLimits(max_bytes=10))

    def test_malformed_requests_controls_capabilities_and_deletion_policy(self):
        ad=advertisement();oid=ObjectId('a'*40);command=PushCommand(b'refs/heads/main',None,oid)
        n=negotiate_push(ad,ObjectFormat.SHA1);wire=encode_push_request(n,(command,),b'PACK')
        for data in (wire[:5],b'0000PACK',wire.replace(b'0000PACK',b'0001PACK'),wire.replace(b'report-status-v2',b'push-cert-----')):
            with self.subTest(data=data[:8]),self.assertRaises(ProtocolError):parse_push_request(data,ad,ObjectFormat.SHA1)
        no_delete=replace(ad,capabilities=tuple(c for c in ad.capabilities if c.name!=b'delete-refs'))
        with self.assertRaises(ProtocolError):parse_push_request(encode_push_request(n,(PushCommand(command.name,oid,None),)),no_delete,ObjectFormat.SHA1)

    def test_report_versions_options_partial_rejections_and_redaction(self):
        for format in ObjectFormat:
            oid=ObjectId('a'*format.hex_size,format);commands=(PushCommand(b'refs/heads/main',None,oid),PushCommand(b'refs/heads/no',oid,None))
            for version in (1,2):
                options=(ReportOption(b'new-oid',str(oid).encode()),ReportOption(b'forced-update')) if version==2 else ()
                report=ReportStatus(True,(CommandStatus(commands[0].name,True,options=options),CommandStatus(commands[1].name,False,b'fixture-secret')))
                wire=encode_report_status(report,commands,format,version=version)
                self.assertEqual(parse_report_status(frames(wire),commands,format,version=version),report)
                self.assertNotIn('fixture-secret',repr(report))
            failed=ReportStatus(False,tuple(CommandStatus(c.name,False,b'unpacker error') for c in commands),b'fixture-secret')
            self.assertEqual(parse_report_status(frames(encode_report_status(failed,commands,format)),commands,format),failed)

    def test_incomplete_duplicate_unknown_and_contradictory_reports_fail(self):
        oid=ObjectId('a'*40);commands=(PushCommand(b'refs/heads/main',None,oid),)
        bad=((Packet(data=b'unpack ok'),FLUSH),
             (Packet(data=b'unpack ok'),Packet(data=b'ok refs/heads/unknown'),FLUSH),
             (Packet(data=b'unpack failed'),Packet(data=b'ok refs/heads/main'),FLUSH),
             (Packet(data=b'unpack ok'),Packet(data=b'ok refs/heads/main'),Packet(data=b'ok refs/heads/main'),FLUSH),
             (Packet(data=b'unpack ok'),Packet(data=b'ok refs/heads/main'),Packet(data=b'option forced-update'),FLUSH))
        for packets in bad:
            with self.subTest(packets=packets),self.assertRaises(ProtocolError):parse_report_status(packets,commands,ObjectFormat.SHA1)
        packets=(Packet(data=b'unpack ok'),Packet(data=b'ng refs/heads/main '+b'x'*1025),FLUSH)
        with self.assertRaises(ProtocolLimitExceeded):parse_report_status(packets,commands,ObjectFormat.SHA1)


class PushProtocolGitTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_native_receive_pack_commands_and_reports_both_formats_versions(self):
        for format in ObjectFormat:
            for version in (1,2):
                with self.subTest(format=format,report=version),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);source=Repository.init(root/'source',bare=True,object_format=format)
                    tip=commit_tree(source,{b'file':b'data'});target=Repository.init(root/'target.git',bare=True,object_format=format)
                    ad=parse_advertisement(frames(oracle.git(root,'receive-pack','--stateless-rpc','--advertise-refs',str(target.git_dir))))
                    n=negotiate_push(ad,format,sideband=False,report_version=version)
                    commands=(PushCommand(b'refs/heads/main',None,tip),PushCommand(b'refs/heads/temporary',None,tip))
                    request=encode_push_request(n,commands,source.pack((tip,)).pack)
                    response=oracle.git(root,'receive-pack','--stateless-rpc',str(target.git_dir),data=request)
                    report=parse_report_status(frames(response),commands,format,version=version)
                    self.assertTrue(report.unpack_ok);self.assertTrue(all(c.ok for c in report.commands))
                    delete=(PushCommand(b'refs/heads/temporary',tip,None),)
                    response=oracle.git(root,'receive-pack','--stateless-rpc',str(target.git_dir),data=encode_push_request(n,delete))
                    self.assertTrue(parse_report_status(frames(response),delete,format,version=version).commands[0].ok)
                    self.assertEqual(target.refs.resolve().oid,tip);self.assertIsNone(target.refs.read('refs/heads/temporary'))
