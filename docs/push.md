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

## Local push

```python
from gitdx import LocalTransport, PushUpdate

tip = source.refs.resolve().oid
result = source.push(LocalTransport('/owned/destination.git'), (
    PushUpdate('refs/heads/main', tip, expected_old=None),  # create-only
))
assert result.complete
```

`expected_old` is required, including explicit `None` for creation. `new=None`
deletes an existing ref. Proposals select portable branch/tag names; overlapping
or aliased batches are refused. Format, advertised old values, object types and
fast-forward/tag replacement policy are checked before upload. A failed
preflight rejects the affected proposals and marks the rest `not_sent`.
`force=True` permits client-side non-fast-forward/tag replacement proposals but
never bypasses expected-old CAS or receiver policy.

The sender reads immutable objects, builds a complete pack and never moves local
branches/tracking refs, index or working files. The selected local destination
must be bare. Its actual capabilities are recomputed by the receiver rather than
trusted from the caller. It never advertises atomic push and refuses an atomic
request before mutation. Complete incoming graphs validate in private quarantine;
objects publish first, followed by independent ref CAS updates. `receive.denyDeletes`
and `receive.denyNonFastForwards` are enforced. Active/configured receive hooks,
proc-receive and requested native fsck policy are refused rather than ignored or
executed; broader policy/server work remains separate.

`PushResult.outcomes` records `accepted`, `rejected`, `not_sent` or `unknown` per
proposal. Receiver rejection is reported with a bounded generic reason, without
echoing peer text. `peer_atomic` records advertisement, `atomic_requested` records
caller selection, and `atomic` confirms only a consistent reported atomic mode.
An I/O failure, cancellation or incomplete/inconsistent report after upload raises
`PushUncertain.result`; affected outcomes stay `unknown`, with atomic completion
unconfirmed. A remote may already have accepted some or all refs. Inspect it before
retrying; the sender never rolls back remote updates or rewrites local state.
Rejected/failed publication can leave verified unreachable destination objects.

Native Git client pushes into Gitdx-created storage and Gitdx local pushes read by
native Git qualify both algorithms. These are storage/client interoperability
fixtures; they do not supply a production receive-pack listener.
