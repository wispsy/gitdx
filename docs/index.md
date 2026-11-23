# Index v2 transactions

`Repository.index` exposes a non-bare repository's `IndexFile`. `read()` returns
an immutable `Index` with raw byte paths, full format-aware OIDs, modes, stat
metadata, assume-valid flags and stages 0–3. Missing indexes read as empty.
Serialization uses Git index version 2 and SHA-1/SHA-256 checksums. Parsing
verifies ordering, duplicate/mixed stages, path lengths, NUL padding and budgets.

```python
from gitdx import Blob, IndexEntry, Repository

repo = Repository.open("example")
oid = repo.objects.write(Blob(b"hello\n").to_raw())
with repo.index.transaction() as transaction:
    transaction.replace([IndexEntry(b"hello.txt", oid)])
    transaction.commit()
```

Transactions acquire `index.lock` exclusively, then read/validate the current
snapshot. Explicit commit flushes and replaces the index; exit without commit
aborts. Existing locks and unsupported indexes are preserved. Concurrent native
Git writers share the same lock convention. A successful transaction never
removes a successor's lock. Caller-owned stale snapshots must not be used as an
unconditional merge: derive edits from the snapshot inside the locked context.
An attempted publication consumes the transaction even on I/O failure; leave
that context and create a fresh transaction to retry. Catching a write/flush
error cannot make the same stream publish a second snapshot.

Optional uppercase-signature extensions are preserved byte-for-byte for an
unchanged snapshot. Any entry change drops all optional extensions, including
cache-tree, resolve-undo and unknown caches, because their semantics cannot be
updated safely yet. This intentionally loses resolve-undo history. Required
unknown extensions, split/sparse indexes, extended flags and versions 3/4 are
refused. This codec does not stage filesystem files or prove referenced objects
exist; DX-06 owns staging/status and working-tree path safety.

Defaults: 64 MiB file, 250,000 entries, 4096-byte paths and 8 MiB aggregate
extensions. As with object/ref storage, directories must be owner-controlled;
power-loss durability and hostile concurrent directory replacement remain
separate hosting qualification work.
