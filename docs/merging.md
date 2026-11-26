# Two-parent merge profile

Current source provides immutable, bounded merge plans and recoverable local
application. This profile corresponds to an explicit `git merge --no-ff
--no-commit`: applying a clean merge prepares the index and worktree, and a
separate call publishes the two-parent commit. An already-merged head is a no-op.
These APIs are source development beyond the initial PyPI preview.

```python
plan = repo.plan_merge("feature")
for change in plan.changes:
    print(change.path, change.before_oid, change.after_oid)
for conflict in plan.conflicts:
    print(conflict.path, conflict.kind, conflict.stages)
result = repo.apply_merge(plan)

# Resolve conflicts deliberately, then stage each chosen file/removal:
# repo.add("file") or repo.remove("file")
oid = repo.finish_merge(
    author=b"Name <name@example.invalid> 1700000000 +0000",
    committer=b"Name <name@example.invalid> 1700000000 +0000",
    message=b"Merge feature\n",
    expected_head=plan.head,
    expected_other=plan.other,
    resolved=True,
)
```

Planning writes no objects, refs, index or working files. Apply rechecks the
reviewed inputs while holding index/HEAD/branch/packed-ref locks, publishes only
immutable generated blobs, then records a private operation journal before
changing paths. HEAD retains its symbolic or detached identity during resolution.
`MERGE_HEAD`, `MERGE_MSG` and `ORIG_HEAD` identify the selected operation. A
normal `repo.commit` refuses active merge state; completion requires both
expected commit IDs, explicit resolution confirmation and an entirely stage-0
index. Unstaged edits remain independent of the committed index.

## Supported merge results

Trivial equality and one-sided additions, edits, deletions and mode changes
collapse to stage 0. Regular byte strings without NULs support deterministic LF
line merging when both sides' edits are separate; touching edits conservatively
conflict. CR bytes and missing final newlines remain bytes. Comparison work,
line count, files, aggregate bytes, tree depth and paths have explicit limits.

Content, binary, add/add, modify/delete, type/mode and directory/file conflicts
retain the available base/ours/theirs entries as stages 1/2/3. Text conflicts use
whole-file `HEAD`/`MERGE_HEAD` markers. Binary/type conflicts keep ours (or the
remaining side after a deletion); directory/file conflicts keep ours' topology.
Every original conflict path is journaled even when its working bytes did not
change, so abort can restore a later manual resolution. Native Git can inspect
the index stages and stage a resolution; use the API to complete its journaled
merge rather than running a separate native commit during the operation.

A unique best common ancestor is selected automatically. With multiple best
bases, pass an explicit `base` ObjectId returned by `repo.merge_bases`; no
recursive virtual base is fabricated. Unrelated histories, automatic rename
heuristics, octopus merges and submodule merges remain outside this profile.
External hooks, drivers, filters and helpers never execute. Existing conservative
configuration/attribute policy still applies.

## Abort and process-interruption recovery

```python
abort = repo.plan_abort_merge()  # unchanged prepared merge
print(abort.changes)
repo.abort_merge(abort)

# If deliberately discarding edited/staged merge resolutions:
abort = repo.plan_abort_merge(discard_tracked=True)
repo.abort_merge(abort)

# After an interrupted begin, abort or finish:
receipt = repo.recover_merge()
```

An abort plan protects staging outside the merge and preserves unaffected
working edits/untracked files. A later edit invalidates the reviewed plan.
`operation-state` blocks other library ref/index/checkout mutations while
recovery is pending. Payload checksums, strict paths, saved config, selected
heads, packed refs and recognized before/after values guard recovery. Unknown
edits, corrupt journals and mismatched refs refuse recovery and retain evidence.

An interrupted operation without its committed marker rolls back; a committed
marker completes cleanup. Begin retains its original snapshot in `merge-state`
until explicit finish/abort. Failures after publication can therefore require
inspection/recovery before the caller knows the final outcome. Native stale
lockfiles still require their owner to resolve them; recovery never deletes an
unknown writer's lock. Matching source/runtime code must be used for recovery.
This is an owner-controlled local repository profile, not hostile filesystem
replacement or power-loss durability qualification.

The source/wheel/sdist qualifier runs native tree/index comparisons for SHA-1
and SHA-256, conflict and fault fixtures, and a separate merge worker without
Git on PATH. Evidence selects `worktree.merge-two-parent` only with its four
matching checks. Index-stage and parent semantics follow the primary
[read-tree](https://git-scm.com/docs/git-read-tree) and
[merge](https://git-scm.com/docs/git-merge) contracts; the finite text alignment
profile does not promise every native heuristic.
