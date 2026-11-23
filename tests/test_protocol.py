from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from gitdx import (Advertisement, AdvertisedRef, Capability, DELIMITER, FLUSH, RESPONSE_END, ObjectFormat,
                   ObjectFormatMismatch, ObjectId, ObjectType, Packet, PacketDecoder, ProtocolError, ProtocolLimitExceeded,
                   ProtocolLimits, RemoteProtocolError, Repository, Tag, encode_advertisement, encode_legacy_request,
                   encode_packet, encode_v2_command, negotiate, parse_ack, parse_advertisement, parse_legacy_request,
                   parse_ls_refs, parse_v2_command)
import test_objects as oracle
from test_checkout_plan import commit_tree
from test_objects import IDENTITY


def frames(wire):
    decoder=PacketDecoder();result=decoder.feed(wire);decoder.finish();return result


def advertisement(format=ObjectFormat.SHA1,version=0):
    oid=ObjectId("a"*format.hex_size,format)
    caps=(Capability(b"object-format",format.value.encode()),Capability(b"agent",b"test/1"))
    if version==2:return Advertisement(version,format,caps+(Capability(b"ls-refs",b"unborn"),Capability(b"fetch",b"shallow wait-for-done"),Capability(b"server-option")))
    return Advertisement(version,format,caps+(Capability(b"multi_ack"),Capability(b"side-band-64k"),Capability(b"symref",b"HEAD:refs/heads/main")),
                         (AdvertisedRef(b"HEAD",oid,symref=b"refs/heads/main"),AdvertisedRef(b"refs/heads/main",oid),AdvertisedRef(b"refs/tags/release",oid,peeled=oid)))


