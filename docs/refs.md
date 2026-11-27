# Files refs, packed refs and HEAD

```python
from gitdx import Commit, Repository, Tree

repo = Repository.init("example")
tree = repo.objects.write(Tree(format=repo.object_format).to_raw())
identity = b"Example <example@example.invalid> 1700000000 +0000"
oid = repo.objects.write(Commit.create(tree, author=identity, committer=identity,
                                      message=b"Initial\n").to_raw())
repo.refs.update("refs/heads/main", oid, expected=None) # create-only
assert repo.refs.resolve().oid == oid
```

`read(name)` returns a `DirectRef`, `SymbolicRef` or None. `resolve(name="HEAD")`
follows at most 15 symbolic links and returns the terminal name, optional ID
and symbolic chain. Unborn and detached HEAD have explicit properties. A missing
target can be represented; resolution does not assert graph connectivity.

`update(name, new, expected=old)` updates direct refs only. None is a create-only
expectation, never an unconditional overwrite. Symbolic refs cannot be detached
implicitly: use `detach_head(new, expected=<exact RefValue>)` or
`set_symbolic(name, target, expected=<exact RefValue or None>)`. Branch/detached
targets must be existing structurally valid commits. This does not yet verify
their entire reachable graph, ancestry, fast-forward policy or signatures.

Names use fully qualified `refs/` paths, plus the explicit HEAD pseudo-ref.
The initial portable subset accepts ASCII, applies Git's forbidden sequences
and additionally rejects Windows-invalid/device names and case-fold aliases.
Non-ASCII refs and platform-specific variants need later compatibility work.
Static symlink paths and existing lockfiles are refused. Owned .lock files are
flushed and atomically replaced only after the expected value matches; failed
writes clean only their own locks. Calls update one ref, not an atomic set.

`packed()` reads bounded sorted packed refs, including peeled IDs. Loose values
take precedence. Mutations coordinate a per-ref lock and `packed-refs.lock`;
deleting a direct ref requires its expected old ID and removes its packed entry
before the loose value. The packed lock stays present across both steps so native
pack-refs cannot republish an old loose value. HEAD deletion is refused.

If removing a loose override fails after packed-file publication, the old loose
value remains authoritative; retry the deletion after correcting the failure.
Stale locks after a process crash require owner inspection. Readers see atomic
file replacements, not a transactional view of multiple reference files.

`update`, `delete`, `set_symbolic` and `detach_head` return `RefUpdateResult` on
committed ref changes. To write a reflog, pass `reflog=ReflogUpdate(identity,
message)` with explicit Git identity bytes including timestamp/timezone. Logging
is opt-in: core.logAllRefUpdates alone does not invent identity or enable writes,
and changing a branch does not automatically append a separate HEAD log. Deletion
retains the explicit audit log with a zero new ID.

Logs append after ref publication. A failed append returns `committed=True`, a
`reflog_error` and, when preparation succeeded, a recovery receipt. Do not repeat
the ref update. `repair_reflog(result)` locks the ref, checks it has not moved and
repairs an absent/partial/already-written append idempotently. Changed history or
a missing receipt requires manual reconciliation; unrelated log data is never
overwritten. An interrupted response still requires a fresh ref read.

Packed refs are limited to 16 MiB/100,000 entries and logs to 8 MiB. Reftable writes,
shared worktrees, multi-ref transactions and full crash durability remain later
work. All operations assume owner-controlled directories; hostile filesystem
replacement and hosting hardening remain separately gated.
