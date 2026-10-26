# Tyvera Discord source package

`libraries/discord` is an experimental, repository-local Tyvera source package
and a real integration example for modules, enums, JSON, `Option`, `Result`,
and pattern matching. The package manager is not implemented; the directory is
not published or automatically installed as a dependency.

## Implemented slice

`src/gateway.tyv` exports `GatewayDispatch` and
`decodeDispatch(payload: String) -> Result<GatewayDispatch, String>`. It
decodes the dispatch envelope, recognizes `READY` and `MESSAGE_CREATE`,
preserves other event names as `Unknown`, validates required fields, and maps
malformed or incomplete payloads to generic errors that do not echo the input.
`src/rest.tyv` exports `decodeFailure(status, body)`, which validates Discord
REST error JSON and returns a `RestFailure` value with status, optional API
code, message, retry-after seconds, and global-limit metadata. It requires
`retry_after` on status 429 and parses metadata only; it never retries a
request.
`examples/decode.tyv` is checked and run by CI as a Tyvera project.

The decoder is local and deterministic. It does not read a bot token, make a
network request, or claim to be a Discord client.

## Planned transport and lifecycle

The reference runtime currently has TCP, DNS, and experimental HTTP/1.1, but
no TLS or WebSocket client. The package therefore does not yet implement
Identify, Hello/heartbeat/ACK, Dispatch over a socket, reconnect/Resume,
Invalid Session handling, REST transport, rate-limit scheduling, cache actors,
or a `DiscordSupervisor`. A TLS implementation must use a reviewed platform
cryptography library with certificate verification enabled by default; Tyvera
will not implement cryptography itself. A future WebSocket implementation
must remain a general networking primitive rather than a Discord-specific
transport.

When authentication is added, tokens must stay in `Secret<String>` until
passed to the trusted transport. Runtime events and errors must not include
them. Current tests use only fake payload strings and need no Discord account
or external network access.
