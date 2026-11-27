# Native hosting API, version 1

`hosting-native/1` is the selected standard-library engine contract for hosting
applications. It qualifies native SHA-1/SHA-256 storage, bounded read serving,
explicit receive/ref outcomes, conservative online maintenance and independently
restorable snapshots on Linux/macOS/Windows. The `hosting.api-v1` capability is
`supported` without evidence and becomes `tested` only when explicitly selected
source/wheel/sdist evidence matches module bytes, package/runtime/platform,
toolchain and artifact/source identities. Package version alone is insufficient.
This source contract does not authorize a package upload or public deployment.

## Selected public surface

Import these APIs from `gitdx`; callers need no Git executable or HTTP framework.

| Boundary | Public entry points and selected records |
| --- | --- |
| Storage | `Repository.init/open`, `ObjectFormat`, `ObjectId`, `RawObject`, `StoreLimits`, `PackLimits`, `repository.objects.read/write` |
| Refs/history | `repository.refs.read/resolve/snapshot/update/delete/transaction/recover_transaction`, `RefChange`, `DirectRef`, `SymbolicRef`, bounded `resolve/is_ancestor/merge_bases/pack` |
| Read serving | `UploadPack`, `UploadPackLimits`, explicit `advertise/respond/chunks`, native-format protocol records |
| Intake | `repository.receive/recover_receive`, `PushCommand`, `ReceivePolicy`, `ReceiveLimits`, `IngestionProfile`, `IngestionLimits`, `FilesystemLimits` |
| Outcomes | `ReceiveResult`, `ReceiveOutcome`, `ReceiveState`, `ReceiveRejected`, `ReceiveInterrupted`, `RefTransactionResult`, `RefTransactionState`, `RefTransactionInterrupted` |
| Local reftable | `repository.refs.writer()`, `ReftableChange`, `ReftableWriteResult`, `ReftableWriteInterrupted`, explicit `write_object/transaction/compact/recover` |
| Maintenance | `plan_maintenance`, `MaintenancePlan`, `apply_maintenance`, `MaintenanceResult`, `recover_maintenance` |
| Backup | `backup_repository(..., writer_guard=...)`, `verify_backup` at a fresh independent destination |
| Evidence | `capability_report`, `QualificationEvidence`, `implementation_digest`, `runtime_toolchain` |

Object IDs always select one native hash; capability negotiation never converts
graphs. Refs and objects remain authoritative in Git storage. Callers supply
explicit expected-old values and must consume complete receipts. Additive APIs
outside this selected surface do not implicitly expand the hosting profile.
Isolated graph translation remains experimental and is not a migration/transport
or hosted-write fallback.

## Storage and trust selection

| Operation | SHA-1 | SHA-256 | Platform/storage conditions |
| --- | --- | --- | --- |
| Trusted local files storage and client operations | Supported | Supported | Qualified owner-controlled Linux/macOS/Windows storage |
| Read-only upload-pack | Supported | Supported | Host authorizes before refs/objects and retains request/resource bounds |
| Untrusted receive | Refused before intake | Supported | Qualified Linux descriptor/allocation profile; controlled owner/ancestors |
| Explicit local reftable writer | Supported | Supported | Native-compatible stack publication on Linux/macOS/Windows; select writer explicitly |
| Hosted/untrusted reftable receive | Refused | Refused | A local writer does not qualify hosted intake |

`ReceivePolicy.ingestion` defaults to `TRUSTED_LOCAL` for explicit local callers.
Hosts must select `UNTRUSTED`; authentication and authorization belong to the
application. A rejected profile must never fall back to trusted intake. Stock
receive clients need `no-thin` when the host selects complete non-thin packs.
Framework adapters own request framing/media types, admission and disconnects.

`object_check` and `ref_check` are trusted application callables over validated
objects and the intake overlay. `publication_guard(view, commands)` retains an
application context from after promotion through final ref-lock checks and
publication. Acquire catalog exclusion before ref locks, recheck time-dependent
expiry at final policy checks, and persist sanitized outcomes separately. No
configured hooks, filters, drivers or external executable runs implicitly.

