# Bare repository maintenance and backup

`plan_maintenance(repository, grace_seconds=...)` inspects bare files-ref storage
without mutation. It validates every inventoried loose/packed object and its
typed dependency closure, and reports reachable/unreachable IDs. Refs, detached
HEAD and all existing reflog old/new IDs are retention roots. Count, byte,
directory depth and cancellation budgets are explicit through `PackWriteLimits`
and `check`. Invalid objects, incomplete graphs and pending ref/worktree recovery
refuse. This is a bounded connectivity/hash check, not every native fsck policy.

`apply_maintenance(repository, plan, prune=True)` writes a deterministic,
non-delta pack retaining **all** planned objects, including unreachable objects.
It publishes the complete pack before its index. Existing packs stay available
to readers. Only planned loose copies older than the selected grace period can
be pruned, and only after the retained pack validates. Device/inode/size/mtime
checks preserve later replacements. Newly written objects and refs survive a
stale plan. The default grace is 14 days; zero requires an explicit selection.

This conservative online GC profile consolidates loose storage. It does not
expire unreachable packed objects, delete old packs, expire reflogs, enforce a
global disk quota or promise progress under an uncooperative native pack
deletion. Concurrent readers and object/ref writers are supported; concurrent
external GC/removal must be excluded. No hook, filter or helper executes.

Pruning creates a checksummed, identity-bound `maintenance-state.json` checkpoint.
`recover_maintenance(repository)` verifies the retained pack and idempotently
finishes remaining duplicate removals. Cancellation/I/O failure retains that
checkpoint. A separate `maintenance.lock` excludes competing maintenance. After
a process exit, unknown lockfiles require owner reconciliation; recovery never
steals them. Neither rollback nor a cross-file power-loss guarantee is claimed.

`backup_repository(repository, fresh_path, writer_guard=guard)` creates an
independent bare repository containing the exact selected refs, HEAD, reflogs
and their complete object closure. The required context manager must exclude
all ref/reflog writers, including native Git, for the snapshot. A service-wide
writer admission gate or externally established offline window supplies this
contract; native lockfile inspection cannot prove it. The implementation also
checks metadata stability before completion. Tests use `nullcontext()` only
where the fixture is already quiescent.

Backups have fresh safe config and copy no hooks, credentials, worktree, arbitrary
configuration or hosting catalog. Existing destinations are never overwritten.
An interruption may leave an incomplete destination; only a completed snapshot
has `backup.json`. `verify_backup(path)` checks that receipt, refs, reflogs,
pack checksum and typed object closure before the backup is selected for restore.
Restore uses the verified independent repository at a separately selected path;
it never replaces a live source implicitly. A complete hosting backup must also
coordinate its catalog, audit/outbox and credentials under the hosting policy.
