"""Binary Git packet lines with bounded incremental and async stream adapters."""

import asyncio
from dataclasses import dataclass, field, fields
from enum import StrEnum
import math
import re
import time

from .errors import GitdxError


class ProtocolError(GitdxError, ValueError):
    """Malformed or unsupported wire data; messages never copy peer payloads."""


class ProtocolLimitExceeded(ProtocolError):pass
class ProtocolTimeout(ProtocolError):pass
class ProtocolCancelled(ProtocolError):pass
class RemoteProtocolError(ProtocolError):pass


class PacketKind(StrEnum):
    DATA="data"
    FLUSH="flush"
    DELIMITER="delimiter"
    RESPONSE_END="response-end"


@dataclass(frozen=True, slots=True)
class Packet:
    kind: PacketKind = PacketKind.DATA
    data: bytes = field(default=b"",repr=False)

    def __post_init__(self):
        object.__setattr__(self,"kind",PacketKind(self.kind))
        if not isinstance(self.data,bytes):raise TypeError("packet payload must be immutable bytes")
        if len(self.data)>65516 or (self.kind is not PacketKind.DATA and self.data):raise ProtocolError("invalid packet payload size")


FLUSH=Packet(PacketKind.FLUSH)
DELIMITER=Packet(PacketKind.DELIMITER)
RESPONSE_END=Packet(PacketKind.RESPONSE_END)


@dataclass(frozen=True, slots=True)
class ProtocolLimits:
    max_packet_size: int = 65520
    max_packets: int = 100_000
    max_bytes: int = 64*1024*1024
    max_error_bytes: int = 1024
    timeout: float = 30.0
    max_refs: int = 100_000
    max_capabilities: int = 256
    max_name_bytes: int = 4096

    def __post_init__(self):
        if any(type(getattr(self,f.name)) is not int or getattr(self,f.name)<=0 for f in fields(self) if f.name!="timeout"):
            raise ValueError("protocol limits must be positive integers")
        if not 4<=self.max_packet_size<=65520 or type(self.timeout) not in (int,float) or not math.isfinite(self.timeout) or self.timeout<=0:
            raise ValueError("invalid packet size or protocol deadline")


class ProtocolBudget:
    def __init__(self,limits=None,*,clock=time.monotonic,cancelled=None):
        self.limits=limits or ProtocolLimits();self.clock=clock;self.cancelled=cancelled
        self.deadline=clock()+self.limits.timeout;self.bytes=self.packets=0

    def check(self):
        if self.cancelled is not None and self.cancelled():raise ProtocolCancelled("protocol operation was cancelled")
        if self.clock()>=self.deadline:raise ProtocolTimeout("protocol deadline exceeded")

    @property
    def remaining(self):
        self.check();return max(0,self.deadline-self.clock())

    def consume_bytes(self,count):
        self.check();self.bytes+=count
        if self.bytes>self.limits.max_bytes:raise ProtocolLimitExceeded("protocol byte limit exceeded")

    def consume_packet(self):
        self.check();self.packets+=1
        if self.packets>self.limits.max_packets:raise ProtocolLimitExceeded("protocol packet count exceeded")


def encode_packet(packet,*,limits=None):
    if not isinstance(packet,Packet):raise TypeError("expected a Packet")
    if packet.kind is not PacketKind.DATA:
        return {PacketKind.FLUSH:b"0000",PacketKind.DELIMITER:b"0001",PacketKind.RESPONSE_END:b"0002"}[packet.kind]
    if len(packet.data)+4>(limits or ProtocolLimits()).max_packet_size:raise ProtocolLimitExceeded("packet size limit exceeded")
    return f"{len(packet.data)+4:04x}".encode("ascii")+packet.data


