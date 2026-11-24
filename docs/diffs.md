# Byte and tree diffs

`diff_bytes(old, new, context=3)` returns immutable binary/hunk records. Inputs
are bytes. LF alone separates lines: CRLF, invalid UTF-8, control bytes and a
missing final LF are preserved. A NUL anywhere selects binary output with no
text hunks. No encoding, whitespace, textconv, filter or attribute conversion is
applied. Hunk alignment uses deterministic `SequenceMatcher(autojunk=False)`;
ambiguous alignments need not match Git's selected diff algorithm.

`repo.diff(old_tree, new_tree, ...)` compares explicit, format-matching tree IDs;
`None` selects an empty snapshot. It reads object storage without consulting or
modifying the index/worktree. Results sort by destination byte path (source for
deletions), then source. Paths, modes, OIDs and complete old/new blob bytes stay
separate. Status is A/D/M/T or R; mode-only changes remain visible. Symlinks are
blob data; gitlinks compare opaque commit IDs without opening a submodule and
produce binary summaries. Missing/corrupt/type-mismatched objects fail explicitly.

Renames are off by default. `detect_renames=True, rename_threshold=50` enables a
threshold from 1 to 100. Same-kind identical OIDs score 100, including binary
files. Modified binary/gitlink candidates are not matched. Other scores are
`floor(100 * shared LF-line bytes / max(old_bytes, new_bytes))`, using multiset
line overlap. Candidates sort by decreasing score, source path, destination
path; each source/destination is selected once. This is a documented heuristic,
not a promise to reproduce Git's similarity scores, copy detection or rewrites.

`DiffLimits` bounds paths/depth, aggregate tree/blob bytes, lines per blob,
aggregate line-comparison work, rename candidate pairs, hunks and patch bytes.
Exhaustion raises `ObjectLimitExceeded`; detection never silently turns off.
The line comparison cost is charged conservatively before running alignment.
Large comparisons may require caller-selected larger budgets.

`FileDiff.patch()` returns raw Git-style patch bytes with full OIDs, C-quoted
paths, mode/rename headers and no-final-newline markers. Binary records are
summaries, not reversible binary patches. Text patch bytes may contain controls;
`render_patch(bytes)` escapes controls/non-ASCII for terminal display. Neither
presentation changes repository content. Patch application is separately scoped.

```
gitdx -C repository diff OLD_TREE_ID NEW_TREE_ID
gitdx -C repository diff empty NEW_TREE_ID --renames 100 --patch
```

Default CLI output is JSON lines with hex byte paths and hunk contents. `--patch`
prints the escaped presentation; use the Python byte API for raw patch export.
These contracts follow the distinctions in Git's
[diff format documentation](https://git-scm.com/docs/diff-format).
