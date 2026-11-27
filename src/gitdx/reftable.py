"""Bounded immutable reftable v1/v2 snapshots; no writer or reflog API."""

from dataclasses import dataclass
from pathlib import Path
import re
import struct
import zlib

from .config import parse_config
from .errors import GitdxError, UnsupportedFormat
from .filesystem import read_file
from .names import InvalidRef, validate_refname
from .object_store import ObjectLimitExceeded
from .objects import ObjectFormat, ObjectId
from .refs import DirectRef, SymbolicRef, ResolvedRef
from .repository_profile import check_repository_features


class InvalidReftable(GitdxError, ValueError):
    """An immutable table or its selected stack is malformed."""


@dataclass(frozen=True, slots=True)
class ReftableLimits:
    max_bytes: int = 64*1024*1024
    max_tables: int = 64
    max_records: int = 200_000
    max_block_bytes: int = 16*1024*1024
    max_key_bytes: int = 4096
    max_log_bytes: int = 64*1024*1024

    def __post_init__(self):
        if any(type(v) is not int or v<=0 for v in (self.max_bytes,self.max_tables,self.max_records,
                self.max_block_bytes,self.max_key_bytes,self.max_log_bytes)):
            raise ValueError('reftable limits must be positive integers')


def _name(data):
    try:name=data.decode('utf-8')
    except UnicodeError as error:raise UnsupportedFormat('reftable names require UTF-8 in this API') from error
    if name!='HEAD':validate_refname(name,portable=False)
    return name


class _Cursor:
    def __init__(self,data,start=0,end=None):self.data=data;self.pos=start;self.end=len(data) if end is None else end

    def take(self,size):
        if size<0 or self.pos+size>self.end:raise InvalidReftable('truncated reftable record')
        value=self.data[self.pos:self.pos+size];self.pos+=size
        return value

    def varint(self):
        value=0
        for n in range(10):
            byte=self.take(1)[0];value=(value<<7)|(byte&127)
            if value>0xffffffffffffffff:break
            if not byte&128:return value
            value+=1
        raise InvalidReftable('excessive reftable varint')


