# Isolated graph translation

`GraphTranslator(source, target).translate(roots)` translates an explicitly
selected complete unsigned graph between SHA-1 and SHA-256. Both repositories
must already exist in separate, non-nested owner-controlled directories. The
target must be bare with files refs, an unborn HEAD and no published refs.
Translation never updates source objects or either repository's refs.

The bounded closure is validated before target writes. Iterative dependency
ordering supports deep histories and shared objects. Blobs, tree names/modes,
header ordering, unknown headers, continuation lines, encodings, identities and
messages retain their raw bytes; only typed object references change. Gitlinks,
mergetags, signature headers and recognized signature-bearing messages refuse
before writing. There is no implicit signature stripping or submodule policy.

The target's version-1 `translation.sqlite3` cache records the exact source and
target directory identities and object formats. Each run re-derives mappings
from source objects and verifies target objects. Missing target objects can be
recreated; conflicting mappings refuse. SQLite commits mappings together, with
nonblocking writer contention. Interrupted runs may leave valid unreachable
objects; restarting is idempotent. The cache never replaces object authority.
`PackWriteLimits` bounds roots, objects, graph edges and bytes; `check` provides
cooperative cancellation. This experimental local API neither migrates a
repository nor enables cross-format fetch/push. Those policies remain separate.
