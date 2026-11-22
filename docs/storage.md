# Loose storage

`LooseObjectStore(path, format, limits=StoreLimits(...))` uses an existing
owner-controlled `objects` directory. `read(ObjectId)` verifies complete zlib
framing, a canonical type/size header, payload size and the requested object ID.
`write(RawObject)` returns its ID and publishes a fully flushed temporary file
with a hard link that cannot replace an existing object. A competing duplicate
writer succeeds only if the existing bytes validate and match. Filesystems
without hard-link support report a write failure rather than falling back to a
potentially destructive replacement.

Defaults bound the payload and compressed file to 64 MiB each and the header
including NUL to 64 bytes. Compressed input is read only up to its limit; expanded
output is limited during decompression, before parsing. Trailing streams and
truncated checksums fail. These per-object limits do not yet provide hosting CPU,
aggregate graph or repository-disk quotas.

Static symlinks within storage are rejected, including fanout/object paths.
The caller must control the directory and its ancestors: the current portable
checks do not defend against concurrent malicious directory replacement. Atomic
visibility is tested, including interrupted writes and competing writers. Full
power-loss durability, packed-object access, garbage collection and hostile
SHA-1 ingestion are later qualification milestones. Missing loose objects report
the current packed-object limitation explicitly.
