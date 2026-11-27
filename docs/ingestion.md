# Explicit ingestion profiles

Current source adds `ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED)`.
The hosting application selects this policy after authentication. Request data
and repository configuration cannot downgrade the selected profile. The
published 0.1.0a1 preview predates these APIs.

Untrusted receive accepts **native SHA-256 only**. SHA-1 is refused before any
storage inspection, quarantine, decompression or policy callback. Ordinary SHA-1
hashing has no collision detector. Trusted local operations retain both native
formats; no conversion or external helper is selected implicitly.

```python
from gitdx import IngestionProfile, ReceivePolicy

result = repository.receive(
    commands, complete_pack,
    policy=ReceivePolicy(ingestion=IngestionProfile.UNTRUSTED),
    coordinated=True,
)
```

## Qualified filesystem boundary

The untrusted profile requires Linux, descriptor-relative filesystem operations,
`O_NOFOLLOW`/`O_DIRECTORY`, and filesystem fragments at most 4096 bytes. Other
platforms fail before staging. POSIX mechanism fixtures also run on macOS with
an explicit test override; that does not enable macOS hosting.

The service owns the repository, its ancestors and all cooperating writers.
Opening a repository captures its device/inode identity. Receive pins that root
with a directory descriptor and refuses a changed identity. Descendant reads,
locks, temporary files, publication, journal recovery and recursive cleanup use
descriptor-relative operations. Directory components cannot be symlinks; data
must be owner-controlled regular files with one link. Group/world-writable
storage, hardlinked files, FIFOs, device files and filesystem crossings fail
closed. New directories/files use 0700/0600. Parent replacement cannot redirect
an already opened directory descriptor to an outside symlink target.

This confines untrusted payloads inside service-controlled storage. It is not a
sandbox against malicious code running as the service owner, arbitrary mount or
ancestor replacement, or non-cooperating privileged filesystem writers. Separate
accounts and controlled parents remain deployment requirements.

## Validation and operation budgets

`ReceiveLimits` combines wire/deadline/ref limits, pack/object/decompression/delta
bounds, complete graph budgets, `IngestionLimits` and `FilesystemLimits`.
Declared inflation and delta output are reserved before allocating their output;
delta programs check cancellation every 1024 instructions. Commit/tag headers
and tree-entry counts are bounded before structural decoding. The selected
validation checks object grammar, complete typed graph edges, canonical Git
identities, unsafe dot/dotdot/dot-git names and symlink `.gitmodules`. It is a
documented structural subset, not every configurable native `git fsck` rule.

Defaults bound headers to 2048/256 KiB, tree entries to 100,000, ref paths to 32
components of at most 255 bytes, filesystem work to 200,000 entries/components,
I/O to 1 GiB and path depth to 64. Filesystem reservations default to 768 MiB;
the quarantine reservation defaults to 288 MiB. Untrusted quarantine accounting
reserves file data rounded to 4096 bytes and 4096 bytes for each new directory,
including its private receipt, before writing. The broader filesystem budget
also covers object promotion and journal files. Small lockfiles and filesystem
metadata/journals are not a physical-volume quota. Reads use at most 64 KiB per
chunk; a failing I/O-byte charge may already have read that one bounded chunk.

These are cumulative per-operation work/allocation reservations, not a global
repository quota or a hard interruption of a blocking kernel I/O call. The host
must separately bound concurrent requests, durable storage and retained orphans.
Disk-full and cancellation failures preserve explicit publication outcomes.

## Cleanup and interrupted recovery

Private quarantines have ownership receipts and device/inode identities. Cleanup
uses a separate bounded allowance (twice the work budget plus 1024 entries and
1 MiB of metadata reads), independent of cancellation/exhausted request budgets.
It cannot silently adopt a replaced directory. Failures report `cleanup_pending`.
A killed process can leave unreferenced incoming storage; an operator must
inspect and remove proven-owned orphans. Unknown lockfiles are never stolen.

Coordinated ref publication retains the existing prepared/committed journal
contract. Once any owned dead-writer locks have been reconciled, the owner may
call `repository.recover_receive(ingestion=IngestionProfile.UNTRUSTED)` under the
same confinement. Prepared journals roll back; committed journals finish. A
failure does not imply rollback. This is process-interruption recovery, without
cross-file power-loss durability or atomic visibility to native readers.

Installed qualification selects `receive.ingestion-validation` on every platform
and `receive.untrusted-sha256` only on Linux. Native comparisons, early SHA-1
refusal, path-swap/link/FIFO/permission failures, allocation/decompression limits,
disk-full/cancellation cleanup and actual process-exit recovery are mandatory
fixtures. The empty-PATH worker exercises real Linux SHA-256 ingestion or the
other platforms' refusal. Public HTTP receive and deployment remain separate
Project gates.
