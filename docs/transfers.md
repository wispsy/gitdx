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

## Local transport and public API

```python
from gitdx import LocalTransport, Repository, TransferLimits

source = LocalTransport("/owned/source.git")
repo = Repository.clone(source, "/owned/new-checkout")
result = repo.fetch(source, remote="origin", branches=("main",))
assert result.complete
```

Transport selection is explicit; paths/URLs/config names never choose commands
or adapters implicitly. `LocalTransport` reads a repository through Gitdx and
emits a complete non-thin pack without invoking Git. Fetch requires matching
formats, refuses shallow sources and validates every selected graph before refs.
Branches map to `refs/remotes/<remote>/...`; `tags=True` also selects tags.
Unselected refs are untouched, deleted remote refs are not pruned, local branches
and working content do not move, and rewinds require `allow_rewind=True`.
Existing tags cannot be replaced by this option. Ref names must fit the portable
mutation profile; arbitrary refspec syntax is not interpreted.

Each destination captures an expected-old value before receiving data. Objects
publish first, then refs use independent CAS outcomes. `FetchResult.updated` and
`.rejected` distinguish success from stale/locked/non-fast-forward refs. Unexpected
publication failure or cancellation raises `FetchInterrupted` with known completed
updates; inspect refs before retrying. Multiple refs are not atomic and an I/O
failure can require inspection of the current ref as well as completed outcomes.

Clone discovers/negotiates before reserving a new destination; existing paths are
never reused. It follows advertised HEAD or an explicit branch, supports bare,
detached, empty/unborn and `checkout=False` destinations, and does not guess a
missing remote HEAD. Worktree checkout uses the safe plan/apply path. Network/
format failure before reservation leaves no destination. Later failures retain
the newly owned repository for inspection and raise `CloneIncomplete.path`;
checkout recovery may be required after an interrupted materialization. No
remote URL, credentials or implicit fetch configuration is persisted.

`TransferLimits` bounds refs, wire bytes, packs and total time. `cancel=` accepts
an explicitly supplied event with `is_set()`. Checks occur between bounded local
object/ref/graph steps. Native Git fixtures qualify clone/fetch in both directions
for both algorithms, including packed source repositories and no-Git runtime
execution. Server-side upload/receive protocol handlers are separate tasks.
