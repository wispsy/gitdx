# Push commands and report status

Push proposals carry an explicit old value, new value and fully qualified ref.
`None` is the typed null value: old `None` means create-only, new `None` means
delete, and two null values are invalid. Native SHA-1/SHA-256 identities cannot
mix. `PushCommand` preserves wire ref bytes; selected mutation backends impose
their portable ref-name profile separately.

`negotiate_push` selects legacy receive-pack discovery and an advertised
report-status version (v1 or v2), optional sideband/offset-delta transport and
explicit atomic mode only when advertised. This does not grant atomicity to a
backend without that capability. `encode_push_request` emits command packets,
first-line NUL capabilities, flush and complete raw pack bytes. Deletion-only
requests omit a pack. `parse_push_request` stops exactly at the flush boundary,
checks capabilities and deletion support, and refuses shallow/certificate/
push-option semantics rather than executing helpers.

`parse_report_status` requires an unpack result and exactly one outcome per
proposed ref. Missing, duplicate, unknown, contradictory or trailing reports
fail. v2 options retain validated ref/old/new/forced-update metadata; callers
must decide whether rewrites fit their selected mutation policy. Error payloads
are bounded opaque bytes omitted from representations. A missing or malformed
report after upload does not establish remote rejection or rollback.

Native Git receive-pack fixtures qualify create/delete requests and both report
versions for both hash algorithms. The library never invokes receive-pack;
Git is a development-only oracle. Production server handlers remain a separate
task. The byte grammar follows [Git pack protocol](https://git-scm.com/docs/gitprotocol-pack).
