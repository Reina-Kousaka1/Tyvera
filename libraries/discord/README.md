# Experimental Tyvera Discord library

This source package currently implements two typed payload adapters:
`gateway.decodeDispatch` recognizes `READY` and `MESSAGE_CREATE`, returns an
`Unknown` case for other dispatch names, and reports malformed JSON or
missing/wrongly typed fields through `Result<GatewayDispatch, String>`.
`rest.decodeFailure` maps an HTTP status and Discord JSON error body to a
`RestFailure` record, including optional API code and retry metadata. A 429
response requires a nonnegative floating-point `retry_after`; this is parsed
metadata only and does not schedule or retry requests.

Run the local example with `tyvera check libraries/discord` and
`tyvera run libraries/discord`.

This is a source package example, not a published package. The repository does
not yet have a package manager. It does not connect to Discord: the current
Tyvera runtime has no TLS or WebSocket client, and the native backend does not
implement networking. Gateway heartbeat, Identify/Resume, reconnect, REST
transport, rate-limit enforcement, cache, and client Actors are planned. The
decoder accepts payload text only; callers should obtain credentials through
Tyvera `Secret` facilities when a transport is added. It never reads or logs a
bot token.
