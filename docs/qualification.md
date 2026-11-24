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
required formats/extensions and reftable are unsupported. Promisor remote config,
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
graph. Unknown extensions/formats, reftable, alternates, shallow, configured and
marker-only promisor profiles, and a real native partial clone must refuse
without changing file bytes or modes.

CI runs source tests plus this qualification on Linux/macOS/Windows. Wheel and
sdist are built, installed into separate environments outside the checkout, and
run the same mandatory qualification on all three platforms. Installation paths
and artifact hashes are checked. Focused installed push fixtures run when that
profile is present; unchanged source behavior is covered by source CI and the
native post-gc worker. The default distribution command retains full installed
test runs; CI selects `--qualification-only` for its dedicated artifact matrix.

```
uv run python scripts/qualify_repository.py --output /selected/source.json
uv run python scripts/check_distribution.py --qualification-only --output-dir /selected/installed
```

Each source/wheel/sdist record names exactly the fixture-qualified scopes; other
implemented features remain `supported`. Reports are uploaded as CI artifacts
for 14 days, with the source revision and exact toolchains. This publishes test
evidence only. It does not publish packages or authorize a release.
