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
