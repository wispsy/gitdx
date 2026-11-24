# Revision resolution

`repo.resolve(revision)` returns one existing, native-format `ObjectId`. An
`ObjectId` input is also checked for existence and format. No helper, reflog,
replace ref, graft or implicit network fetch is used. Required repository
profiles are checked again on each public operation.

The supported string subset is:

- `HEAD`, a fully qualified portable `refs/...` name or a full hexadecimal OID.
- A portable short ref searched under `refs/`, `refs/tags/`, `refs/heads/`,
  `refs/remotes/` and `refs/remotes/NAME/HEAD`.
- A unique hexadecimal abbreviation of at least four characters, searched in
  loose storage and every validated pack index, including unreachable objects.
- Chained `^`, `^N`, `~` and `~N`: numbered parents and first-parent ancestors.
  `^0` and `~0` peel a commit-ish to its commit. An absent parent fails.
- `^{}` recursively peels tags. `^{commit}`, `^{tree}`, `^{blob}`, `^{tag}` and
  `^{object}` select a type; object validates existence without peeling. Commit
  to tree peeling is supported. Declared tag target types are verified.

Full OIDs take precedence. Short ref ambiguity, object abbreviation ambiguity,
or a short ref/object-prefix collision raises `AmbiguousRevision`, even when
multiple matching ref names point to the same object. This stricter policy
requires callers to select a fully qualified ref rather than relying on Git's
warning/precedence behavior. Missing refs, prefixes, unborn HEAD and absent
parents raise `RevisionNotFound`; missing/corrupt referenced objects retain their
storage errors. Type mismatches raise `InvalidObject`.

Ranges/exclusions, reflog selectors, upstream/push selectors, message searches,
`REV:path`, index selectors, implicit bases, non-HEAD pseudorefs and describe
output are outside this subset. Reserved syntax raises `UnsupportedRevision`
rather than being approximated. Ref names retain the existing portable ASCII
profile. Raw-path lookup is not inferred from string parsing.

`RevisionLimits` bounds syntax bytes, unique objects, aggregate object/index
bytes, traversal steps, parent count, tag depth and storage scan entries.
Repository pack limits also apply. All traversals are iterative, and budget or
corruption failures never become a negative/partial answer. Results describe
observed storage, not a transactionally locked snapshot against concurrent ref
or object changes. Repositories carrying replacement or graft metadata are refused before
history resolution.

```
gitdx -C repository rev-parse 'HEAD~2^{tree}'
gitdx -C repository rev-parse 'refs/tags/release^{}'
```

This selected grammar follows Git's
[revision notation](https://git-scm.com/docs/gitrevisions); the explicit
ambiguity and unsupported-syntax rules above define this implementation's scope.

## Ancestry and merge bases

`repo.is_ancestor(ancestor, descendant)` accepts two supported revision strings
or OIDs and returns a boolean. `repo.merge_bases(left, right)` returns **all** best
common ancestors of exactly two commit-ish roots, sorted by full OID. Annotated
tags peel to commits. Disconnected histories return `False`/`()`, a commit is its
own ancestor/base, and criss-cross histories may have multiple bases. Commit
dates and input order do not select or discard a base. Octopus, fork-point,
reflog/replace semantics and arbitrary single-base selection are not implied.

The complete ancestry reachable from both roots is type/parent/cycle-validated
iteratively within one shared `RevisionLimits` budget before a result is returned.
Missing parents, corruption or exhausted limits raise; they never mean no common
ancestor. Merge-base labels propagate once over a descendant-to-parent
topological traversal, discarding common ancestors dominated by better ones.
This avoids pairwise ancestor queries and recursive traversal. Both queries are
read-only and do not execute Git.

```
gitdx -C repository is-ancestor 'release^{}' HEAD
gitdx -C repository merge-base main topic
```

The predicate CLI uses status 0 for true and 1 for false. `merge-base` always
prints all bases (one full ID per line), with status 0 for a nonempty result and
1 for disconnected roots. Both commands use status 2 for errors. The two-root
definition follows Git's [merge-base contract](https://git-scm.com/docs/git-merge-base).
