import asyncio
import unittest

from gitdx import (DELIMITER, FLUSH, RESPONSE_END, Packet, PacketDecoder, PacketKind, ProtocolBudget,
                   ProtocolCancelled, ProtocolError, ProtocolLimitExceeded, ProtocolLimits, ProtocolTimeout,
                   RemoteProtocolError, decode_sideband, encode_packet, encode_sideband, read_packets, write_packets)


class PacketTests(unittest.TestCase):
    def test_official_vectors_binary_empty_and_controls_at_every_split(self):
        vectors=((b"0006a\n",Packet(data=b"a\n")),(b"0005a",Packet(data=b"a")),
                 (b"000bfoobar\n",Packet(data=b"foobar\n")),(b"0004",Packet()),
                 (b"0000",FLUSH),(b"0001",DELIMITER),(b"0002",RESPONSE_END),
                 (b"0007\0\xff\n",Packet(data=b"\0\xff\n")))
        for wire,packet in vectors:
            self.assertEqual(encode_packet(packet),wire)
            for split in range(len(wire)+1):
                decoder=PacketDecoder();actual=decoder.feed(wire[:split])+decoder.feed(wire[split:]);decoder.finish()
                self.assertEqual(actual,(packet,))
        self.assertNotEqual(Packet(),FLUSH)
        decoder=PacketDecoder();self.assertEqual(decoder.feed(b"0004000000010002"),(Packet(),FLUSH,DELIMITER,RESPONSE_END));decoder.finish()

    def test_maximum_and_invalid_sizes_headers_and_truncation(self):
        maximum=Packet(data=b"x"*65516);wire=encode_packet(maximum)
        self.assertEqual(wire[:4],b"fff0");self.assertEqual(PacketDecoder().feed(wire),(maximum,))
        with self.assertRaises(ProtocolError):Packet(data=b"x"*65517)
        for raw in (b"0003",b"ffff",b"nope"):
            decoder=PacketDecoder()
            with self.assertRaises(ProtocolError):decoder.feed(raw)
            with self.assertRaises(ProtocolError):decoder.feed(b"0000")
        for raw in (b"0",b"000",b"0007ab"):
            decoder=PacketDecoder();decoder.feed(raw)
            with self.assertRaises(ProtocolError):decoder.finish()

    def test_count_bytes_deadline_and_cancellation_poison_decoders(self):
        for limits,wire in ((ProtocolLimits(max_packets=1),b"00000000"),(ProtocolLimits(max_bytes=4),b"0005x"),(ProtocolLimits(max_packet_size=4),b"0005x")):
            with self.assertRaises(ProtocolLimitExceeded):PacketDecoder(limits=limits).feed(wire)
        now=[0.0];budget=ProtocolBudget(ProtocolLimits(timeout=1),clock=lambda:now[0]);decoder=PacketDecoder(budget=budget)
        decoder.feed(b"00");now[0]=1
        with self.assertRaises(ProtocolTimeout):decoder.feed(b"00")
        with self.assertRaises(ProtocolCancelled):PacketDecoder(budget=ProtocolBudget(cancelled=lambda:True)).feed(b"")

    def test_sidebands_and_remote_errors_are_bounded_and_redacted(self):
        for channel in (1,2,3):
            wire=encode_sideband(channel,b"sensitive-peer-message")
            side=decode_sideband(PacketDecoder().feed(wire)[0])
            self.assertEqual(side.channel,channel);self.assertNotIn("sensitive-peer-message",repr(side))
            if channel==3:
                with self.assertRaises(RemoteProtocolError) as caught:side.raise_if_error()
                self.assertEqual(str(caught.exception),"remote protocol error")
        with self.assertRaises(ProtocolLimitExceeded):encode_sideband(3,b"a"*1025)
        for data in (b"",b"\0bad",b"\4bad"):
            with self.assertRaises(ProtocolError):decode_sideband(Packet(data=data))


class PacketStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_chunking_and_control_boundaries_do_not_consume_raw_pack(self):
        reader=asyncio.StreamReader();reader.feed_data(b"0005a0000PACKraw");reader.feed_eof()
        packets=read_packets(reader)
        self.assertEqual(await anext(packets),Packet(data=b"a"));self.assertEqual(await anext(packets),FLUSH)
        await packets.aclose()
        self.assertEqual(await reader.read(),b"PACKraw")
        reader=asyncio.StreamReader()
        async def chunks():
            for byte in b"0007a\0b0000":reader.feed_data(bytes((byte,)));await asyncio.sleep(0)
            reader.feed_eof()
        task=asyncio.create_task(chunks())
        self.assertEqual([p async for p in read_packets(reader)],[Packet(data=b"a\0b"),FLUSH]);await task

    async def test_timeout_and_cancellation_interrupt_stalled_reads(self):
        reader=asyncio.StreamReader()
        with self.assertRaises(ProtocolTimeout):await anext(read_packets(reader,limits=ProtocolLimits(timeout=0.01)))
        cancel=asyncio.Event();packets=read_packets(reader,cancel=cancel)
        task=asyncio.create_task(anext(packets));await asyncio.sleep(0);cancel.set()
        with self.assertRaises(ProtocolCancelled):await task
        await packets.aclose()

    async def test_writer_limits_stalled_drain_and_native_task_cancellation(self):
        class Writer:
            def __init__(self):self.data=bytearray();self.stall=False
            def write(self,data):self.data.extend(data)
            async def drain(self):
                if self.stall:await asyncio.Future()
        writer=Writer();await write_packets(writer,(Packet(data=b"binary\0"),FLUSH))
        self.assertEqual(bytes(writer.data),b"000bbinary"+bytes((0,))+b"0000")
        with self.assertRaises(ProtocolLimitExceeded):await write_packets(writer,(FLUSH,FLUSH),limits=ProtocolLimits(max_packets=1))
        writer.stall=True
        with self.assertRaises(ProtocolTimeout):await write_packets(writer,(FLUSH,),limits=ProtocolLimits(timeout=0.01))
        reader=asyncio.StreamReader();task=asyncio.create_task(anext(read_packets(reader)))
        await asyncio.sleep(0);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
