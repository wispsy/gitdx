# Read-only reftable storage

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

This profile is read-only. Ref updates, transactions, reflog repair, repository
object writes, staging/index and checkout/worktree operations refuse. `packed()`
is a files-specific operation and refuses on reftable. A writer and its atomic
publication semantics belong to DX-28; read support does not enable them.

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
