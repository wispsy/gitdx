# gitdx

Initial PyPI preview: **0.1.0a1**. Released behavior and current development are
tracked in [Wispsy Releases](https://github.com/orgs/wispsy/projects/31).

A standalone, standard-library Python Git engine with Git-compatible storage and protocols.

Python 3.14+ · MIT

[Capability reports](docs/qualification.md) distinguish implementation support
from explicitly selected, runtime-bound qualification evidence.
The native compatibility matrix covers source/wheel/sdist on Linux, macOS and
Windows, including same-format local/HTTP exchange and separate no-Git workers.

[Byte/tree diffs](docs/diffs.md) preserve byte paths, modes and content, with
bounded LF hunks, binary summaries and optional deterministic rename matching.

[Revision resolution](docs/revisions.md) selects explicit refs/OIDs, unambiguous
abbreviations, parent suffixes and typed tag peeling within traversal budgets.

[Push proposals](docs/push.md) select expected-old branch/tag updates and expose
per-ref outcomes through explicit local or smart-HTTP destinations. Atomic
requests require peer advertisement and consistent reports; local refs remain
independent transactions.

Current source adds [quarantined receive transactions](docs/receive.md) with
explicit object/ref policy, complete graph validation and interruption outcomes.
[Explicit ingestion profiles](docs/ingestion.md) retain trusted local SHA-1 and
SHA-256 support and select SHA-256-only untrusted receive on qualified Linux
storage. Public HTTP receive is a separate gate; the published preview is unchanged.

[Coordinated ref transactions](docs/ref-transactions.md) validate every expected
old value under deterministic locks, with explicit prepared/committed recovery.
Native readers can observe intermediate values; atomic visibility is refused.

[Configuration policy](docs/configuration.md) provides ordered Git values,
quoting/continuations and explicitly confined include inspection.

[Commit/history](docs/history.md) builds staged trees with explicit identities
and expected-HEAD publication, plus bounded parent-order history and ref listings.

[Two-parent merge plans](docs/merging.md) on current source provide bounded
text merges, conflict index stages, explicit commit resolution, guarded abort
and process-interruption recovery.
[Reset/restore plans](docs/reset-restore.md) separate ref, index and worktree
effects, with explicit discard and the same guarded recovery.

[Pack writing](docs/pack-writing.md) validates selected object closures and emits
native-format non-delta packs with v2 indexes, without a Git runtime.

[Safe checkout](docs/checkout.md) provides reviewable plans, protected untracked
content, explicit tracked overwrite and guarded journal recovery.

[Protocol framing](docs/protocol.md) adds bounded packet/sideband streams,
v0/v1 discovery and v2 command boundaries with explicit same-format negotiation.

[Upload-pack sessions](docs/upload-pack.md) provide bounded read-only v0/v1/v2
discovery and complete-pack fetch responses for an already authorized host.

[Fetch/clone](docs/transfers.md) uses explicit local or smart-HTTP transports,
private object quarantine, validated graph closures and expected-old tracking
refs. HTTPS verifies certificates; authorization is explicitly selected.

Development starts with the [implementation plan](IMPLEMENTATION_PLAN.md) and
[shared Project](https://github.com/orgs/wispsy/projects/30). The roadmap records
qualification targets; server handlers and public hosting remain later milestones.

Source development does not imply a published package or deployed service.

The initial Python API provides SHA-1/SHA-256 identities and byte-preserving
blob, tree, commit and annotated-tag codecs, without runtime dependencies or
calls to the Git executable:

```python
from gitdx import Blob, ObjectFormat

blob = Blob(b"hello\n").to_raw()
print(blob.oid())                    # SHA-1
print(blob.oid(ObjectFormat.SHA256)) # SHA-256
```

[Object contracts](docs/objects.md) describe byte handling and validation.
[Loose storage](docs/storage.md) adds bounded verified reads and atomic
publish-if-absent writes under an existing owner-controlled objects directory.
[Repository lifecycle and plumbing](docs/repositories.md) provide `init`,
`hash-object` and `cat-file`, with separately qualified staging and transfer APIs.

[Files refs](docs/refs.md) include packed refs, guarded deletion, explicit reflog
outcomes, validated single-ref compare-and-swap and
explicit unborn, symbolic and detached HEAD handling through `Repository.refs`.

[Read-only reftable storage](docs/reftable.md) selects native v1/v2 table stacks,
exact-case references and the same history interfaces; mutations remain refused.

[Index v2 transactions](docs/index.md) expose format-aware staging snapshots
and exclusive, explicitly committed writes through `Repository.index`.

[Staging/status](docs/working-tree.md) adds explicit paths/removals, scoped
ignore rules and byte-oriented porcelain status without invoking Git.

[Indexed pack reading](docs/packs.md) provides bounded SHA-1/SHA-256 base-object
access and bounded iterative OFS/REF delta resolution; incoming transfer packs
also resolve deltas in private quarantine.

Run `uv sync --locked` and `uv run python -m unittest discover -s tests -v`.
Git is used only as a test oracle; `GITDX_REQUIRE_GIT=1` makes a missing oracle
an error. `uv run python scripts/check_distribution.py` checks isolated wheel
and sdist installations and runtime startup without Git on PATH.