class ProtocolTests(unittest.TestCase):
    def test_legacy_ref_capability_peeled_and_empty_roundtrips_both_formats(self):
        for format in ObjectFormat:
            for version in (0,1):
                ad=advertisement(format,version)
                self.assertEqual(parse_advertisement(frames(encode_advertisement(ad))),ad)
                empty=replace(ad,refs=())
                self.assertEqual(parse_advertisement(frames(encode_advertisement(empty))),empty)
                self.assertEqual(negotiate(ad,format).format,format)
        self.assertEqual(parse_advertisement((FLUSH,)),Advertisement(0,ObjectFormat.SHA1,()))

    def test_legacy_want_have_done_roundtrips_and_ack_records(self):
        for format in ObjectFormat:
            ad=advertisement(format,1);oid=ad.refs[0].oid
            for done in (False,True):
                wire=encode_legacy_request(ad,format,(oid,),haves=(oid,),capabilities=(b"multi_ack",b"side-band-64k"),done=done)
                request=parse_legacy_request(frames(wire),ad,format)
                self.assertEqual(request.wants,(oid,));self.assertEqual(request.haves,(oid,));self.assertEqual(request.done,done)
            self.assertEqual(parse_legacy_request((FLUSH,),ad,format).wants,())
            self.assertEqual(negotiate(ad,format,capabilities=(b"side-band-64k",)).sideband_packet_size,65520)
            for suffix in (b"",b" continue",b" common",b" ready"):
                self.assertEqual(parse_ack(Packet(data=b"ACK "+str(oid).upper().encode()+suffix+b"\n"),format),(oid,suffix.strip() or None))
            self.assertEqual(parse_ack(Packet(data=b"NAK"),format),(None,None))
            with self.assertRaises(ProtocolError):encode_legacy_request(ad,format,(ObjectId("b"*format.hex_size,format),))

    def test_v2_capabilities_fetch_command_and_ls_refs_boundaries(self):
        for format in ObjectFormat:
            ad=advertisement(format,2);parsed=parse_advertisement(frames(encode_advertisement(ad)))
            self.assertEqual(parsed,ad);self.assertIn(b"wait-for-done",ad.features(b"fetch"))
            n=negotiate(ad,format,command=b"fetch",capabilities=(Capability(b"server-option",b"local option"),))
            oid=ObjectId("a"*format.hex_size,format)
            wire=encode_v2_command(n,(b"want "+str(oid).encode(),b"have "+str(oid).encode(),b"done"))
            request=parse_v2_command(frames(wire),ad,format)
            self.assertEqual(request.name,b"fetch");self.assertEqual(request.arguments[-1],b"done")
            records=parse_ls_refs((Packet(data=str(oid).encode()+b" HEAD symref-target:refs/heads/main\n"),
                                   Packet(data=b"unborn refs/heads/new symref-target:refs/heads/new\n"),FLUSH,RESPONSE_END),format)
            self.assertEqual(records[0].symref,b"refs/heads/main");self.assertIsNone(records[1].oid)
            self.assertIsNone(parse_v2_command((FLUSH,),ad,format))

    def test_formats_never_infer_translation_or_accept_wrong_width_wants(self):
        sha1=advertisement();sha256=advertisement(ObjectFormat.SHA256,2)
        with self.assertRaises(ObjectFormatMismatch):negotiate(sha1,ObjectFormat.SHA256)
        with self.assertRaises(ObjectFormatMismatch):negotiate(sha256,ObjectFormat.SHA1)
        request=(Packet(data=b"command=fetch\n"),DELIMITER,Packet(data=b"done\n"),FLUSH)
        with self.assertRaises(ObjectFormatMismatch):parse_v2_command(request,sha256,ObjectFormat.SHA256)
        with self.assertRaises(ProtocolError):encode_v2_command(negotiate(advertisement(version=2),ObjectFormat.SHA1,command=b"fetch"),(b"want "+b"a"*64,))
        unknown=(Packet(data=b"version 2\n"),Packet(data=b"object-format=unknown\n"),FLUSH)
        with self.assertRaises(ProtocolError):parse_advertisement(unknown)

    def test_bad_boundaries_duplicates_names_caps_limits_and_safe_errors(self):
        ad=advertisement(version=2)
        for packets in ((Packet(data=b"version 3\n"),FLUSH),
                        (Packet(data=b"version 2\n"),Packet(data=b"fetch\n"),Packet(data=b"fetch\n"),FLUSH),
                        (Packet(data=b"version 2\n"),DELIMITER,FLUSH)):
            with self.assertRaises(ProtocolError):parse_advertisement(packets)
        for packets in ((Packet(data=b"command=fetch"),FLUSH),
                        (Packet(data=b"command=fetch"),DELIMITER,DELIMITER,FLUSH),
                        (Packet(data=b"command=fetch"),DELIMITER,FLUSH,Packet(data=b"smuggled"))):
            with self.assertRaises(ProtocolError):parse_v2_command(packets,ad,ObjectFormat.SHA1)
        with self.assertRaises(ProtocolError):negotiate(ad,ObjectFormat.SHA1,capabilities=(b"unadvertised",),command=b"fetch")
        with self.assertRaises(ProtocolLimitExceeded):parse_advertisement(frames(encode_advertisement(ad)),limits=ProtocolLimits(max_capabilities=1))
        with self.assertRaises(ProtocolLimitExceeded):parse_advertisement(frames(encode_advertisement(advertisement())),limits=ProtocolLimits(max_refs=1))
        with self.assertRaises(ProtocolError):parse_ls_refs((Packet(data=b"a"*40+b" refs/../escape\n"),FLUSH),ObjectFormat.SHA1)
        for secret in (b"secret-peer-text",b"x"*1025):
            with self.assertRaises(ProtocolError) as caught:parse_advertisement((Packet(data=b"ERR "+secret),FLUSH))
            self.assertNotIn(secret.decode(),str(caught.exception))

    def test_legacy_symbolic_wire_names_preserve_non_ascii_bytes(self):
        name=b"refs/heads/caf\xc3\xa9"
        ad=replace(advertisement(),capabilities=(Capability(b"object-format",b"sha1"),Capability(b"symref",b"HEAD:"+name)),
                   refs=(AdvertisedRef(b"HEAD",ObjectId("a"*40),symref=name),AdvertisedRef(name,ObjectId("a"*40))))
        self.assertEqual(parse_advertisement(frames(encode_advertisement(ad))),ad)


