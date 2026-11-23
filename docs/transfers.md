# Same-format transfers

Incoming objects pass through a private `ObjectQuarantine` before live refs can
change. `unpack_objects` reads complete unindexed pack v2/v3 bytes, verifies the
native SHA-1/SHA-256 trailer, object framing/counts, declared/inflated sizes and
object codecs. OFS and forward REF deltas resolve under shared byte/operation/
depth limits. Thin packs and missing/cyclic bases are refused; no external
program or implicit object source resolves them.

`receive(pack)` writes only below an owned `objects/incoming-*` directory.
`validate(roots)` traverses the selected typed commit/tree/tag closure through
quarantine plus existing verified storage. Missing/wrong-type edges fail before
publication; gitlinks remain external-repository boundaries. `promote()` requires
successful validation and publishes immutable loose objects before any ref may
name them. Interrupted promotion may leave verified, unreachable objects.
Context exit removes the private directory on normal failures; process termination
or cleanup failure may leave an unreferenced directory for later inspection.

Limits bound pack/object sizes, counts, expanded bytes, delta work and graph
edges. A caller check callback runs between bounded parsing/graph/publication
steps for cancellation/deadlines. This owner-controlled filesystem profile does
not promise atomic multi-ref visibility, power-loss durability or full graph
fsck/maintenance qualification.
