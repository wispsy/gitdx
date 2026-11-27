# Read-only upload-pack sessions

Current source adds `UploadPack(repository, limits=..., cancel=...)` for one
already authorized stateless request. `advertise(version, http=True)` produces
smart-HTTP discovery; `respond(body, version)` handles upload-pack. The hosting
application owns catalog lookup, authentication, HTTP headers and concurrency.
The engine imports no web framework and never executes Git or configured hooks.
The published 0.1.0a1 preview predates this API.

Protocols 0, 1 and 2 support same-format clone/fetch, branch and tag discovery,
peeled annotated tags, symbolic/detached/unborn HEAD, v2 `ls-refs` prefixes and
nonterminal have rounds. Wants must be current advertised branch/tag/HEAD or
peeled tag tips; unreferenced or arbitrary reachable-object wants are refused.
Explicit object-format mismatches and wrong-width IDs fail. Native legacy
clients may omit the redundant object-format echo after discovery.

Common haves are acknowledged within the requested graph. Packs contain complete
non-thin closures, including applicable `include-tag` targets, without exclusion
or delta optimization. This is compatible negotiation, with more transferred
bytes than an incremental pack. Shallow/filter/deepen/ref-in-want requests,
server options and other unadvertised features refuse. No receive-pack service
or network authentication claim follows from this interface.

`UploadPackLimits` bounds framing (default 1 MiB/16,000 request packets), refs
(10,000), wants (256), haves (4096), prefixes (128), tag peeling (32), graph work
and response bytes (64 MiB). Graph defaults permit 100,000 objects, 1,000,000
edges, 16 MiB per object, 128 MiB total object bytes and a 60 MiB complete pack.
The host should select smaller budgets where appropriate. A shared 30-second
cooperative deadline/cancellation check covers discovery, graph reads, pack
generation and `chunks()` iteration. It cannot interrupt a blocking kernel I/O
or one bounded object compression call.

The response is prepared in bounded memory before transmission; `chunks()`
exposes bounded output pieces for an HTTP streaming response. It is not an
unbounded streaming pack writer. The host must separately bound concurrent
preparations and cancel workers when clients disconnect. Storage follows the
trusted local repository contract; read-only serving of an enrolled SHA-1
repository does not permit untrusted SHA-1 ingestion.

Source/wheel/sdist qualification compares stock Git clone/fetch, branches/tags,
empty repositories and all three protocols in both hashes. A separate empty-PATH
worker generates and verifies the same complete pack responses. Gittycat CAT-07
owns the Expose API adapter and private-resource concealment. Deployment remains
separate.
