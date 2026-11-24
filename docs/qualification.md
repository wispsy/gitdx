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
