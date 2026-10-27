# Tyvera Discord source package

`libraries/discord` is an experimental, repository-local Tyvera source package
and an integration exercise for TLS, WebSockets, JSON, `Secret`, `Option`,
`Result`, and pattern matching. The package manager is not implemented; the
directory is not published or automatically installed as a dependency.

## Implemented slice

`src/gateway.tyv` exports `GatewayDispatch` and
`decodeDispatch(payload: String) -> Result<GatewayDispatch, String>`. It
decodes the dispatch envelope, recognizes `READY` and `MESSAGE_CREATE`,
preserves other event names as `Unknown`, validates required fields, and maps
malformed or incomplete payloads to generic errors that do not echo the input.
`src/gateway.tyv` exports `GatewayDispatch`, `GatewayEnvelope`, and decoders for
`READY`, `MESSAGE_CREATE`, unknown dispatches, and the Gateway envelope.
`src/rest.tyv` provides `requestBot(token, method, endpoint, body, timeout)`.
It pins requests to `https://discord.com/api/v10/`, uses the verified HTTPS
client, puts the `Secret<String>` only in the `Authorization: Bot ...` header,
and returns status, bounded body bytes, and raw rate-limit response metadata.
`decodeFailure` and `decodeResponseFailure` map Discord error bodies to typed
`RestFailure` values. Requests are not retried; callers must decide whether an
operation is safe and honor rate-limit metadata themselves.

`src/client.tyv` provides typed `GatewayIntent` values and a bounded
`runGateway` session pinned to `wss://gateway.discord.gg/`. It performs TLS
verification, the WebSocket handshake, Hello parsing, Identify, heartbeat
sends, ACK tracking, sequence updates, READY/session capture, and basic
MESSAGE_CREATE counting. It does not expose message content to logs or emit a
user callback. The `bot.tyv` and `rest_client.tyv` examples do nothing networked
without `DISCORD_TOKEN`; CI checks and runs them without credentials.

The local fake Gateway and fake REST integration tests use temporary
self-signed test certificates, patched test-only DNS/port routing, and a fixed
fake token. They do not access a Discord account or public service.

## Experimental limits

The Gateway currently handles one bounded connection session. Opcode 7,
Invalid Session, disconnect, and missing heartbeat ACK produce controlled
errors; automatic reconnect, session Resume, close-code policy, jittered
backoff, and Invalid Session retry are not implemented. `resume_gateway_url`
is captured but not followed. The typed event decoder recognizes READY and
MESSAGE_CREATE, but the client only counts dispatches; there is no typed event
subscription API, Actor/Supervisor client tree, cache, or shard coordinator.

REST has one-request transport and rate-limit metadata parsing. There is no
bucket/global coordinator, bounded request queue, automatic retry, API model
coverage, or REST client Actor. Side-effect requests are never retried.
Gateway compression is not negotiated. Networking, TLS, HTTP, WebSockets, and
this package remain interpreter-only and experimental; generated native
programs do not yet provide these services. Do not treat this package as
production-ready.

When authentication is used, the token remains `Secret<String>` until the
request header or Identify payload is formed. The library itself never logs it;
HTTP header and request representations are redacted. Callers remain
responsible for not printing remote response bodies or explicitly revealed
secrets.
