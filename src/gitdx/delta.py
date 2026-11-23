"""Bounded Git delta programs and iterative, hash-verified dependency chains."""

from .pack import InvalidPack as InvalidObject
from .object_store import ObjectLimitExceeded
from .objects import RawObject


class DeltaBudget:
    def __init__(self,limits):self.limits,self.bytes,self.operations=limits,0,0

    @property
    def remaining(self):return self.limits.max_delta_bytes-self.bytes

    def consume(self,size):
        if size>self.remaining:raise ObjectLimitExceeded("delta reconstruction exceeds its aggregate byte budget")
        self.bytes+=size


def _integer(data,cursor):
    value=0
    for n in range(10):
        if cursor>=len(data):raise InvalidObject("truncated delta size")
        byte=data[cursor];cursor+=1;value|=(byte&127)<<(7*n)
        if value>0xffffffffffffffff:raise InvalidObject("delta size exceeds 64 bits")
        if not byte&128:return value,cursor
    raise InvalidObject("excessive delta size encoding")


def _apply(base,instructions,budget):
    source,cursor=_integer(instructions,0);target,cursor=_integer(instructions,cursor)
    if source!=len(base):raise InvalidObject("delta source size does not match its base")
    if target>budget.limits.max_object_bytes:raise ObjectLimitExceeded("delta target exceeds the object byte limit")
    budget.consume(target)
    output=bytearray()
    while cursor<len(instructions):
        budget.operations+=1
        if budget.operations>budget.limits.max_delta_operations:raise ObjectLimitExceeded("delta operation budget exceeded")
        opcode=instructions[cursor];cursor+=1
        if opcode&128:
            offset=count=0
            for bit in range(7):
                if opcode&(1<<bit):
                    if cursor>=len(instructions):raise InvalidObject("truncated delta copy")
                    value=instructions[cursor];cursor+=1
                    if bit<4:offset|=value<<(8*bit)
                    else:count|=value<<(8*(bit-4))
            count=count or 0x10000
            if offset+count>len(base) or len(output)+count>target:raise InvalidObject("delta copy range exceeds source or target")
            output.extend(memoryview(base)[offset:offset+count])
        elif opcode:
            if cursor+opcode>len(instructions) or len(output)+opcode>target:raise InvalidObject("delta insert exceeds program or target")
            output.extend(instructions[cursor:cursor+opcode]);cursor+=opcode
        else:raise InvalidObject("reserved zero delta opcode")
    if len(output)!=target:raise InvalidObject("delta result size does not match its target")
    return bytes(output)


def apply_delta(base:bytes,instructions:bytes,*,limits=None)->bytes:
    from .pack import PackLimits
    if not isinstance(base,bytes) or not isinstance(instructions,bytes):raise TypeError("delta inputs must be immutable bytes")
    budget=DeltaBudget(limits or PackLimits());budget.consume(len(base)+len(instructions))
    return _apply(base,instructions,budget)


def resolve_delta_chain(oid,locate,limits):
    budget=DeltaBudget(limits);frames=[];seen=set();current=oid
    while True:
        if current in seen:raise InvalidObject("cyclic REF_DELTA dependency")
        seen.add(current)
        located=locate(current,budget)
        if isinstance(located,RawObject):
            budget.consume(len(located.data));resolved=located
        else:
            pack,offset=located;entry=pack._entries[offset]
            budget.consume(entry.size)
            payload=pack._inflate(entry)
            if entry.kind in (6,7):
                if len(frames)>=limits.max_delta_depth:raise ObjectLimitExceeded("delta chain exceeds its depth limit")
                frames.append((current,payload))
                current=pack._offset_ids[entry.base] if isinstance(entry.base,int) else entry.base
                continue
            resolved=RawObject({1:"commit",2:"tree",3:"blob",4:"tag"}[entry.kind],payload)
        if resolved.oid(oid.format)!=current:raise InvalidObject("delta base hash does not match its index ID")
        break
    while frames:
        expected,instructions=frames.pop()
        resolved=RawObject(resolved.kind,_apply(resolved.data,instructions,budget))
        if resolved.oid(oid.format)!=expected:raise InvalidObject("reconstructed delta hash does not match its index ID")
    return resolved
