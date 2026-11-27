"""Native-compatible local reftable transactions with one stack publication."""

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from itertools import islice
import json
import os
import struct
import sys
import uuid
import zlib

from .errors import GitdxError, InvalidObject, UnsupportedFormat
from .filesystem import check_path, read_file, replace_file
from .object_store import LooseObjectStore, ObjectLimitExceeded
from .objects import ObjectFormat, ObjectType, parse_object
from .refs import DirectRef, RefConflict, RefLocked, SymbolicRef
from .reftable import InvalidReftable, Reftable, _name
from .ref_transaction import RefTransactionState


STATE = 'reftable-write-state.json'


@dataclass(frozen=True, slots=True)
class ReftableChange:
    name: str
    new: DirectRef | SymbolicRef | None
    expected: DirectRef | SymbolicRef | None

    def __post_init__(self):
        _name(self.name.encode('utf-8'))
        for value in (self.new, self.expected):
            if value is not None and not isinstance(value, (DirectRef, SymbolicRef)):
                raise TypeError('reftable changes use DirectRef, SymbolicRef or None')


@dataclass(frozen=True, slots=True)
class ReftableWriteResult:
    transaction_id: str
    state: RefTransactionState
    journal_pending: bool = False
    retired_tables: tuple[str, ...] = ()
    atomic: bool = True

    @property
    def committed(self): return self.state is RefTransactionState.COMMITTED


class ReftableWriteInterrupted(GitdxError):
    def __init__(self, result):
        super().__init__('reftable publication interrupted; inspect the receipt and recover explicitly')
        self.result = result


def _varint(value):
    result = bytearray((value & 127,))
    while value >> 7:
        value = (value >> 7)-1
        result.insert(0, 128 | (value & 127))
    return bytes(result)


def _block(kind, records, skip, limits):
    payload = bytearray();restarts = []
    for key, value_type, value in records:
        if len(key) > limits.max_key_bytes: raise ObjectLimitExceeded('reftable writer key limit exceeded')
        restarts.append(skip+4+len(payload))
        payload.extend(b'\0'+_varint(len(key) << 3 | value_type)+key+value)
        if len(payload)+skip+6+3*len(restarts) >= min(limits.max_block_bytes, 0x1000000):
            raise ObjectLimitExceeded('reftable writer single-block budget exceeded')
    if not restarts: return b''
    if len(restarts) > 65535: raise ObjectLimitExceeded('reftable writer restart count exceeded')
    payload.extend(b''.join(p.to_bytes(3, 'big') for p in restarts)+len(restarts).to_bytes(2, 'big'))
    length = skip+4+len(payload)
    return kind+length.to_bytes(3, 'big')+(zlib.compress(payload) if kind == b'g' else payload)


def encode_reftable(refs, format, minimum, maximum, *, logs=(), updates=None, limits):
    """A deliberately bounded single-ref-block/single-log-block native table."""
    version = 1 if format is ObjectFormat.SHA1 else 2
    header = b'REFT'+bytes((version,))+bytes(3)+struct.pack('>QQ', minimum, maximum)
    if version == 2: header += b's256'
    records = []
    for name, value in sorted(refs, key=lambda p: p[0].encode('utf-8')):
        key = name.encode('utf-8');_name(key)
        kind = 0 if value is None else 3 if isinstance(value, SymbolicRef) else 1
        encoded = _varint((updates or {}).get(name, minimum)-minimum)
        if kind == 1: encoded += value.oid.bytes
        elif kind == 3:
            target = value.target.encode('utf-8');encoded += _varint(len(target))+target
        records.append((key, kind, encoded))
    if len(records)+len(logs) > limits.max_records:
        raise ObjectLimitExceeded('reftable writer record budget exceeded')
    refs_block = _block(b'r', records, len(header), limits)
    log_position = len(header)+len(refs_block) if refs_block and logs else 0
    log_block = _block(b'g', sorted(logs), 0 if refs_block else len(header), limits)
    footer = header+struct.pack('>5Q', 0, 0, 0, log_position, 0)
    footer += zlib.crc32(footer).to_bytes(4, 'big')
    data = header+refs_block+log_block+footer
    # Use the full reader as a final structural/budget check before staging.
    Reftable.from_bytes(data, format, limits=limits)
    return data


def _canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii')


