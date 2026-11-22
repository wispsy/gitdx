# Loose refs and HEAD

```python
from gitdx import Commit, Repository, Tree

repo = Repository.init("example")
tree = repo.objects.write(Tree(format=repo.object_format).to_raw())
identity = b"Example <example@example.invalid> 1700000000 +0000"
oid = repo.objects.write(Commit.create(tree, author=identity, committer=identity,
                                      message=b"Initial\n").to_raw())
repo.refs.update("refs/heads/main", oid, expected=None) # create-only
assert repo.refs.resolve().oid == oid
```

`read(name)` returns a `DirectRef`, `SymbolicRef` or None. `resolve(name="HEAD")`
follows at most 15 symbolic links and returns the terminal name, optional ID
and symbolic chain. Unborn and detached HEAD have explicit properties. A missing
target can be represented; resolution does not assert graph connectivity.

`update(name, new, expected=old)` updates direct refs only. None is a create-only
expectation, never an unconditional overwrite. Symbolic refs cannot be detached
implicitly: use `detach_head(new, expected=<exact RefValue>)` or
`set_symbolic(name, target, expected=<exact RefValue or None>)`. Branch/detached
targets must be existing structurally valid commits. This does not yet verify
their entire reachable graph, ancestry, fast-forward policy or signatures.

Names use fully qualified `refs/` paths, plus the explicit HEAD pseudo-ref.
The initial portable subset accepts ASCII, applies Git's forbidden sequences
and additionally rejects Windows-invalid/device names and case-fold aliases.
Non-ASCII refs and platform-specific variants need later compatibility work.
Static symlink paths and existing lockfiles are refused. Owned .lock files are
flushed and atomically replaced only after the expected value matches; failed
writes clean only their own locks. Calls update one ref, not an atomic set.

The backend refuses repositories with packed-refs, reftable, shared worktree
metadata or changed object format. It does not implement reflog writes, deletion,
multi-ref transactions, concurrent native pack-refs or full crash durability yet.
Ref writes do not currently implement core.logAllRefUpdates. All operations
assume owner-controlled directories; hosting hardening remains separately gated.