@dataclass(frozen=True, slots=True)
class Reftable:
    format: ObjectFormat
    min_update: int
    max_update: int
    refs: tuple[tuple[str,object],...]
    # Encoded log values retain native identity/message/timestamp bytes exactly.
    logs: tuple[tuple[bytes,int,bytes],...] = ()
    ref_updates: tuple[tuple[str,int],...] = ()

    @classmethod
    def from_bytes(cls,data,format=ObjectFormat.SHA1,*,limits=None,check=None):
        if not isinstance(data,bytes):raise TypeError('reftable data must be immutable bytes')
        limits=limits or ReftableLimits();format=ObjectFormat(format);check=check or (lambda:None)
        if len(data)>limits.max_bytes:raise ObjectLimitExceeded('reftable byte budget exceeded')
        if len(data)<92 or data[:4]!=b'REFT':raise InvalidReftable('invalid reftable header')
        version=data[4]
        if version not in (1,2):raise UnsupportedFormat('unsupported reftable version')
        header=24 if version==1 else 28;footer=header+44;end=len(data)-footer
        if end<header or data[end:end+header]!=data[:header]:raise InvalidReftable('reftable header/footer mismatch')
        if zlib.crc32(data[end:-4])!=int.from_bytes(data[-4:],'big'):raise InvalidReftable('reftable footer checksum mismatch')
        selected=b'sha1' if version==1 else data[24:28]
        if selected not in (b'sha1',b's256'):raise UnsupportedFormat('unsupported reftable hash ID')
        if selected!={ObjectFormat.SHA1:b'sha1',ObjectFormat.SHA256:b's256'}[format]:
            raise InvalidReftable('reftable hash differs from repository')
        block_size=int.from_bytes(data[5:8],'big');minimum,maximum=struct.unpack_from('>QQ',data,8)
        if minimum>maximum:raise InvalidReftable('reftable update bounds are reversed')
        ref_index,obj_field,obj_index,log_pos,log_index=struct.unpack_from('>5Q',data,end+header)
        obj_pos=obj_field>>5;obj_width=obj_field&31
        positions=(ref_index,obj_pos,obj_index,log_pos,log_index)
        if any(p and not header<=p<end for p in positions):raise InvalidReftable('reftable footer offset is out of range')
        if bool(obj_pos)!=bool(obj_width) or obj_width and not 2<=obj_width<=min(31,format.digest_size):
            raise InvalidReftable('invalid reftable object abbreviation width')
        blocks={};groups={'r':[],'o':[],'g':[]};indexes={'r':[],'o':[],'g':[]}
        refs=[];logs=[];ref_updates=[];cursor=header;group=None;phase=-1;records=0;log_bytes=0
        while cursor<end:
            check();position=0 if cursor==header else cursor;skip=header if position==0 else 0
            if cursor+4>end:raise InvalidReftable('truncated reftable block header')
            kind=chr(data[cursor]);length=int.from_bytes(data[cursor+1:cursor+4],'big')
            if kind not in ('r','i','o','g'):raise InvalidReftable('unknown reftable block type')
            if not skip+6<length<=limits.max_block_bytes:raise ObjectLimitExceeded('invalid/excessive reftable block size')
            if kind!='i':group=kind
            if group is None:raise InvalidReftable('index without a data section')
            rank={'r':0,'o':2,'g':4}[group]+(kind=='i')
            if rank<phase:raise InvalidReftable('reftable sections are out of order')
            phase=rank
            if kind=='g':
                log_bytes+=length
                if log_bytes>limits.max_log_bytes:raise ObjectLimitExceeded('reftable log inflation budget exceeded')
                decoder=zlib.decompressobj();compressed=memoryview(data)[cursor+4:end]
                try:inflated=decoder.decompress(compressed,length-skip-4+1)
                except zlib.error as error:raise InvalidReftable('invalid compressed log block') from error
                if not decoder.eof or len(inflated)!=length-skip-4:raise InvalidReftable('reftable log size mismatch')
                next_position=cursor+4+len(compressed)-len(decoder.unused_data)
                block=data[position:cursor+4]+inflated
            else:
                if position+length>end:raise InvalidReftable('reftable block crosses footer')
                if kind in ('r','o') and block_size and length>block_size:raise InvalidReftable('data block exceeds alignment size')
                block=data[position:position+length];next_position=position+length
            count=int.from_bytes(block[-2:],'big');restart_start=length-2-count*3
            if not count or restart_start<skip+4:raise InvalidReftable('invalid reftable restart table')
            restarts=[int.from_bytes(block[p:p+3],'big') for p in range(restart_start,length-2,3)]
            if restarts!=sorted(set(restarts)) or restarts[0]!=skip+4 or restarts[-1]>=restart_start:
                raise InvalidReftable('reftable restart offsets are invalid')
            source=_Cursor(block,skip+4,restart_start);prior=b'';keys=[];values=[];restart_records=set()
            while source.pos<source.end:
                check();records+=1
                if records>limits.max_records:raise ObjectLimitExceeded('reftable record budget exceeded')
                record_position=source.pos;prefix=source.varint();suffix=source.varint();value_type=suffix&7;suffix>>=3
                if prefix>len(prior) or prefix+suffix>limits.max_key_bytes:raise InvalidReftable('invalid/excessive reftable key prefix')
                key=prior[:prefix]+source.take(suffix)
                if not key or keys and key<=prior:raise InvalidReftable('reftable keys are not strictly ordered')
                if prefix==0:restart_records.add(record_position)
                if kind=='r':
                    name=_name(key);update=minimum+source.varint()
                    if update>maximum:raise InvalidReftable('ref update index outside header bounds')
                    if value_type==0:value=None
                    elif value_type in (1,2):
                        value=DirectRef(ObjectId(source.take(format.digest_size).hex(),format))
                        if value_type==2:
                            peeled=source.take(format.digest_size)
                            if not any(peeled):raise InvalidReftable('null peeled reftable target')
                    elif value_type==3:
                        size=source.varint()
                        if size>limits.max_key_bytes:raise ObjectLimitExceeded('symbolic target exceeds reftable key limit')
                        target=_name(source.take(size))
                        if target=='HEAD':raise InvalidReftable('symbolic target must be under refs/')
                        value=SymbolicRef(target)
                    else:raise InvalidReftable('reserved reftable reference type')
                    refs.append((name,value))
                    ref_updates.append((name,update))
                elif kind=='i':
                    if value_type:raise InvalidReftable('reserved index record type')
                    value=source.varint()
                elif kind=='o':
                    if len(key)!=obj_width:raise InvalidReftable('object abbreviation length differs from footer')
                    amount=value_type or source.varint()
                    if amount>limits.max_records:raise ObjectLimitExceeded('reftable object positions exceeded')
                    value=[];last=0
                    for i in range(amount):
                        delta=source.varint();last+=delta
                        if i and not delta:raise InvalidReftable('object block positions repeat')
                        value.append(last)
                else:
                    log_start=source.pos
                    if len(key)<10 or key[-9]!=0:raise InvalidReftable('invalid reftable log key')
                    _name(key[:-9]);update=0xffffffffffffffff-int.from_bytes(key[-8:],'big')
                    if update>maximum:raise InvalidReftable('log update index exceeds header maximum')
                    if value_type not in (0,1):raise InvalidReftable('reserved log record type')
                    if value_type:
                        source.take(2*format.digest_size)
                        for _ in range(2):source.take(source.varint())
                        source.varint();source.take(2);source.take(source.varint())
                    logs.append((key,value_type,block[log_start:source.pos]))
                    value=None
                keys.append(key);values.append(value);prior=key
            if not set(restarts)<=restart_records:raise InvalidReftable('restart points do not identify complete keys')
            if not keys:raise InvalidReftable('empty reftable block')
            if kind=='i':indexes[group].append(position)
            else:
                if groups[group] and blocks[groups[group][-1]][2][-1]>=keys[0]:raise InvalidReftable('data blocks are not strictly ordered')
                groups[group].append(position)
            blocks[position]=(kind,group,keys,values)
            cursor=next_position
            if cursor<end and data[cursor]==0:
                aligned=position+block_size
                if kind=='g' or not block_size or not cursor<aligned<=end or any(data[cursor:aligned]):
                    raise InvalidReftable('invalid reftable block padding')
                cursor=aligned
        if cursor!=end:raise InvalidReftable('reftable block layout differs from footer')
        if obj_pos!=(groups['o'][0] if groups['o'] else 0) or log_pos!=(groups['g'][0] if groups['g'] else 0):
            raise InvalidReftable('footer section offset differs from block layout')
        for group,root in (('r',ref_index),('o',obj_index),('g',log_index)):
            if not root:
                if indexes[group] or (group=='r' and not block_size and len(groups[group])>1):
                    raise InvalidReftable('missing reftable index root')
                continue
            if root not in blocks or blocks[root][0]!='i':raise InvalidReftable('index root must identify an index block')
            # Native writers can leave several blocks in the highest index
            # level. The footer points at that level's first block.
            pending=[p for p in indexes[group] if p>=root];seen=set();leaves=set();previous=b''
            for p in pending:
                keys=blocks[p][2]
                if previous and keys[0]<=previous:raise InvalidReftable('root index blocks are out of order')
                previous=keys[-1]
            while pending:
                check();p=pending.pop()
                if p in seen or p not in blocks:raise InvalidReftable('invalid/cyclic reftable index position')
                seen.add(p);kind,selected,keys,values=blocks[p]
                if selected!=group:raise InvalidReftable('index crosses reftable sections')
                if kind!='i':leaves.add(p);continue
                for key,target in zip(keys,values):
                    if target>=p or target not in blocks or key!=blocks[target][2][-1]:raise InvalidReftable('index key/target differs from referenced block')
                    pending.append(target)
            if leaves!=set(groups[group]) or seen-leaves!=set(indexes[group]):raise InvalidReftable('index does not cover its data blocks exactly')
        ref_positions=set(groups['r'])
        for p in groups['o']:
            for positions in blocks[p][3]:
                if any(pos not in ref_positions for pos in positions):raise InvalidReftable('object index points outside reference blocks')
        return cls(format,minimum,maximum,tuple(refs),tuple(logs),tuple(ref_updates))


