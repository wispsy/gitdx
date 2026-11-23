# Staging and status

`repo.add(*paths, force=False)` stages explicit root-relative files/directories
and removals of tracked paths. `repo.remove(*paths)` removes only index entries;
it never deletes working files. Both publish through the existing locked index
transaction. An error preserves the prior index; already written immutable
objects may remain unreachable. No hooks or external commands run.

`repo.status()` returns sorted `StatusEntry(path_bytes, index_code, worktree_code)`
records against HEAD, the index and working files. Tracked changes precede
untracked files, matching porcelain v1 with all untracked files listed. Modes,
binary contents, missing files, staged deletes and merge stages are represented.
This does not create commits or perform rename detection. Assume-valid entries
suppress worktree checks; an explicit add replaces their stat/flag metadata.

The CLI adds `gitdx add PATH... [-f]` and `gitdx status --porcelain [-z]`.
NUL output preserves byte paths. APIs use repository-root paths; CLI paths are
relative to `-C`. Plain paths are supported, not Git pathspec syntax.

Ignore precedence is info/exclude, root .gitignore, then nested .gitignore files.
Rules support comments, escaped leading markers/spaces, negation, anchors,
directory patterns, wildcard components and component `**`. An excluded parent
must be re-included before its child can be re-included. Tracked entries are
still updated even beneath ignored directories. Global excludes are not loaded.

The portable mutation profile refuses traversal, metadata aliases, reserved
device/Windows characters, trailing dots/spaces, case/normalization collisions,
symlink parents, nested repositories/submodules and configured filters or
attribute/line-ending transformations. Symlink values themselves are stored as
link-text blobs without following targets. Filemode and symlink configuration
are honored. Paths outside this profile remain readable as raw tree/index bytes
but cannot be staged by this API.

Default limits cover 100,000 visited paths, 64 MiB per file and 256 MiB total
content; ignore files are bounded and at most 10,000 rules are loaded. Ordinary
concurrent file changes are detected around reads. These checks assume
owner-controlled directories and do not establish hostile filesystem isolation.
