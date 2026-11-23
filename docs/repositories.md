# Repository lifecycle and plumbing

```python
from gitdx import Blob, ObjectFormat, Repository

repo = Repository.init("example", object_format=ObjectFormat.SHA256)
oid = repo.objects.write(Blob(b"hello\n").to_raw())
assert Repository.open("example").objects.read(oid).data == b"hello\n"
```

`init(path, bare=False, object_format=SHA1, initial_branch="main")` creates new
metadata and an unborn symbolic HEAD. It never replaces or reinitializes existing
metadata. Non-bare initialization preserves existing user files; bare
initialization requires an absent/empty directory. Initialization assumes
owner-controlled paths; concurrent initialization and power-loss recovery are
not qualified yet.

`open(path)` selects exactly that working root or metadata/bare directory.
`discover(start)` explicitly searches parents and stops on unsupported metadata
rather than silently using an outer repository. Gitfiles, linked worktrees,
alternates, shallow repositories, custom worktrees and sparse checkout are
currently refused. Object lookup is loose-only; an existing repository with
packs may open but missing loose objects report the packed-object limitation.

Repository format versions 0 and 1 are recognized. Only native SHA-1/SHA-256
and files refs are selected; reftable and all other required extensions fail
before operations. The local UTF-8 config subset supports case-insensitive
section/variable names, quoted subsection names, unquoted values, inline comments,
bare booleans and repeated last-value variables. Includes and quoted, escaped or
continued values are refused. Global/system/environment config is not loaded.
The library never executes hooks, filters, helpers or signing programs.

The CLI operates relative to `-C DIRECTORY`:

```text
gitdx init example --object-format sha256
gitdx -C example hash-object --stdin -w
gitdx -C example cat-file <full-object-id>
gitdx -C example cat-file --type <full-object-id>
gitdx -C example cat-file --size <full-object-id>
```

Without `-w`, hash-object can run outside a repository and defaults to SHA-1;
`--object-format` selects a format explicitly. Within a repository it defaults
to that repository's format. Inputs use the default object byte budget. Non-blob
payloads are structurally validated. Cat-file writes raw payload bytes, including
raw binary tree entries; it does not provide Git's pretty-print porcelain yet.
Expected failures return status 1 and a concise error; argparse usage errors
return status 2. These commands run without Git installed.
Object-ID/type/size output uses LF consistently on every platform; payload
output preserves bytes exactly.
