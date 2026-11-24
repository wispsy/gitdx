# Same-format transfers

Incoming objects pass through a private `ObjectQuarantine` before live refs can
change. `unpack_objects` reads complete unindexed pack v2/v3 bytes, verifies the
native SHA-1/SHA-256 trailer, object framing/counts, declared/inflated sizes and
object codecs. OFS and forward REF deltas resolve under shared byte/operation/
depth limits. Thin packs and missing/cyclic bases are refused; no external
program or implicit object source resolves them.

`receive(pack)` writes only below an owned `objects/incoming-*` directory.
`validate(roots)` traverses the selected typed commit/tree/tag closure through
quarantine plus existing verified storage. Missing/wrong-type edges fail before
publication; gitlinks remain external-repository boundaries. `promote()` requires
successful validation and publishes immutable loose objects before any ref may
name them. Interrupted promotion may leave verified, unreachable objects.
Context exit removes the private directory on normal failures; process termination
or cleanup failure may leave an unreferenced directory for later inspection.

Limits bound pack/object sizes, counts, expanded bytes, delta work and graph
edges. A caller check callback runs between bounded parsing/graph/publication
steps for cancellation/deadlines. This owner-controlled filesystem profile does
not promise atomic multi-ref visibility, power-loss durability or full graph
fsck/maintenance qualification.

## Local transport and public API

```python
from gitdx import LocalTransport, Repository, TransferLimits

source = LocalTransport("/owned/source.git")
repo = Repository.clone(source, "/owned/new-checkout")
result = repo.fetch(source, remote="origin", branches=("main",))
assert result.complete
```

Transport selection is explicit; paths/URLs/config names never choose commands
or adapters implicitly. `LocalTransport` reads a repository through Gitdx and
emits a complete non-thin pack without invoking Git. Fetch requires matching
formats, refuses shallow sources and validates every selected graph before refs.
Branches map to `refs/remotes/<remote>/...`; `tags=True` also selects tags.
Unselected refs are untouched, deleted remote refs are not pruned, local branches
and working content do not move, and rewinds require `allow_rewind=True`.
Existing tags cannot be replaced by this option. Ref names must fit the portable
mutation profile; arbitrary refspec syntax is not interpreted.

Each destination captures an expected-old value before receiving data. Objects
publish first, then refs use independent CAS outcomes. `FetchResult.updated` and
`.rejected` distinguish success from stale/locked/non-fast-forward refs. Unexpected
publication failure or cancellation raises `FetchInterrupted` with known completed
updates; inspect refs before retrying. Multiple refs are not atomic and an I/O
failure can require inspection of the current ref as well as completed outcomes.

Clone discovers/negotiates before reserving a new destination; existing paths are
never reused. It follows advertised HEAD or an explicit branch, supports bare,
detached, empty/unborn and `checkout=False` destinations, and does not guess a
missing remote HEAD. Worktree checkout uses the safe plan/apply path. Network/
format failure before reservation leaves no destination. Later failures retain
the newly owned repository for inspection and raise `CloneIncomplete.path`;
checkout recovery may be required after an interrupted materialization. No
remote URL, credentials or implicit fetch configuration is persisted.

`TransferLimits` bounds refs, wire bytes, packs and total time. `cancel=` accepts
an explicitly supplied event with `is_set()`. Checks occur between bounded local
object/ref/graph steps. Native Git fixtures qualify clone/fetch in both directions
for both algorithms, including packed source repositories and no-Git runtime
execution. Server-side upload/receive protocol handlers are separate tasks.

## Smart HTTP

`SmartHTTPTransport(url, authorization=HTTPAuthorization.basic(user, password))`
selects direct HTTPS. System certificate and hostname verification are required;
an explicitly supplied verifying `SSLContext` can add a private CA. Cleartext
requires `allow_http=True`. Basic/Bearer values are caller supplied and omitted
from representations. No ambient proxy, netrc, credential helper, askpass,
authentication retry or cookie store is used. Redirects are always refused,
including same-origin redirects, and credentials never follow peer-provided URLs.
Userinfo, query/fragment, traversing and ambiguous encoded URL paths are refused.

Discovery defaults to v2 `ls-refs` and falls back to a server's legacy discovery.
`protocol_version=0` or `1` explicitly selects legacy negotiation. Requests ask
for complete non-thin packs without haves, shallow depth, filters, server options,
packfile URIs or external bases; repeated fetch is correct but not incremental
on the wire. v2 `packfile` and legacy sideband/raw-pack boundaries are validated.
Unsupported sections, shallow sources, peer errors, wrong content types,
compression, truncated or ambiguous HTTP framing fail explicitly. Object format
is established from capabilities, never ID width; legacy empty peers that omit
it establish only SHA-1. v2 qualifies native empty/unborn SHA-256 discovery.

Request/body budgets share the operation deadline. A socket monitor interrupts
stalled headers, slow-drip bodies and TLS on cancellation/deadline; bounded idle
timeouts also apply. DNS runs in a read-only daemon lookup with bounded caller
waiting; an underlying cancelled OS resolver can finish later without publishing
anything. No background worker may mutate the repository. Objects/refs run only
on the calling thread after validated responses. Cancellation during independent
ref publication reports the completed outcomes described above.

Native Git `http-backend` is a development-only loopback fixture for v0/v1/v2,
SHA-1/SHA-256, auth, packed transfers and reverse client reads. Optional OpenSSL
development fixtures generate ephemeral TLS keys to verify a trusted CA and
untrusted-certificate refusal; keys are never committed. These tests do not
implement or deploy a hosting server.

## CLI

```
gitdx clone /owned/source.git /owned/new --transport local
gitdx -C /owned/new fetch https://host/repo.git --transport http --branch main
```

Clone supports `--bare`, `--branch`, `--no-checkout` and an expected
`--object-format`. Fetch supports `--remote`, repeated `--branch`, `--tags` and
explicit `--allow-rewind`. Both require a transport and accept a bounded
`--timeout`. HTTP adds `--protocol-version`, `--allow-http` and optional
`--authorization-env NAME` to read exactly one explicitly named environment
variable containing a Basic/Bearer header. No secret value goes into arguments,
output or repository configuration. Fetch rejection returns status 2; other
transfer failures return 1 and leave the documented inspection/recovery state.
