# Packet framing

`Packet(data=bytes)` is a binary data frame; `FLUSH`, `DELIMITER` and
`RESPONSE_END` are distinct immutable controls. `encode_packet()` emits lowercase
hexadecimal lengths. `PacketDecoder.feed(bytes)` accepts arbitrary chunk splits
and returns complete packets; `finish()` detects truncated input. Empty data
(`0004`) stays distinct from flush (`0000`). The maximum frame is 65,520 bytes,
including its four-byte header. Malformed/reserved/oversized frames consume the
decoder; it cannot be resumed after an error or finish.

`ProtocolLimits` defaults to 100,000 packets, 64 MiB wire bytes, a 30-second total
deadline and 1,024 error bytes. A decoder retains at most one partial frame.
Incremental callers own the blocking I/O and must call feed/finish to observe a
deadline/cancellation; the decoder never starts a transport or subprocess.
`ProtocolBudget` can share a caller-selected monotonic clock/cancellation check.

`read_packets(reader, cancel=asyncio.Event())` and `write_packets(writer, packets)`
adapt nonblocking async read(n)/write/drain streams. Reads request only the next
frame's needed bytes, so stopping at a control boundary leaves following raw
pack data in the reader. Timeout and cancellation interrupt stalled awaits and
clean up owned helper tasks. Native task cancellation propagates. Deadlines cover
the complete iterator/operation lifetime, including caller pauses. Writers may
have emitted a prefix when cancellation/error occurs; framing is not a transport
transaction. Stream readers must honor read(n)'s maximum and return bytes.

`decode_sideband()` separates channels 1 (data), 2 (progress) and 3 (error).
`encode_sideband()` applies the same packet/error bounds. Peer payloads stay as
bytes and are omitted from representations. `raise_if_error()` raises a fixed
`RemoteProtocolError` message rather than copying untrusted text into logs or
terminals. No peer progress/error text is printed automatically.

Vectors and limits follow Git's [common protocol](https://git-scm.com/docs/gitprotocol-common)
and [v2 control boundaries](https://git-scm.com/docs/gitprotocol-v2). Ref/capability
advertisement and negotiation are a separate layer; no network, fetch/clone,
push or cross-format transfer is implied by framing support.

## Discovery and negotiation

`parse_advertisement(packets)` and `encode_advertisement(Advertisement(...))`
handle v0/v1 ref/capability records, peeled tags, symbolic targets, empty refs,
shallow metadata and v2 capability discovery. Wire names remain bytes and are
validated as protocol refs, not converted into filesystem paths. Unknown advertised
capabilities/features remain metadata; they are never selected automatically or
inferred from the informative agent string. Counts default to 100,000 refs and
256 capabilities; names/values are bounded to 4 KiB plus the aggregate wire budget.

`negotiate(advertisement, local_format, capabilities=..., command=...)` selects
only explicitly requested advertised capabilities and a supported v2 fetch or
ls-refs command. Mutually exclusive legacy sideband/ACK modes are refused.
`object-format` must match local native storage. Absence means SHA-1; ID widths
never select an algorithm and advertisements never authorize translation. A
capability-less empty legacy advertisement cannot establish SHA-256 support.
Matching advertised formats are echoed explicitly when building requests.
`Negotiated.sideband_packet_size` reports 1,000/65,520-byte framing or None for
unmultiplexed legacy data; callers select the corresponding stream limits.

`encode_legacy_request`/`parse_legacy_request` cover basic advertised wants,
haves, done and flush round boundaries; `parse_ack` covers NAK and ACK status
records. Legacy wants must have been advertised. Advanced shallow/deepen/filter
request semantics are explicitly refused by this basic request parser.

`encode_v2_command`/`parse_v2_command` require one command header, capability
section, delimiter, argument section and final flush. Empty requests are explicit.
Fetch want/have IDs are validated against the negotiated algorithm. Other v2
arguments remain bounded opaque metadata for future command handlers to validate;
parsing does not implement their feature semantics or execute a fetch.
`parse_ls_refs` handles peeled/symbolic/unborn records and optional stateless
response-end controls. Unknown ref attributes are ignored.

Peer ERR and sideband errors use fixed redacted exceptions. Transcript helpers
apply packet/byte/ref/capability limits; stream adapters own I/O deadlines and
cancellation. A transport/application must authorize discovery/object access,
select supported features and own connection cleanup. No sockets, credentials,
remote URLs, pack ingestion, ref writes or hooks are managed by these functions.

Tests capture stock Git upload-pack v0/v1/v2 advertisements and v2 ls-refs replies
for both algorithms, including unborn repositories, and check a local file
transport's packet trace. Request boundaries and feature profiles follow
[Git pack negotiation](https://git-scm.com/docs/gitprotocol-pack),
[capabilities](https://git-scm.com/docs/gitprotocol-capabilities) and
[v2 commands](https://git-scm.com/docs/gitprotocol-v2).
