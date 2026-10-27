# Experimental Tyvera Discord library

This repository-local source package now has a bounded real HTTPS REST request
path and WSS Gateway session in the reference runtime. The REST client is pinned
to Discord's verified HTTPS API origin, uses `Secret<String>` for Bot
authorization, returns bounded response bytes and rate-limit headers, and
never retries by default. `requestBotWithRetries` can retry a bounded number
of explicit HTTP 429 rejections, preferring Discord's `retry_after` body
value and falling back to a numeric `Retry-After` delta-seconds header when
the body value is missing or invalid. Delays are capped at 120 seconds;
HTTP-date headers are not parsed. It does not retry
transport failures or other statuses. `decodeFailure` maps Discord
error JSON into a typed `RestFailure` record.

The Gateway client uses typed intents and implements TLS verification,
WebSocket handshake, Hello, Identify, heartbeats, ACK tracking, sequence
updates, and basic READY/MESSAGE_CREATE dispatch handling. `GatewayRunSummary.events`
returns typed dispatches observed across the bounded reconnect loop. `bot.tyv` and
`rest_client.tyv` require `DISCORD_TOKEN`; without it, they print an
instruction and do not make a network request. CI checks/runs both without
credentials. Local integration tests use fake TLS Gateway/REST servers and
fixed fake tokens, never Discord production.

There is no shared REST bucket/global coordinator or queue, typed event
callback, client Actor/cache tree, or native networking. The package manager
is not implemented and this package is not published. See
[the full support status](../../docs/discord.md). This software is experimental
and not production-ready.
