# gitdx

A standalone, standard-library Python Git engine with Git-compatible storage and protocols.

Python 3.14+ · MIT

Development starts with the [implementation plan](IMPLEMENTATION_PLAN.md) and
[shared Project](https://github.com/orgs/wispsy/projects/30). The roadmap records
qualification targets; remote exchange and public hosting are later milestones.

Source development does not imply a published package or deployed service.

The initial Python API provides SHA-1/SHA-256 identities and byte-preserving
blob, tree, commit and annotated-tag codecs, without runtime dependencies or
calls to the Git executable:

```python
from gitdx import Blob, ObjectFormat

blob = Blob(b"hello\n").to_raw()
print(blob.oid())                    # SHA-1
print(blob.oid(ObjectFormat.SHA256)) # SHA-256
```

[Object contracts](docs/objects.md) describe byte handling and validation.
[Loose storage](docs/storage.md) adds bounded verified reads and atomic
publish-if-absent writes under an existing owner-controlled objects directory.
[Repository lifecycle and plumbing](docs/repositories.md) provide `init`,
`hash-object` and `cat-file`. Filesystem staging and network operations
remain separate milestones.

[Files refs](docs/refs.md) include packed refs, guarded deletion, explicit reflog
outcomes, validated single-ref compare-and-swap and
explicit unborn, symbolic and detached HEAD handling through `Repository.refs`.

[Index v2 transactions](docs/index.md) expose format-aware staging snapshots
and exclusive, explicitly committed writes through `Repository.index`.

[Indexed pack reading](docs/packs.md) provides bounded SHA-1/SHA-256 base-object
access. Delta resolution, staging/porcelain and network exchange remain queued.

Run `uv sync --locked` and `uv run python -m unittest discover -s tests -v`.
Git is used only as a test oracle; `GITDX_REQUIRE_GIT=1` makes a missing oracle
an error. `uv run python scripts/check_distribution.py` checks isolated wheel
and sdist installations and runtime startup without Git on PATH.
