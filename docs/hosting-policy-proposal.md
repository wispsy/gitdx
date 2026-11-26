# Proposed untrusted-ingestion policy (DX-26)

Status: proposed, awaiting the owner's SHA-1 policy choice. This document does
not enable an untrusted hosting profile or change trusted local compatibility.

## Recommendation

Initially permit **native SHA-256 only for untrusted receive**. Keep SHA-1 and
SHA-256 available for explicitly trusted local operations. Refuse untrusted
SHA-1 before quarantine creation or pack inflation, with no format conversion,
fallback to ordinary SHA-1, subprocess helper or new runtime dependency.

This means a SHA-1 repository cannot push to that hosted write profile. Existing
SHA-1 repositories are not rewritten. Same-format compatibility remains the
contract; graph translation/migration is separate DX-29/DX-30 work. Read-only
serving of trusted, previously enrolled SHA-1 storage can be qualified separately
from accepting untrusted objects into it.

## Current evidence and boundary

- `RawObject.oid()` uses `hashlib.new(format.value, ...)`; incoming pack checksum
  validation does too. These are ordinary digest checks. No collision detector
  is selected, bundled or qualified.
- The runtime remains standard-library-only. Native Git is a development oracle,
  never a runtime collision-detection provider.
- Receive now checks complete supplied/reachable graphs, target kinds, pack/delta
  work and selected quotas. Ref transactions add coordinated locks and explicit
  interruption recovery. Both native hash formats pass trusted-storage fixtures.
- Directory/root identities, receipts and static symlink/reparse checks assume
  controlled storage paths. They do not establish hostile concurrent filesystem
  replacement protection, crash-proof global disk quotas or cross-file power-loss
  atomicity. The new APIs do not claim an untrusted hosting capability.

Git's [hash transition rationale](https://git-scm.com/docs/hash-function-transition)
distinguishes hardened SHA-1 from ordinary hashing and describes the move to
SHA-256. [Upstream SHA1DC](https://github.com/cr-marcstevens/sha1collisiondetection)
provides a C collision-detection library under MIT, with a collision indicator
and documented attack coverage. Its existence is not evidence that a Python
binding, selected build or this engine's ingestion paths are qualified.

## Concrete implementation after selection

1. Add an explicit ingestion profile selected by the trusted application, separate
   from branch/authorization policy and immutable native object-format selection.
   Remote commands, object contents and request headers cannot choose its trust
   level. The future Gittycat receive service must select the untrusted profile.
2. In that profile, reject SHA-1 storage before staging, inflation or policy hooks.
   Trusted local APIs keep their existing two-format behavior. Advertisements
   and receive services must agree on allowed formats; no hidden conversion.
3. Close remaining path/alias and decompression/allocation gaps for each selected
   hosting filesystem profile. Descriptor/handle-based confinement must be
   qualified; refuse unqualified platforms instead of substituting static checks.
4. Add an adversarial fixture corpus covering malformed/ambiguous paths, concurrent
   replacement, object and graph kinds, compressed/delta resource exhaustion,
   quota/disk failures, cancellation, file permissions and interrupted recovery.
   Record concrete allocation/work/disk limits and durability assumptions.
5. Qualify installed artifacts with SHA-1 untrusted refusal before any intake,
   SHA-256 adversarial receive and unchanged trusted two-format interoperability.
   A format gate alone does not establish complete hosting hardening. CAT-08 and
   public serving remain behind all other Project gates.

## Alternative requiring a provider workstream

If untrusted SHA-1 ingestion is required, select an audited collision-detection
provider and binding first. Record exact upstream source, license notices,
platform/toolchain/wheel support, streaming/cancellation behavior and failure
semantics. Validate published collision vectors and engine-specific object/pack
hashing paths, including reconstruction, immutable-object deduplication and
existing-object comparisons. Missing/unsupported detectors must fail closed.
Any additional runtime/native dependency and packaging support needs explicit
selection; invoking the installed Git executable is not an allowed substitute.

The owner choice is between the recommended initial SHA-256-only untrusted
profile and prioritizing that provider qualification workstream. It does not
authorize deployment, publication, rewriting existing repositories or weakening
the independent filesystem/resource gates.
