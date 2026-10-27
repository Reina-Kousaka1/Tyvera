# Tyvera Discord source package

`libraries/discord` is an experimental, repository-local Tyvera source package
and an integration exercise for TLS, WebSockets, JSON, `Secret`, `Option`,
`Result`, and pattern matching. The package manager is not implemented; the
directory is not published or automatically installed as a dependency.

## Implemented slice

`src/gateway.tyv` exports `GatewayDispatch`, `GatewayEnvelope`, and decoders for
`READY`, `MESSAGE_CREATE`, unknown dispatches, and the Gateway envelope. A
message's content is represented as `Option<String>` because Discord may omit
it when Message Content access is unavailable. The decoder validates required
identifiers and rejects a present non-string content field; malformed payloads
map to generic errors that do not echo the input.
`src/rest.tyv` provides `requestBot(token, method, endpoint, body, timeout)`.
It pins requests to `https://discord.com/api/v10/`, uses the verified HTTPS
client, puts the `Secret<String>` only in the `Authorization: Bot ...` header,
and returns status, bounded body bytes, and raw rate-limit response metadata.
`decodeFailure` and `decodeResponseFailure` map Discord error bodies to typed
`RestFailure` values. `requestBotWithRetries(..., maxRetries)` optionally
retries an explicit Discord 429 response only, with a caller budget of at most
five retries and a maximum accepted wait of 120 seconds. It honors the
`retry_after` body field through a monotonic scheduler timer, and falls back to a numeric
`Retry-After` delta-seconds header only when that body value is unavailable or malformed.
Both forms are capped at 120 seconds; HTTP-date headers are not parsed. Transport errors,
other statuses, and missing, malformed, negative, or over-limit delays are returned
without retry. This avoids replay after an ambiguous side-effecting request,
but does not coordinate concurrent requests across buckets or global limits.

`src/client.tyv` provides typed `GatewayIntent` values and a bounded
`runGateway` session pinned to `wss://gateway.discord.gg/`. It performs TLS
verification, the WebSocket handshake, Hello parsing, Identify, heartbeat
sends, ACK tracking, sequence updates, READY/session capture, and basic
MESSAGE_CREATE counting. `runGatewayReliable` adds a caller-bounded reconnect
loop with deterministic capped exponential backoff and returns the typed
dispatches observed in `GatewayRunSummary.events`. It resumes with the saved
session ID, resume URL, and last sequence after Opcode 7, resumable Invalid
Session, missed heartbeat ACK, or resumable peer disconnect; non-resumable
Invalid Session and close codes 4007/4009 start a new Identify session.
`GatewayRecoveryAction` centralizes close-code handling: 1000 stops, 1001 and
4000/4001/4002/4003/4005/4008 reconnect, 4004 and 4010–4014 are fatal, and
unrecognized codes fail closed. Reconnects are capped at 32, dispatches at 10,000, and the
backoff arguments at 120 seconds. Heartbeats use monotonic fixed deadlines;
the first heartbeat is currently sent after one full interval, without
Discord's recommended random initial jitter. There is no reconnect jitter;
callers should use a small reconnect budget appropriate to their application.
Resume URLs must be
`wss`, port 443, root-path URLs on `discord.gg` or its subdomains; this prevents
forwarding the bot token to an arbitrary host. `runGateway` remains a single
session API and returns the recovery action for its caller. The client does not
expose message content to logs or emit a user callback. The `bot.tyv` and
`rest_client.tyv` examples do nothing networked without `DISCORD_TOKEN`; CI
checks and runs them without credentials.

The local fake Gateway and fake REST integration tests use temporary
self-signed test certificates, patched test-only DNS/port routing, and a fixed
fake token. They do not access a Discord account or public service.

## Experimental limits

The reliable Gateway client is still experimental. Reconnect backoff is
deterministic and has no jitter, the retry budget is supplied by the caller,
and DNS/connect/Hello failures before a session is established return a
controlled error rather than being retried. There is no automatic resume after
process restart or durable session store. The typed event decoder recognizes
READY and MESSAGE_CREATE and returns these typed values in the run summary;
there is no callback/subscription API, Actor/Supervisor client tree, cache, or
shard coordinator. Gateway compression is not negotiated.

REST has one-request transport and rate-limit metadata parsing plus the
explicit bounded 429 retry function. There is no bucket/global coordinator,
concurrent request queue, automatic retry by default, API model coverage, or
REST client Actor. Side-effect requests are only retried after an explicit
429 rejection, never after a transport failure.
The package now has an experimental native lowering path for linked source
modules: non-generic records/enums, typed JSON use, `Secret<String>`, native
environment lookup, async REST helpers, and the Gateway client can be
represented in the native C++17 program without Discord-specific backend
rewrites. The checked-in decoder example is used for interpreter/native
differential execution. Linux CI also runs the same Gateway and REST examples
through both the reference runtime and a native C++17 executable against local
fake TLS services. Coverage includes Identify, heartbeat ACK, READY, reconnect,
resume, invalid-session re-identification, REST response metadata, bounded
429 retries, and token-redaction checks. A test-only resolver shim routes only
the native test process to loopback; the Tyvera package keeps its fixed,
verified Discord origins. This does not yet provide an Actor/Supervisor-owned
Discord lifecycle, a shared REST bucket coordinator, or production readiness.
There is no interpreter fallback.

When authentication is used, the token remains `Secret<String>` until the
request header or Identify payload is formed. The library itself never logs it;
HTTP header and request representations are redacted. Callers remain
responsible for not printing remote response bodies or explicitly revealed
secrets.