class ReftableWriter:
    """Explicit trusted local writer; never an untrusted hosted intake profile."""

    def __init__(self, store):
        if sys.platform not in ('linux', 'darwin', 'win32'):
            raise UnsupportedFormat('local reftable writes require a qualified platform')
        self.store = store
        self.path = store.path
        self.root = self.path/'reftable'
        info = self.path.stat();self.identity = (info.st_dev, info.st_ino)
        self._guard(recovering=True)

    def _guard(self, *, recovering=False, check=lambda: None):
        check_path(self.path, self.path)
        info = self.path.stat()
        if (info.st_dev, info.st_ino) != self.identity: raise UnsupportedFormat('reftable storage identity changed')
        for name in ('ref-transaction-state', 'checkout-state', 'operation-state', 'merge-state'):
            path = self.path/name;check_path(self.path, path)
            if path.exists(): raise UnsupportedFormat('recover the pending repository operation first')
        pending = self.path/STATE;check_path(self.path, pending)
        if not recovering and pending.exists(): raise UnsupportedFormat('reftable writer recovery is pending')
        self.store._values(check)

    @contextmanager
    def _lock(self, *, recovering=False, check=lambda: None):
        self._guard(recovering=recovering, check=check)
        lock = self.root/'tables.list.lock';check_path(self.path, lock)
        try: fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error: raise RefLocked('reftable stack lock has another owner') from error
        os.close(fd)
        try:
            self._guard(recovering=recovering, check=check)
            yield
        finally: lock.unlink(missing_ok=True)

    def write_object(self, raw):
        self._guard()
        return LooseObjectStore(self.store.objects.path, self.store.objects.format,
                                limits=self.store.objects.limits).write(raw)

    def _stack(self, check):
        listing = read_file(self.path, self.root/'tables.list', 64*1024)
        tables = [];total = 0
        for name in listing.splitlines():
            check()
            data = read_file(self.path, self.root/name.decode('ascii'), self.store.limits.max_bytes-total)
            total += len(data)
            tables.append(Reftable.from_bytes(data, self.store.objects.format, limits=self.store.limits, check=check))
        return listing, tables

    def _target(self, name, value):
        if not isinstance(value, DirectRef): return
        if value.oid.format != self.store.objects.format: raise InvalidObject('reftable target format differs')
        raw = self.store.objects.read(value.oid);parse_object(raw, value.oid.format)
        if (name == 'HEAD' or name.startswith('refs/heads/')) and raw.kind is not ObjectType.COMMIT:
            raise InvalidObject('branch and detached HEAD targets must be commits')

    def transaction(self, changes, *, before_publish=None, check=None):
        check = check or (lambda: None)
        changes = tuple(islice(changes, 257))
        if not changes or len(changes) > 256 or any(not isinstance(c, ReftableChange) for c in changes):
            raise ValueError('select 1–256 ReftableChange records')
        if len({c.name for c in changes}) != len(changes): raise ValueError('duplicate exact reftable changes')
        with self._lock(check=check):
            before, tables = self._stack(check);values = self.store._values(check)
            for change in changes:
                check()
                if values.get(change.name) != change.expected: raise RefConflict('reftable expected value changed')
                self._target(change.name, change.new)
                if change.new is None: values.pop(change.name, None)
                else: values[change.name] = change.new
            # Validate symbolic cycles against the proposed complete snapshot.
            for name in values:
                chain = set();current = name
                for _ in range(16):
                    if current in chain: raise InvalidObject('proposed symbolic ref cycle')
                    chain.add(current);value = values.get(current)
                    if not isinstance(value, SymbolicRef): break
                    current = value.target
                else: raise InvalidObject('proposed symbolic chain exceeds supported depth')
            update = max((t.max_update for t in tables), default=0)+1
            if update > 0xffffffffffffffff: raise ObjectLimitExceeded('reftable update index exhausted')
            data = encode_reftable(((c.name, c.new) for c in changes), self.store.objects.format,
                                  update, update, limits=self.store.limits)
            return self._publish(before, data, update, update, compact=False, before_publish=before_publish, check=check)

    def compact(self, *, check=None):
        check = check or (lambda: None)
        with self._lock(check=check):
            before, tables = self._stack(check)
            values = {};logs = {};updates = {}
            for table in tables:
                check()
                for name, value in table.refs:
                    if value is None: values.pop(name, None)
                    else: values[name] = value
                updates.update(table.ref_updates)
                for key, kind, payload in table.logs:
                    if kind: logs[key] = (key, kind, payload)
                    else: logs.pop(key, None)
            minimum = min((t.min_update for t in tables), default=1)
            maximum = max((t.max_update for t in tables), default=1)
            data = encode_reftable(values.items(), self.store.objects.format, minimum, maximum,
                                  logs=tuple(logs.values()), updates=updates, limits=self.store.limits)
            return self._publish(before, data, minimum, maximum, compact=True, check=check)

    def _save(self, state):
        data = {'state': state, 'sha256': hashlib.sha256(_canonical(state)).hexdigest()}
        replace_file(self.path, self.path/STATE, _canonical(data))

    def _publish(self, before, data, minimum, maximum, *, compact, check, before_publish=None):
        identity = uuid.uuid4().hex
        name = f'0x{minimum:012x}-0x{maximum:012x}-{identity[:8]}.ref'
        after = (b'' if compact else before)+name.encode('ascii')+b'\n'
        if len(after) > 64*1024 or len(after.splitlines()) > self.store.limits.max_tables:
            raise ObjectLimitExceeded('compact the reftable stack before appending')
        if not compact:
            _, old_tables = self._stack(check)
            old_bytes = sum((self.root/n.decode('ascii')).stat().st_size for n in before.splitlines())
            new_table = Reftable.from_bytes(data, self.store.objects.format, limits=self.store.limits)
            if old_bytes+len(data) > self.store.limits.max_bytes or sum(len(t.refs) for t in old_tables)+len(new_table.refs) > self.store.limits.max_records:
                raise ObjectLimitExceeded('compact the reftable stack before exceeding its budgets')
        check()
        if before_publish: before_publish()
        self._guard(check=check)
        if read_file(self.path, self.root/'tables.list', 64*1024) != before: raise RefConflict('reftable stack changed under its lock')
        target = self.root/name;check_path(self.path, target)
        with target.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        state = {'version': 1, 'identity': list(self.identity), 'id': identity, 'table': name,
                 'table_sha256': hashlib.sha256(data).hexdigest(), 'before': base64.b64encode(before).decode('ascii'),
                 'after': base64.b64encode(after).decode('ascii'), 'compact': compact}
        prepared = False
        try:
            prepared = True;self._save(state)
            check()
            if before_publish: before_publish()
            self._guard(recovering=True, check=check)
            if read_file(self.path, self.root/'tables.list', 64*1024) != before: raise RefConflict('reftable stack changed before publication')
            replace_file(self.path, self.root/'tables.list', after)
            check()
            (self.path/STATE).unlink()
            return ReftableWriteResult(identity, RefTransactionState.COMMITTED, retired_tables=tuple(n.decode('ascii') for n in before.splitlines()) if compact else ())
        except BaseException as error:
            if not prepared: raise
            try:
                current = read_file(self.path, self.root/'tables.list', 64*1024)
                phase = RefTransactionState.COMMITTED if current == after else RefTransactionState.PREPARED if current == before else RefTransactionState.UNKNOWN
            except BaseException: phase = RefTransactionState.UNKNOWN
            raise ReftableWriteInterrupted(ReftableWriteResult(identity, phase, (self.path/STATE).exists())) from error

    def recover(self, *, check=None):
        check = check or (lambda: None)
        with self._lock(recovering=True, check=check):
            try: raw = read_file(self.path, self.path/STATE, 256*1024)
            except FileNotFoundError: return None
            envelope = json.loads(raw);state = envelope['state']
            if state.get('version') != 1 or state.get('identity') != list(self.identity) or envelope.get('sha256') != hashlib.sha256(_canonical(state)).hexdigest():
                raise InvalidReftable('reftable journal version, checksum or identity differs')
            before = base64.b64decode(state['before'], validate=True)
            after = base64.b64decode(state['after'], validate=True)
            if len(before) > 64*1024 or len(after) > 64*1024: raise InvalidReftable('journal stack byte limit exceeded')
            name = state['table']
            import re
            if not re.fullmatch(r'0x[0-9a-f]{12,16}-0x[0-9a-f]{12,16}-[0-9a-f]{8}\.ref', name):
                raise InvalidReftable('invalid owned reftable journal filename')
            if after != (b'' if state['compact'] else before)+name.encode('ascii')+b'\n':
                raise InvalidReftable('journal stack does not match its owned table')
            current = read_file(self.path, self.root/'tables.list', 64*1024)
            if current == after: phase = RefTransactionState.COMMITTED
            elif current == before: phase = RefTransactionState.ROLLED_BACK
            else: raise RefConflict('later native stack edits require owner reconciliation')
            data = read_file(self.path, self.root/name, self.store.limits.max_bytes)
            if hashlib.sha256(data).hexdigest() != state['table_sha256']: raise InvalidReftable('owned table changed during recovery')
            Reftable.from_bytes(data, self.store.objects.format, limits=self.store.limits, check=check)
            check()
            # Unreferenced tables are retained, including interrupted prepared
            # tables. Never remove a file that a native reader may have opened.
            (self.path/STATE).unlink()
            return ReftableWriteResult(state['id'], phase,
                retired_tables=tuple(n.decode('ascii') for n in before.splitlines()) if state['compact'] and phase is RefTransactionState.COMMITTED else ())
