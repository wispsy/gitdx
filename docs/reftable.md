# Reftable storage

Current source selects `ReftableRefStore` when format-version-1 configuration
sets `extensions.refStorage=reftable`. `Repository.open()` recognizes the dummy
compatibility HEAD and reads the real HEAD from the selected table stack. It
never interprets the dummy files as a files-ref backend. The published 0.1.0a1
preview predates this reader.

`Repository.refs.read()`, `resolve()` and `snapshot()` support exact-case names,
symbolic and detached/unborn HEAD, newer-table overrides and deletion tombstones.
Branch/tag listings, history, typed revision resolution and object/pack reads
work through the same repository interfaces. `snapshot()` is also part of the
files-ref interface; callers do not need to equate reftable with packed refs.
Mixed-case refs, UTF-8 names and platform device names remain data in a table;
the reader never creates a corresponding filesystem path. Non-UTF-8 ref names
are explicitly outside this string API.

The default interface is read-only. Ref updates, transactions, reflog repair, repository
object writes, staging/index and checkout/worktree operations refuse. `packed()`
is a files-specific operation and refuses on reftable. An explicit trusted-local
writer is described below; read support does not implicitly enable mutations.

## Format validation and bounds

The reader accepts v1/SHA-1 and v2 `sha1`/`s256` tables matching the repository
format. It validates repeated headers, footer CRC, update ranges, section/block
boundaries, padding, sorted unique keys, prefix/restart offsets, reference value
types and object widths. Ref/object/log index positions must identify valid
blocks with matching last keys and complete section coverage, including multiple
top-level index blocks produced by native Git. Optional object mappings must
point at ref blocks. Reflog blocks are bounded, inflated and structurally checked
without exposing a reflog query API. The format has no per-ref-block checksum;
the footer CRC does not authenticate object/ref data.

`ReftableLimits` defaults to 64 MiB total stack input, 64 tables, 200,000 records
per table and total ref records, 16 MiB per inflated block, 4096-byte keys and
64 MiB inflated logs per table. Cancellation callbacks run during table opening,
block/record parsing and index traversal. Stack filenames cannot select parent
paths. Missing tables trigger at most three fresh listing attempts, as concurrent
native compaction may retire a previously selected file. Each successful query
uses a single selected immutable stack; separate queries may observe new stacks.

Storage is owner-controlled, following the local repository filesystem contract.
This read-only codec is not an untrusted receive profile. Resource limits are
finite reader budgets, not a guarantee of constant-time lookup on arbitrary
tables. Aligned/unaligned and empty tables, multi-block indexes, compressed logs,
stack tombstones and native-created mixed-case refs are qualified in both hashes.

The format follows the [Git reftable specification](https://git-scm.com/docs/reftable).
Installed wheel/sdist qualification records `reftable-native`,
`reftable-corruption` and `reftable-no-git`; the separate empty-PATH worker reads
native migrated storage without executing Git.
Native reftable clients also clone distinct mixed-case tips through upload-pack
in both hashes, exercising the combined read-serving interface.

## Explicit local transactions and compaction

`writer = repository.refs.writer()` selects the local Linux/macOS/Windows writer.
`writer.transaction(changes)` takes 1–256 `ReftableChange(name, new, expected)`
records. Values are `DirectRef`, `SymbolicRef` or `None`, compared by exact name
and complete value. HEAD can be symbolic or detached; branches target commits.
The proposed snapshot refuses symbolic cycles, stale values and unknown locks.
`writer.write_object(raw)` explicitly writes immutable objects for this profile.
Ordinary repository object/ref/worktree/index mutation still refuses reftable.

The native `reftable/tables.list.lock` serializes writers. A new immutable table
is synchronized before one complete stack-list replacement publishes the batch.
One selected stack snapshot therefore sees all old or all new values, including
mixed-case refs on case-insensitive filesystems. Separate read calls may select
different stacks. This does not enable atomic files refs or advertise atomic
receive-pack; hosted/untrusted reftable writes remain outside this local API.
Optional `before_publish` guards run under the stack lock before preparation and
again before publication. Cooperative cancellation is checked throughout.

`writer.compact()` rewrites the complete selected stack into one bounded table.
It preserves exact-case names, symbolic/unborn/detached HEAD, per-ref update
indexes, and native reflog key/value bytes, applying tombstones in stack order.
It appends no new reflogs. Old tables remain on disk for existing readers and
are returned as `retired_tables`; automatic deletion/retention is not selected.
The writer supports one unaligned ref block and one compressed log block, each
bounded by the reader limits and 65,535 restart records. Larger compactions
refuse before publication rather than silently dropping data. New appends also
respect the total stack byte/record/table budgets; compact before exhaustion.

The checksummed `reftable-write-state.json` records exact before/after lists,
repository identity and the owned new table hash. `ReftableWriteInterrupted`
exposes prepared/committed/unknown state and pending recovery. After an error,
`repository.refs.writer().recover()` reacquires the native stack lock and verifies
the journal/table. An unchanged before list establishes no publication; the
exact after list establishes committed publication. Later native stack edits,
changed tables, corrupt journals and foreign identities require owner
reconciliation; recovery never overwrites them. A real process exit can leave
the native lockfile, which must be reconciled only after proving its owner has
exited. Unreferenced prepared tables remain available for owner inspection.

This is an owner-controlled process-interruption contract, not a hostile
filesystem sandbox or cross-file power-loss guarantee. Capability qualification
requires native read/write/fsck, native and Python snapshot visibility, log/case
preservation, contention, before/after interruption and an empty-PATH worker in
each source/wheel/sdist platform profile.
