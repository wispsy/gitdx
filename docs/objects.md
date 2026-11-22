# Object contracts

Object payloads, messages, header values and tree names are immutable bytes.
`RawObject.encode()` returns the exact type/decimal-byte-size/NUL/payload
representation used for hashing. `RawObject.oid(format)` returns a full lowercase
`ObjectId` carrying its algorithm. Algorithms cannot be inferred from abbreviated
IDs. Tree and commit child references must use the selected format.

`Blob.to_raw()`, `Tree.to_raw()`, `Commit.to_raw()` and `Tag.to_raw()` serialize
typed values. `parse_object(raw, format)` decodes them. Tree construction sorts
entries using Git directory ordering and rejects duplicates, unsupported modes
and invalid single-component names. Parsing rejects noncanonical ordering rather
than changing the bytes. Supported modes are regular/executable files,
directories, symlinks and gitlinks; recognizing a mode does not implement checkout
or submodule behavior.

Commit/tag parsing retains unknown and duplicate optional headers, continuation
spaces, encodings and message bytes. Required structural headers and full IDs
are validated. Construct commits with explicit author/committer identity bytes;
the library does not invent identities or timestamps. Signature material remains
opaque and unchanged. These codecs neither verify signatures nor translate
histories. They do not claim to accept every malformed legacy Git object.

Path safety belongs to repository operations. A raw Git tree can represent names
that a particular operating system cannot check out safely; decoding a tree is
not authority to write those names to disk. Git-compatible SHA-1 hashing is also
separate from collision-detecting untrusted ingestion.