class NativeProtocolTests(unittest.TestCase):
    setUpClass=classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def run_git(self,*args,data=None,version=0,trace=False):
        env={k:v for k,v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1",GIT_CONFIG_GLOBAL=os.devnull,GIT_PROTOCOL=f"version={version}",LC_ALL="C")
        if trace:env["GIT_TRACE_PACKET"]="1"
        return subprocess.run(["git",*args],input=data,capture_output=True,env=env,check=True)

    def test_stock_git_discovery_versions_ls_refs_and_transport_trace_both_formats(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                repo=Repository.init(Path(tmp)/"repo.git",bare=True,object_format=format)
                first=commit_tree(repo,{b"file":b"data"});repo.refs.update("refs/heads/main",first,expected=None)
                tag=repo.objects.write(Tag.create(first,target_type=ObjectType.COMMIT,name=b"release",message=b"tag\n",tagger=IDENTITY).to_raw())
                repo.refs.update("refs/tags/release",tag,expected=None)
                for version in (0,1,2):
                    wire=self.run_git("upload-pack","--stateless-rpc","--advertise-refs",str(repo.git_dir),version=version).stdout
                    ad=parse_advertisement(frames(wire))
                    self.assertEqual(ad.version,version);self.assertEqual(ad.format,format)
                    if version<2:
                        self.assertEqual(next(r for r in ad.refs if r.name==b"refs/heads/main").oid,first)
                        self.assertEqual(next(r for r in ad.refs if r.name==b"refs/tags/release").peeled,first)
                    else:
                        request=encode_v2_command(negotiate(ad,format,command=b"ls-refs"),(b"peel",b"symrefs"))
                        response=self.run_git("upload-pack","--stateless-rpc",str(repo.git_dir),data=request,version=2).stdout
                        refs=parse_ls_refs(frames(response),format)
                        self.assertEqual(next(r for r in refs if r.name==b"HEAD").symref,b"refs/heads/main")
                        self.assertEqual(next(r for r in refs if r.name==b"refs/tags/release").peeled,first)
                traced=self.run_git("-c","protocol.version=2","ls-remote",str(repo.git_dir),version=2,trace=True)
                self.assertIn(b"version 2",traced.stderr);self.assertIn(b"command=ls-refs",traced.stderr)
                self.assertIn(str(first).encode(),traced.stdout)

    def test_stock_git_empty_unborn_discovery_and_v2_ls_refs(self):
        for format in ObjectFormat:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                repo=Repository.init(Path(tmp)/"empty.git",bare=True,object_format=format)
                for version in (0,1,2):
                    ad=parse_advertisement(frames(self.run_git("upload-pack","--stateless-rpc","--advertise-refs",str(repo.git_dir),version=version).stdout))
                    self.assertEqual(ad.refs,())
                    if version==2:
                        self.assertEqual(ad.format,format)
                        arguments=(b"symrefs",b"unborn") if b"unborn" in ad.features(b"ls-refs") else (b"symrefs",)
                        wire=encode_v2_command(negotiate(ad,format,command=b"ls-refs"),arguments)
                        response=self.run_git("upload-pack","--stateless-rpc",str(repo.git_dir),data=wire,version=2).stdout
                        refs=parse_ls_refs(frames(response),format)
                        if b"unborn" in arguments:
                            self.assertEqual(refs,(AdvertisedRef(b"HEAD",None,symref=b"refs/heads/main"),))
                    elif not any(c.name==b"object-format" for c in ad.capabilities):
                        self.assertEqual(ad.format,ObjectFormat.SHA1)
                        if format is ObjectFormat.SHA256:
                            with self.assertRaises(ObjectFormatMismatch):negotiate(ad,format)