## Durability and concurrency

Verified immutable objects publish before refs. Independent receive can report
partial acceptance; coordinated files-ref receive checks all expected values and
policy under deterministic locks. It still reports `atomic=False`: native readers
can observe intermediate files-ref values. Unknown outcomes require inspection,
not inferred rollback or a blind retry. Prepared ref journals restore known
before-bytes; committed journals finalize known after-bytes. Later edits, foreign
identities, corrupt journals and unknown locks refuse recovery. A process exit
can leave exact owned locks that require external owner reconciliation.

Explicit reftable batches publish one immutable stack-list snapshot atomically.
Separate queries, or separate native iterators in one command, may select
different snapshots. Compaction preserves exact-case refs, symbols, per-ref
update indexes and native log bytes. Retired tables remain for readers. The
ordinary repository interface keeps its read-only reftable mutation default.

Online maintenance retains every inventoried object and old pack before pruning
only old loose duplicates. New receive/ref writers and readers can overlap that
operation; external destructive GC/removal must be excluded. Unreachable expiry
and global storage quotas are not enabled. Backups require caller-owned exclusion
of every ref/reflog writer, including native Git, and copy complete retained
closures into fresh storage with a verified receipt. They copy no hooks or
credentials and never replace a live source implicitly.

These are explicit owner-controlled process-interruption and file-sync/rename
contracts. They are not cross-file power-loss guarantees, a sandbox against
service-owner code, or a distributed transaction with a hosting catalog. See
[receive](receive.md), [ref transactions](ref-transactions.md),
[reftable](reftable.md), [maintenance](maintenance.md) and [ingestion](ingestion.md).

## Resource and performance budgets

Every host should select smaller budgets suited to its admission/disk policy.
Engine defaults are finite and remain independent of a service-wide quota:

| Scope | Selected defaults |
| --- | --- |
| Read negotiation/output | 1 MiB request, 64 MiB response; 16 MiB object and 128 MiB reachable bytes |
| Receive transfer | 256 refs, 288 MiB wire bytes, cooperative 30 seconds |
| Receive graph | 100,000 objects, 1,000,000 edges, 64 MiB object, 256 MiB aggregate bytes |
| Quarantine | 288 MiB accounted file bytes; allocation reservations are separate |
| Untrusted filesystem | 200,000 work entries, 1 GiB I/O, 768 MiB reserved allocation, depth 64; separate bounded cleanup |
| Files-ref journal | 256 changes, 48 MiB journal, 16 MiB packed snapshot, 4096-byte individual ref snapshots |
| Reftable stack | 64 MiB/64 tables, 200,000 records, 16 MiB inflated block, 4096-byte key; local writer uses one ref/log block with at most 65,535 restarts each |

The separate no-Git worker runs a representative 32-commit/96-object workload
in both formats: pack, receive, retain-all repack/prune, backup and verification.
It selects 128 graph objects, 512 edges, 8192-byte objects, 256 KiB graph bytes,
512 KiB wire bytes and eight commands, and checks deliberate object-budget
exhaustion. Linux's SHA-256 workload selects actual untrusted intake; other
workloads select trusted local storage. A concurrency fixture keeps receive
intake active while maintenance prunes its old loose copies, then checks the
accepted graph with native fsck.

`*-hosting.json` supplements the ordinary qualification receipt with observed
stage times, counts/bytes, explicit budgets, implementation/toolchain/source and
artifact identities. Observed times are measurements, not wall-clock thresholds
or a service guarantee. Deadlines are cooperative; kernel I/O/compression is not
forcibly interrupted. CI retains source/wheel/sdist records for 14 days.

Alternates, shallow/filter/promisor profiles, linked worktrees, unknown required
extensions, configured helpers, untrusted SHA-1, broader unqualified-platform
confinement, global quotas, destructive retention and public deployment remain
outside this version. Package publication is a separate owner-selected action.