class ReftableRefStore:
    """Exact-case read-only stack selected by repository extensions."""

    def __init__(self,git_dir,objects,*,limits=None):
        self.path=Path(git_dir).absolute();self.objects=objects;self.limits=limits or ReftableLimits()

    def _values(self,check=None):
        check=check or (lambda:None);check()
        config=parse_config(read_file(self.path,self.path/'config',1024*1024));check_repository_features(self.path,config)
        if config.ref_storage!='reftable' or config.object_format!=self.objects.format:raise UnsupportedFormat('reference storage changed since open')
        root=self.path/'reftable';limit=self.limits
        for _ in range(3):
            check();listing=read_file(self.path,root/'tables.list',64*1024);names=listing.splitlines()
            if listing and not listing.endswith(b'\n'):raise InvalidReftable('unterminated reftable stack listing')
            if len(names)>limit.max_tables:raise ObjectLimitExceeded('reftable stack count exceeded')
            if len(set(names))!=len(names) or any(not re.fullmatch(rb'[A-Za-z0-9_-]{1,200}\.(?:ref|log)',n) for n in names):
                raise InvalidReftable('invalid or repeated reftable filename')
            buffers=[];size=0
            try:
                for name in names:
                    check();data=read_file(self.path,root/name.decode('ascii'),limit.max_bytes-size,limit_error=ObjectLimitExceeded)
                    size+=len(data);buffers.append(data)
            except FileNotFoundError:continue
            values={};previous=-1;records=0
            for data in buffers:
                table=Reftable.from_bytes(data,self.objects.format,limits=limit,check=check)
                if table.min_update<=previous:raise InvalidReftable('reftable stack update ranges overlap or reverse')
                previous=table.max_update;records+=len(table.refs)
                if records>limit.max_records:raise ObjectLimitExceeded('reftable stack record limit exceeded')
                for name,value in table.refs:
                    if value is None:values.pop(name,None)
                    else:values[name]=value
            return values
        raise InvalidReftable('reftable stack changed repeatedly while opening')

    def snapshot(self,prefix='refs/',*,max_refs=10_000,max_depth=64,check=None):
        values=self._values(check);selected=tuple(sorted((n,v) for n,v in values.items() if n.startswith(prefix)))
        if len(selected)>max_refs or any(len(n.split('/'))>max_depth for n,_ in selected):raise ObjectLimitExceeded('ref snapshot limit exceeded')
        return selected

    def read(self,name):
        _name(name.encode('utf-8'));return self._values().get(name)

    def resolve(self,name='HEAD'):
        _name(name.encode('utf-8'));values=self._values();chain=[]
        for _ in range(16):
            if name in chain:raise InvalidRef('symbolic reftable cycle')
            value=values.get(name)
            if not isinstance(value,SymbolicRef):return ResolvedRef(name,value.oid if value else None,tuple(chain))
            chain.append(name);name=value.target
        raise InvalidRef('symbolic reftable chain exceeds supported depth')

    def _readonly(self,*args,**kwargs):raise UnsupportedFormat('reftable mutations require a separately qualified writer')

    def writer(self):
        """Select the explicit local writer; default repository mutation stays refused."""
        from .reftable_writer import ReftableWriter
        return ReftableWriter(self)

    update=delete=set_symbolic=detach_head=repair_reflog=transaction=recover_transaction=packed=_readonly
