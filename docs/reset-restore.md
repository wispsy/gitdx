# Explicit reset and restore plans

Current source separates ref, index and working-tree effects. Planning writes
nothing; apply rechecks the immutable preview under the shared operation locks.
The APIs are source development beyond the initial PyPI preview.

## Reset

```python
before = repo.refs.resolve().oid
plan = repo.plan_reset("HEAD~1", mode="mixed", expected_head=before)
print(plan.ref_name, plan.expected_head, plan.target)
print(plan.index_changes, plan.worktree_changes)
repo.apply_reset(plan)
```

| Mode | Selected ref | Index | Worktree |
| --- | --- | --- | --- |
| `soft` | Compare-and-swap to the target commit | Preserved, including bytes and file identity | Preserved |
| `mixed` | Compare-and-swap | Target tree snapshot | Preserved |
| `hard` | Compare-and-swap | Target tree snapshot | Target tree snapshot |

`mode` and `expected_head` are required. Mixed reset rejects staged changes
unless `discard_staged=True` is selected. Hard reset rejects staged/working
changes unless `overwrite_tracked=True` is selected. These options apply only
to their stated modes. Untracked/ignored content that would be overwritten
remains protected even with tracked overwrite. `ORIG_HEAD` records the previous
tip; the symbolic branch remains selected. Detached HEAD is also supported.

No implicit branch selection, bare reset, unmerged indexes, active merge
abandonment, `--merge`, `--keep`, pathspec reset or submodule recursion is implied.
Finish or explicitly abort a journaled merge before resetting.

## Restore

```python
# Restore working bytes from the index; confirm discarding local edits:
plan = repo.plan_restore("file", overwrite_tracked=True)
print(plan.index_changes, plan.worktree_changes)
repo.apply_restore(plan)

# Restore selected staging from HEAD, preserving working bytes:
plan = repo.plan_restore("dir", staged=True, worktree=False, discard_staged=True)
repo.apply_restore(plan)

# Select another commit for both effects:
plan = repo.plan_restore(
    "file", "dir", source="other-branch",
    staged=True, worktree=True, discard_staged=True, overwrite_tracked=True,
)
repo.apply_restore(plan)
```

Paths are explicit literal files/directory prefixes; `.` selects the root.
There is no glob/pathspec language. Worktree-only restoration defaults to the
index; staging restoration defaults to HEAD. An explicit `source` selects a
commit snapshot. Refs never move during restore.

Index and worktree changes are previewed separately. Dropping staged changes
requires `discard_staged`; dropping unstaged selected working changes requires
`overwrite_tracked`. Unselected index entries/stat data, working edits and
untracked files remain independent. Index changes invalidate advisory cached
extensions; soft reset and worktree-only restore retain original index bytes.
File/directory collisions or untracked obstructions refuse the plan rather
than deleting content. Unmerged/submodule indexes require another profile.

## Publication and recovery

Reset and restore share merge/checkout's checksummed path payloads and guarded
installation helpers. The journal records index snapshots, selected refs,
configuration, packed refs and operation metadata. Objects are validated before
publication. A late edit, different expected ref or changed index invalidates
the plan before mutation. Recovery refuses unknown state rather than replacing
unrecognized content.

```python
result = repo.recover_operation()
```

Without a committed marker, recovery restores the saved state. With the marker,
it verifies completion and retires the journal. This includes interruption after
ref publication. Pending operation journals block other library mutations;
stale native lockfiles require their owner to resolve them. No atomic visibility
of the complete ref/index/worktree set, hostile filesystem replacement or
power-loss durability is promised.

Native Git comparisons cover all three reset modes and the three index/worktree
restore selections in both object formats. Installed source/wheel/sdist evidence
adds `worktree.reset-restore` with native, fault and no-Git checks.
