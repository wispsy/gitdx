# Configuration and repository policy

`parse_values(bytes)` returns ordered `ConfigEntry` records. Section and variable
names are case insensitive; quoted subsection names retain case. Deprecated
`[section.subsection]` syntax lowercases its subsection. `get_all` preserves
multivars and `get` returns the last value. A bare variable has value `None`
(boolean true), while `key =` is an empty value (boolean false).

The UTF-8 profile accepts a BOM, CRLF, inline comments outside double quotes,
quoted whitespace, escaped quotes/backslashes, `\n`, `\t`, `\b`, and backslash
line continuations. Unsupported escapes, unterminated quotes, NUL, more than
10,000 records or more than 1 MiB fail explicitly. Representations omit values,
which can contain caller credentials. No value executes a command.

`parse_config` validates repository format and required extensions. Repository
operations read only `.git/config`: system/global config, environment overrides,
include files, hooks, aliases, filters, credential helpers and signing programs
are never selected implicitly. Unsupported required extensions, sparse/custom
worktrees and includes are refused before repository mutation.

For separate inspection, `read_config(path, root=..., includes=True)` explicitly
selects relative includes beneath an owned directory. It rejects parent/home/
absolute paths, symlinks/reparse points, cycles and conditional includes. Reads
share a 1 MiB/10,000-record budget and a bounded depth. Included entries appear
at the inclusion point. Missing selected files fail; they are not silently
ignored. This reader does not change repository-operation policy or import
personal config.

The syntax and oracle fixtures follow the [Git configuration specification](https://git-scm.com/docs/git-config#_syntax).

## Staging, status and checkout

Working-tree operations use parsed last-value configuration, so quoting,
continuation or repeated `core.autocrlf` settings cannot bypass conversion
checks. Enabled/input autocrlf, explicit eol/encoding policy, filter drivers and
external attribute files are refused. Helper, alias, hook, fsmonitor, signing
and credential command settings never execute.

An explicit local `core.excludesFile` selects a file relative to the worktree
root, with no absolute/home/parent paths or filesystem redirects. Empty disables
it; a missing file supplies no patterns. The precedence is this file, then
`.git/info/exclude`, then root/deeper `.gitignore` files. Last matching rules
win within each level, ignored parents cannot be re-included by child rules,
and tracked paths stay tracked. BOM, escaped markers/spaces, directory patterns,
basic glob classes and globstars are supported; POSIX and caret-negated classes
are explicitly refused. System/global excludes are not read.

`.gitattributes` (including target checkout blobs) and `.git/info/attributes`
are checked before byte-changing operations. Rules may disable conversion with
`-text`, `-crlf`, `-ident`, `-filter`, `-eol`, `-working-tree-encoding`, or the
built-in `binary` macro. Basic diff/merge/delta flags and export-ignore metadata
are passive during these local operations. Every other assignment, custom macro,
quoted/escaped pattern or conversion rule is refused, even if no current path
matches it. Attribute matching, text conversion and external drivers are not
approximated. Selected bytes remain exact, including CRLF and identifier text.

Oracle tests compare [ignore precedence](https://git-scm.com/docs/gitignore)
and [attribute states](https://git-scm.com/docs/gitattributes) with native Git.
