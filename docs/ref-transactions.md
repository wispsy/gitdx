# Coordinated files-ref transactions

`repository.refs.transaction(changes)` validates a complete expected-old batch
and publishes it under deterministic locks. Each `RefChange(name, new, expected)`
selects a fully qualified direct ref, an ObjectId or deletion (`new=None`), and
an explicit expected ID or creation (`expected=None`). HEAD and symbolic updates
remain separate single-ref APIs. A batch contains 1–256 changes with bounded
portable names. Case aliases and directory/file namespace changes are refused.

```python
from gitdx import RefChange

result = repository.refs.transaction((
    RefChange("refs/heads/main", new_main, old_main),
    RefChange("refs/tags/release", new_tag, None),
))
assert result.committed
assert result.atomic is False
```

The backend acquires `packed-refs.lock`, then every selected ref lock in name
order. Lock acquisition is nonblocking: contention releases only locks owned
by this operation. It validates all current values and target types before any
ref publication, and repeats expected-value/physical checks immediately before
publication. `before_publish()` is an optional trusted guard called under all
locks before journal creation and again before publishing. It can raise to
refuse; it must not mutate storage. `check()` supports cooperative cancellation
through lock acquisition, validation, publication and recovery.

The guarantee is **coordination and recoverable all-value CAS**, not atomic
multi-ref visibility. Native readers ignore lockfiles and can observe one new
ref alongside another old ref. `atomic=True` is refused before writes. The
capability registry keeps `refs.atomic-multi-update` unsupported and separately
reports `refs.coordinated-multi-update`. Local protocol discovery does not
advertise atomic. These APIs do not mutate the worktree/index or append reflogs;
single-ref callers may continue selecting explicit reflog updates separately.

The prepared `ref-transaction-state` journal records exact loose/packed
before/after bytes, selected changes, repository directory identity, transaction
ID, phase and checksum. It is bounded to 48 MiB, with packed snapshots at most
16 MiB and individual loose snapshots at most 4096 bytes. New loose values
publish first, packed entries for selected refs are removed next, then selected
loose deletions publish. Every file is complete when replaced. A committed
marker follows all ref writes; the journal then retires by rename before
recursive cleanup. The directory must be controlled by its owner. Checksums
detect corruption, not malicious rewriting by someone controlling that storage.

`RefTransactionInterrupted.result` records `prepared`, `committed` or `unknown`,
whether the active journal remains, and any retained retired path. A prepared
transaction may have published an intermediate subset; do not assume rollback.
`repository.refs.recover_transaction()` reacquires the same locks, verifies the
journal and current bytes, rolls a prepared transaction back to the exact
original bytes, or completes a committed transaction. Only a committed result
exposes committed `updates`. Retired cleanup failure does not block later
transactions; its exact private path is returned for owner inspection/cleanup.

Recovery refuses unknown later loose/packed bytes, corrupt journals, different
repository identity and active competing locks. It never overwrites such edits
or automatically removes unknown lockfiles. Identical-byte ABA changes cannot
be distinguished by the files backend. Native writers do not honor the recovery
journal: stop writers and establish that the previous owner exited before
recovery. After a process crash, its lockfiles may remain; proving their owner
is dead and reconciling those exact locks is an owner operation, not an automatic
library action. An unrelated native packed-ref rewrite can require manual
reconciliation because recovery preserves the complete original packed snapshot.
New Gitdx ref/index/checkout/commit operations refuse a pending journal. Reads
and immutable object storage remain available for inspection.

The process-interruption contract assumes normal filesystem rename/file-sync
behavior in owner-controlled directories. It is not hostile concurrent path
replacement protection, a backup-restoration identity scheme, or a cross-file
power-loss/atomic-reader guarantee.

`Repository.receive(..., coordinated=True)` selects this backend for a batch.
An initial stale/policy rejection rejects the coordinated batch before object
promotion. Once objects are verified and promoted, receive checks policy under
all ref locks and applies the transaction. Interrupted prepared publication
reports unknown ref outcomes and retains the transaction receipt for recovery;
a committed receipt establishes accepted ref outcomes even when retirement
was interrupted. Default receive continues independent per-ref CAS, allowing
explicit partial acceptance. Both modes report `atomic=False`.