class PacketDecoder:
    """One bounded partial frame; framing errors consume this decoder."""

    def __init__(self,*,limits=None,budget=None):
        if limits is not None and budget is not None:raise ValueError("select limits or a shared budget")
        self.budget=budget or ProtocolBudget(limits);self.buffer=bytearray();self.expected=None;self.closed=False

    @property
    def needed(self):return (4 if self.expected is None else self.expected)-len(self.buffer)

    def feed(self,data):
        if self.closed:raise ProtocolError("packet decoder is no longer active")
        try:
            if not isinstance(data,bytes):raise ProtocolError("wire chunks must be immutable bytes")
            self.budget.consume_bytes(len(data));cursor=0;result=[]
            while cursor<len(data):
                count=min(self.needed,len(data)-cursor)
                self.buffer.extend(memoryview(data)[cursor:cursor+count]);cursor+=count
                if self.expected is None and len(self.buffer)==4:
                    if not re.fullmatch(rb"[0-9A-Fa-f]{4}",self.buffer):raise ProtocolError("invalid packet length header")
                    size=int(self.buffer,16)
                    if size in (0,1,2):
                        self.budget.consume_packet();result.append((FLUSH,DELIMITER,RESPONSE_END)[size]);self.buffer.clear();continue
                    if size<4:raise ProtocolError("reserved packet length")
                    if size>self.budget.limits.max_packet_size:raise ProtocolLimitExceeded("packet size limit exceeded")
                    self.expected=size
                if self.expected is not None and len(self.buffer)==self.expected:
                    self.budget.consume_packet();result.append(Packet(data=bytes(self.buffer[4:])))
                    self.buffer.clear();self.expected=None
            return tuple(result)
        except Exception:
            self.closed=True;self.buffer.clear();raise

    def finish(self):
        if self.closed:raise ProtocolError("packet decoder is no longer active")
        self.closed=True;self.budget.check()
        if self.buffer:raise ProtocolError("truncated packet line")


async def _await_step(awaitable,budget,cancel):
    task=asyncio.ensure_future(awaitable);watch=asyncio.create_task(cancel.wait()) if cancel is not None else None
    tasks=[task]+([watch] if watch else [])
    try:
        done,_=await asyncio.wait(tasks,timeout=budget.remaining,return_when=asyncio.FIRST_COMPLETED)
        budget.check()
        if task not in done:raise ProtocolTimeout("protocol deadline exceeded")
        return task.result()
    finally:
        for pending in tasks:
            if not pending.done():pending.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def read_packets(reader,*,limits=None,cancel=None):
    budget=ProtocolBudget(limits,cancelled=cancel.is_set if cancel is not None else None)
    decoder=PacketDecoder(budget=budget)
    while True:
        budget.check();needed=decoder.needed
        data=await _await_step(reader.read(needed),budget,cancel)
        if not isinstance(data,bytes) or len(data)>needed:raise ProtocolError("reader violated the bounded byte-stream contract")
        if not data:decoder.finish();return
        for packet in decoder.feed(data):yield packet


async def write_packets(writer,packets,*,limits=None,cancel=None):
    budget=ProtocolBudget(limits,cancelled=cancel.is_set if cancel is not None else None)
    for packet in packets:
        budget.check();wire=encode_packet(packet,limits=budget.limits)
        budget.consume_packet();budget.consume_bytes(len(wire));writer.write(wire)
        await _await_step(writer.drain(),budget,cancel)


@dataclass(frozen=True, slots=True)
class Sideband:
    channel: int
    data: bytes = field(repr=False)

    def raise_if_error(self):
        if self.channel==3:raise RemoteProtocolError("remote protocol error")


def decode_sideband(packet,*,limits=None):
    limits=limits or ProtocolLimits()
    if not isinstance(packet,Packet) or packet.kind is not PacketKind.DATA or not packet.data or packet.data[0] not in (1,2,3):raise ProtocolError("invalid sideband packet")
    channel,data=packet.data[0],packet.data[1:]
    if channel==3 and len(data)>limits.max_error_bytes:raise ProtocolLimitExceeded("remote error exceeds its byte limit")
    return Sideband(channel,data)


def encode_sideband(channel,data,*,limits=None):
    if type(channel) is not int or channel not in (1,2,3) or not isinstance(data,bytes):raise ProtocolError("invalid sideband payload")
    packet=Packet(data=bytes((channel,))+data)
    decode_sideband(packet,limits=limits)
    return encode_packet(packet,limits=limits)
