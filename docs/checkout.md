# Portable checkout planning

`repo.plan_checkout("branch")` returns an immutable `CheckoutPlan` with target,
captured HEAD/ref/index inputs and ordered `CheckoutChange` records. Each change
contains a byte path, old/new mode and content IDs; payloads and config are omitted
from its representation. Planning reads and validates the complete selected tree
and working inputs without publishing files, index entries or refs.

Pass a full `ObjectId` for detached checkout. `unborn=True` explicitly selects a
missing branch with an empty target; a missing branch is otherwise an error.
Branch refs themselves are not created/moved by a switch. A symbolic HEAD must
point directly to a local branch. Existing merge/rebase/sequencer state and
unmerged/submodule indexes are refused.

The default refuses staged or modified tracked content, even on paths the target
would leave unchanged. `overwrite_tracked=True` explicitly permits discarding
tracked changes. Untracked content, including ignored files and empty untracked
directories that block a file, is protected in both modes. There is no option to
discard untracked data. Clean directory/file transitions are planned explicitly.

Every target component and existing affected namespace uses the portable path
profile: no traversal, metadata aliases, Windows devices/streams, trailing dots
or spaces, case/NFC aliases, compatibility metadata variants or HFS-ignorable
characters. Symlink values are never followed; a symlink parent cannot redirect
a checkout. The metadata protections account for the categories described in
Git's [path checks](https://github.com/git/git/blob/master/path.c) and
[HFS checks](https://github.com/git/git/blob/master/utf8.c).

External filters, active attributes and line-ending transformations remain
unsupported. POSIX link values are bounded to 1,023 bytes; Windows requires
`core.symlinks=false` to represent a committed link as a regular link-text file.
New files/directories use owner-only permissions; existing regular permissions
are retained without special bits, with the user executable bit adjusted when
filemode applies. The rebuilt v2 index retains target Git modes and drops advisory
cache extensions/assume-valid flags.

Default limits are 100,000 tree/working paths, depth 128, 64 MiB per file and
256 MiB combined planning content. Checks assume owner-controlled directories;
they are not a sandbox against hostile concurrent path replacement. Applying a
reviewed plan and interrupted-write recovery are separate explicit operations.

## Application and recovery

`repo.apply_checkout(plan)` revalidates the entire plan under index, HEAD,
packed-ref and source/target branch locks. A stale or modified plan is refused.
Before changing working files it publishes a private `.git/checkout-state`
journal containing verified old/new payloads, the old/new index, explicit HEAD
states and a config digest. Working changes use atomic leaf replacements; index
publication precedes HEAD publication. Source/destination branches never move.
Checkout does not infer identity or write reflogs.

An interrupted application raises `CheckoutRecoveryRequired` and retains the
journal. Further Gitdx index/ref mutations are blocked. After reopening the
repository, explicitly call `repo.recover_checkout()`: an unfinished application
rolls back to its captured files/index/HEAD; a journal already marked committed
is validated and finalized. Recovery can itself be retried after interruption.
It refuses outside edits, moved refs, changed config/index, unexpected untracked
children or invalid/tampered journal data rather than overwriting them. Move or
preserve that outside work and inspect the retained state before retrying.

`CheckoutResult` reports target, HEAD, whether recovery rolled back, and whether
cleanup remains. A completed journal is atomically retired before deletion;
cleanup failures may leave an owned `checkout-complete-*` directory. Preparation
or crashes inside a temporary write may leave owned temporary artifacts for
inspection. No multi-file atomic view, hostile-filesystem protection or power-loss
durability is claimed. Do not run native Git mutations during pending recovery;
native Git does not honor this journal marker. Stale process-crash lock files
require owner inspection before recovery. Backups must be protected as private
repository data. Journal metadata is capped at 32 MiB and payloads at 512 MiB.

Windows requires `core.filemode=false` and writable affected paths; native links
require the link-text profile. Windows permission reporting is normalized to
writable regular/directory attributes, while the index retains Git's mode.

CLI `gitdx checkout BRANCH --dry-run` prints paths as hex plus mode/content-ID
changes. Omit `--dry-run` to apply; `--detach` selects a full commit ID,
`--unborn` selects an absent branch, and `--overwrite-tracked` explicitly discards
tracked changes. `gitdx recover-checkout` performs guarded recovery. The Python
API can retain and apply the exact reviewed immutable plan; CLI application
always creates and validates a fresh plan.
