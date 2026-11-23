# Indexed pack reading

`Repository.objects` is now a `RepositoryObjectStore`: writes remain loose,
while reads try loose storage first and then paired `objects/pack/pack-*.idx`
and `.pack` files. Corrupt loose objects are reported, never hidden by a packed
copy. Pack/index discovery rejects symlinks and malformed primary filenames.
Bitmaps, reverse indexes and multi-pack indexes are optional accelerators and
are not needed to read independently indexed packs.

`PackIndex.from_bytes(data, format)` accepts versions 1 and 2. It verifies the
index checksum, fanout counts, sorted unique IDs, offset tables and large-offset
references. `PackFile(data, index).read(oid)` accepts pack versions 2 and 3,
checks pairing/trailers/counts, every indexed object's boundaries/header and v2
CRC32, then inflates and hashes the requested base object. SHA-1 and SHA-256 use
their native full OID/checksum widths throughout.

Only undeltified blobs, trees, commits and tags are readable. OFS/REF delta base
headers are bounded and validated structurally, but requesting their object
raises `UnsupportedFormat` naming DX-12. This is not full existing-repository
compatibility or a whole-pack fsck: unrequested compressed payloads and reachable
graphs are not inflated/validated. Thin-pack resolution, alternates, maintenance
races and all network exchange remain later work.

Default `PackLimits` cap a pack at 256 MiB, an index at 64 MiB, 250,000 objects,
64 MiB expanded/compressed per object, 128 packs and 512 MiB aggregate lookup
scan bytes. Inflation is additionally bounded by the declared object size.
`Repository.open(..., pack_limits=...)` and `discover` accept explicit limits;
without an override, existing `StoreLimits` also bound packed object sizes.
The initial implementation reads bounded files into memory and verifies pack
metadata on each lookup. Large-repository performance and caching need separate
qualification rather than silently relaxing budgets.

Native-Git fixtures cover both hashes and index v1/v2, prune-packed fallback and
pack v3 ingestion. Synthetic fixtures cover 64-bit offset-table entries,
corruption, oversized input, framing, deltas and static symlink refusal. Runtime
pack access is also exercised without Git on PATH. All storage assumes
owner-controlled directories; hostile concurrent replacement and power-loss
guarantees remain part of hosting qualification.
