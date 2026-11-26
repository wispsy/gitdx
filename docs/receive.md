# Quarantined receive transactions

Current source provides `Repository.receive(commands, pack, policy=policy)` for
an explicitly selected bare repository and native-format `PushCommand` records.
Authentication belongs to the caller. The trusted-storage limits below apply;
this API is not permission to accept hostile filesystem writers or untrusted
SHA-1 ingestion. DX-26 still owns those hosting gates. The published 0.1.0a1
preview predates this receive API.

```python
from gitdx import PushCommand, ReceivePolicy

command = PushCommand(b"refs/heads/main", expected_old, new_commit)
result = destination.receive((command,), complete_pack,
                             policy=ReceivePolicy())
```

Select an explicit `ReceivePolicy`. Defaults allow creation and fast-forward
branch updates, and refuse deletion, branch rewinds and tag replacement.
`allow_deletes`, `allow_non_fast_forwards` and `allow_tag_replacement` opt into
those operations; configured `receive.denyDeletes`/`denyNonFastForwards` remain
restrictions. Branches must target commits. Only portable, bounded names under
`refs/heads/` and `refs/tags/` are supported; symbolic destinations, aliases and
overlapping names are refused. A batch with any non-deletion target supplies a
complete non-thin pack, including when objects already exist. Delete-only
batches supply no pack.

`object_check(oid, raw)` returns a boolean for every validated incoming/reachable
object. `ref_check(view, command)` returns `None` to allow or 1–128 printable
ASCII bytes describing refusal. These are trusted Python callables selected by
the application, never repository-configured code. They must not mutate storage
or return credentials in a reason. The view reads quarantined/existing objects
and provides bounded `is_ancestor(old, new)` without external Git. Ref policy is
checked before object promotion and again immediately before each ref CAS. A
native writer may still move the ref; expected-old comparison remains mandatory.
Local transport now uses this same intake and publication implementation.

All supplied objects, including objects outside selected tip closures, undergo
connectivity and kind validation. Commit parents, trees, blobs and annotated tag
targets must be complete and type-consistent. Gitlinks refer to another
repository and do not require its objects locally. Bounded REF/OFS deltas are
accepted only with bases inside the pack. Configured hooks, proc-receive and
native fsck requirements are refused without execution.

`ReceiveLimits` selects `TransferLimits` (pack expansion, object count/size,
delta work, wire bytes, ref count and deadline), `PackWriteLimits` for the full
graph (objects, edges and total bytes), and a quarantine file-byte budget.
Cancellation is checked throughout intake, graph reads, promotion and between
ref updates. Quarantine disk accounting includes its receipt and compressed
object bytes; filesystem allocation/metadata is outside that byte count. A
single bounded object can temporarily exceed the budget before its size check;
no ref is published on that failure. These are per-operation bounds, not a
global repository disk quota or an interruptible operating-system I/O deadline.

Each quarantine records its directory/root identity and a private receipt.
Replaced/foreign storage is neither promoted nor recursively deleted. No
automatic orphan adoption occurs. Caller-owned directories and controlled
ancestors remain required; static identity checks do not provide a sandbox
against hostile concurrent path replacement. Cleanup failure leaves private,
unreferenced storage and sets `cleanup_pending` and a safe exception class name.
An owning live quarantine may retry `cleanup()`; unknown orphans require owner
inspection.

Verified immutable objects publish before refs. Rejected ref proposals can leave
verified unreachable objects when another proposal was accepted or publication
was interrupted. `ReceiveResult` records unpack success, received/promoted
counts, cleanup state and per-ref `accepted`/`rejected` outcomes. `ReceiveRejected`
carries an invalid pack/graph/object-policy result before any ref publication.
`ReceiveInterrupted` carries completed outcomes, `not-attempted` refs and an
`unknown` ref when publication may have occurred before an error. Do not infer
rollback from an exception or retry an unknown outcome blindly. Promoted counts
are completed write calls, including existing objects, not a new-object count;
an interrupted write may have published one additional immutable object.

This profile performs independent single-ref CAS. A stale/locked ref can be
rejected while another is accepted. `atomic` is always false; local discovery
does not advertise atomic. Coordinated files-ref recovery is the next backend
task and still cannot promise atomic multi-ref visibility to native readers.
