# Repository qualification and capability reports

`capability_report()` and `gitdx capabilities` emit versioned JSON without Git,
network access or repository mutation. A capability is `supported`, `unsupported`
or `experimental` by implementation scope. It becomes `tested` only when the
caller explicitly selects matching `QualificationEvidence` whose fixture checks
cover that exact scope. Unsupported profiles stay unsupported even when refusal
fixtures pass. Bounded include inspection is experimental and does not enable
includes during repository mutation.

Evidence binds the installed module bytes, package version, full Python/runtime
platform/machine/OpenSSL identity, exact Git and uv versions, source revision,
source/wheel/sdist profile and installed artifact digest. Both native hash formats
and a mandatory oracle are required. Unknown schemas/records, missing fixture
checks, stale module/runtime fingerprints and installed evidence without artifact
identity are refused. Selected evidence is a development result, not a signed
attestation or a guarantee about a different machine or future repository.

```
gitdx capabilities
gitdx capabilities --evidence /selected/qualification.json
```

Required repository profiles fail before local operations rather than triggering
helpers or approximating data. Alternates, shallow/commondir metadata, unknown
required formats/extensions are unsupported; [reftable](reftable.md) selects a bounded read-only backend. Promisor remote config,
partial-clone filters and pack `.promisor` markers are refused without lazy fetch;
open/index/ref/worktree checks repeat this policy. Filesystem checks assume the
documented owner-controlled profile. The registry does not imply production Git
serving, atomic multi-ref updates or cross-format conversion.

## Native and installed matrix

`scripts/qualify_repository.py` requires Git and uv; a missing oracle cannot turn
the profile into a skipped success. In each hash format it creates native Git
history, annotated tags, index and packed refs, runs native gc, then executes
Gitdx packed-object/delta/ref/index/status/commit/checkout operations in a child
with Git absent from PATH. Separate native REF/OFS fixtures must contain actual
deltas. Native Git then verifies the new commit, worktree status and full object
graph. Unknown extensions/formats, incomplete reftable metadata, alternates, shallow, configured and
marker-only promisor profiles, and a real native partial clone must refuse
without changing file bytes or modes.

The same worker compares raw tree diffs, typed revisions and all criss-cross
merge bases with exact native Git answers. It clones/fetches with local and
smart-HTTP transports, pushes expected-old branch/tag updates, and exercises
both report versions and explicitly advertised atomic HTTP updates. HTTP
discovery/fetch runs separately for protocol 0, 1 and 2. The runtime worker has
an empty PATH, disabled ambient proxies and a failing subprocess entry point;
only the separate development CGI peer executes native Git.

Native clients then clone and push over local and HTTP transports to
Gitdx-created storage. A second no-Git worker reads those native-written objects
after gc. Native `index-pack --strict` verifies a Gitdx-produced pack. These are
client/storage interoperability claims: the CGI peer uses stock Git, and this
does not qualify production Gitdx server routes.

Corrupt incoming packs and exhausted wire budgets must preserve destination
refs/files. Native v3/v4, split and sparse indexes, synthetic unknown required
index extensions and a corrupt index checksum must refuse without file/mode
changes. Graft and packed replacement history are refused by revision queries.
An empty/default capability report never acquires these test claims implicitly.

The receive profile runs native-pack, policy, quota, ownership and interruption
fixtures against the selected source/wheel/sdist import. A separate empty-PATH
worker refuses a policy proposal, then receives a complete graph and verifies
its objects and expected-old ref without an executable. It does not qualify
public server authentication or SHA-1 collision detection.

Files-ref transaction qualification covers deterministic locking, complete
expected-old validation, competing writers, loose/packed deletion, faults at
each publication boundary, commit-marker recovery, corrupt/changed journal
refusal and a real process exit. Native readers must demonstrate intermediate
visibility and native selected-ref writers must fail on owned locks. The
empty-PATH worker executes coordinated receive and a second ref transaction;
atomic visibility remains unsupported.

| Profile | Supported scope exercised | Explicitly refused/separate scope |
| --- | --- | --- |
| Repository/storage | Same-format SHA-1 and SHA-256, files/packed refs, read-only reftable, index v2, native gc, indexed REF/OFS deltas, native-written and Gitdx-written objects | Alternates, shallow/promisor/filter metadata, linked worktrees/commondir, unknown required formats, reftable mutations |
| Local APIs | Stage/status/commit/checkout, explicit-tree byte diffs, typed revision subset, all two-root merge bases | Non-v2/split/sparse/unknown required indexes, graft/replacement history; broader revision syntax remains unsupported |
| Fetch/clone | Local and smart HTTP; legacy 0/1 and protocol v2; complete packs with same-format negotiation | Cross-format conversion, shallow/filter requests, haves/incremental negotiation, implicit helpers |
| Push | Expected-old branch/tag proposals; report-status v1/v2; local individual CAS and native advertised HTTP atomic mode | Local atomic visibility, signed/options/rewritten pushes, production server routes |
| Failure handling | Corrupt/resource-limited incoming transfer refusal; source/installed focused stale/partial/lost-report regressions | A lost report cannot imply rollback or known remote rejection |

CI runs source tests plus this qualification on Linux/macOS/Windows. Wheel and
sdist are built and installed into separate environments outside the checkout,
then run the same mandatory qualification on all three platforms. Installation
paths and artifact hashes are checked. Focused diff/revision/ancestry/push
regressions also run against each installed artifact, including deep histories,
native oracle cases and negative outcomes. The default distribution command
retains full installed test runs; CI selects `--qualification-only` for its
dedicated artifact matrix. Development CGI helpers live with the packaged test
fixtures; they are not runtime package services or dependencies.

```
uv run python scripts/qualify_repository.py --output /selected/source.json
uv run python scripts/check_distribution.py --qualification-only --output-dir /selected/installed
```

Each source/wheel/sdist record names exactly the fixture-qualified scopes; other
implemented features remain `supported`. Reports are uploaded as CI artifacts
for 14 days, with the source revision and exact toolchains. This publishes test
evidence only. It does not publish packages or authorize a release.


## Two-parent merge qualification

Current source adds `worktree.merge-two-parent`, selected only with
`merge-native`, `merge-conflicts`, `merge-recovery` and `merge-no-git`. Each
source/wheel/sdist qualification records the selected checks and scopes. The
native/conflict/recovery suite runs against the selected installed
module before a separate no-Git worker exercises a two-parent merge for each
object format. The supported finite merge profile is documented in
[merging.md](merging.md); this does not certify native alignment heuristics,
recursive bases or hostile filesystem replacement.


## Reset and restore qualification

`worktree.reset-restore` requires `reset-native`, `restore-native`,
`reset-restore-recovery` and `reset-restore-no-git`. Native comparisons cover
soft/mixed/hard reset and independent/combined literal-path restore in both
hash formats. Dirty staging/worktree, untracked collisions, stale previews and
interrupted ref/index/worktree publication are mandatory fixtures against each
selected installed implementation. The separate no-Git worker exercises the
same APIs without a Git runtime.

## Untrusted ingestion qualification

The [explicit ingestion profile](ingestion.md) adds mandatory early SHA-1
refusal, bounded structural validation, native fsck comparisons and a separate
empty-PATH receive check. Linux additionally selects descriptor confinement,
path/link/permission adversarial tests, disk-full/cancellation cleanup and real
process-exit journal recovery. macOS/Windows qualify the public profile refusal;
macOS also exercises POSIX descriptor mechanics without enabling hosting there.
