# Staged commits and history

`repo.commit(author=identity_bytes, committer=identity_bytes, message=bytes,
expected_head=old_oid_or_none, parents=None)` builds byte-preserving trees from
a locked index snapshot, writes the commit, then updates the expected branch or
detached HEAD. None explicitly requires unborn HEAD. Identities include name,
email, nonnegative Unix seconds and numeric timezone, for example
`b"Example <example@example.invalid> 1700000000 +0000"`. Identity, clock,
configuration, environment, hooks and signing helpers never supply defaults.

The default parent is the expected HEAD; explicit parents support merges and
must keep that HEAD first. Unborn branches may select existing commit parents.
Duplicate/excessive parents, unmerged entries, missing/wrong-type staged blobs,
submodules and file/directory collisions are refused. Empty and repeated trees
are permitted; the message is stored exactly. The index and worktree stay intact.
The API requires a worktree index; bare plumbing can use `Commit.create` directly.

Objects exist before expected-old publication. Attached HEAD must point directly
to a branch and is locked/rechecked against checkout races. Stale HEAD/ref writes
leave unreachable immutable objects, never a redirected branch update. Reflogs
are not automatically written by commit; the separate ref API supports explicit
reflog metadata and recovery. This is one-ref publication, not multi-ref atomicity.

`repo.log(start=None, max_count=100)` returns a window of `HistoryEntry(oid,
commit)` records. `walk_history(starts, limits=HistoryLimits(...))` is an iterative
depth-first parent-order walk with deduplication; it is neither date order nor
Git's topological log ordering. Missing/malformed parents in the visited window
fail. Hard budgets default to 100,000 commits, 256 MiB of commit bytes, 64 parents
per commit; staged trees use 100,000 paths/directories and depth 128. Windows stop
at max_count without claiming the rest of the graph was verified.

`branches()` and `tags()` list sorted fully qualified names and resolved IDs,
combining packed refs with loose overrides. Scans are bounded to 10,000 entries
and depth 128; active lock files are omitted. These are local refs only.

CLI `commit` requires `--author`, `--committer`, `--expected-head` (full ID or
`unborn`) and `-m`; optional repeated `--parent` selects full IDs. It UTF-8 encodes
text arguments and adds one message newline. `log --max-count N [--start OID]`,
`branches` and `tags` emit escaped JSON lines. Revision expressions, inferred
identity, amend, hooks, signing and automatic reflogs are separate features.
