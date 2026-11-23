# Pack writing and selected object closures

`repo.pack(roots, limits=PackWriteLimits(...))` returns a `PackBundle` with pack
bytes, v2 index bytes, native object format, sorted object IDs and pack checksum.
It performs no filesystem publication, subprocess execution or network I/O.
`write_pack(store, roots)` and `reachable_objects(store, roots)` expose the same
ordinary object-store boundary. Callers own transport and paired-file publication.

Every selected commit/tree/tag dependency is read, hash checked and structurally
validated before a complete bundle is returned. Edge types must match: commits
name trees/commit parents, tree directories name trees, file/symlink entries name
blobs, and annotated tags honor their declared target type. Gitlinks refer to
external repository commits and are preserved without traversal. Unrelated and
unreachable objects are excluded. Missing/corrupt/wrong-type dependencies fail
before any successful output; there are no partial successful transfers.

Traversal is iterative and deduplicated, including deep tag/commit graphs. Output
is sorted by full object ID, independent of root order. Each object uses a full
non-delta zlib representation in a version-2 pack. CRC32 tables, fanout, offsets,
pack trailers and index trailers use the repository's native hash width. Bytes
are deterministic for the same object closure and compression implementation;
cross-zlib-version byte identity is not promised. Compression/delta optimization,
thin output, exclusions/negotiation and streaming are separate work.

Default budgets are 100,000 objects, 1,000,000 dependency edges, 64 MiB per object,
256 MiB total object data, 256 MiB pack output and 64 MiB index output. The bundle
is materialized in memory; peak memory includes retained objects and output
copies. Offsets are bounded below 2 GiB; large-offset writing is not advertised.

Native Git `index-pack --strict`, `verify-pack`, `unpack-objects --strict` and
`fsck --strict` qualify empty, merge and annotated-tag closures in both hash
formats. The emitted v2 index matches Git's index for those fixtures. This does
not install packs into a live repository or qualify concurrent maintenance.
The format follows Git's [pack format](https://git-scm.com/docs/pack-format).
