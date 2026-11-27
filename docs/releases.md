# Developer previews

## 0.1.0a2

This developer preview packages the integrated native hosting API baseline.
Production qualification is unfinished. Hosted writes remain SHA-256-only on
qualified Linux descriptor storage. SHA-1 remains available for trusted local
work and read serving.

- Quarantined receive and coordinated files-ref publication guards, including
  stock-Git receive capability parsing and explicit unknown-outcome recovery.
- Bounded upload-pack, native reftable reading and explicitly selected local
  reftable transactions/compaction/recovery.
- Conservative retain-all maintenance, recoverable loose-duplicate pruning and
  verified bare backups under caller-owned exclusion of all writers.
- Experimental unsigned graph translation between isolated SHA-1/SHA-256 stores.
- The documented hosting-native/1 public API, hard budgets and source/wheel/sdist
  qualification with native Git as an oracle and separate no-helper workers.

The integrated baseline passed 307 tests and source/installed checks on Linux,
macOS and Windows. Release artifacts pass their own identity and installed
qualification gates before PyPI upload. Qualification is bound to the recorded
runtime/profile; it does not establish production serving.

Untrusted SHA-1, unqualified-platform hosted writes, atomic files-ref visibility,
global quotas, destructive packed-object expiry, complete hosting backup and
public deployment remain outside this preview. Repository migration and
cross-format transport retain separate policy gates. Existing capability/API
limits are described in [hosting-api.md](hosting-api.md).

Gittycat 0.1.0a2 selects the published `gitdx==0.1.0a2` package. Repository
browsing, collaboration/reviews and durable jobs can follow the paired preview.

## 0.1.0a1

Initial published preview. Its immutable release and artifact history remain
available in the [release](https://github.com/wispsy/gitdx/releases/tag/v0.1.0a1).
