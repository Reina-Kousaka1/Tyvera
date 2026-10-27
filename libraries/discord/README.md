# Experimental Tyvera Discord library

This repository-local source package now has a bounded real HTTPS REST request
path and WSS Gateway session in the reference runtime. The REST client is pinned
to Discord's verified HTTPS API origin, uses `Secret<String>` for Bot
authorization, returns bounded response bytes and rate-limit headers, and
never retries automatically. `decodeFailure` maps Discord error JSON into a
typed `RestFailure` record.

The Gateway client uses typed intents and implements TLS verification,
WebSocket handshake, Hello, Identify, heartbeats, ACK tracking, sequence
updates, and basic READY/MESSAGE_CREATE dispatch counting. `bot.tyv` and
`rest_client.tyv` require `DISCORD_TOKEN`; without it, they print an
instruction and do not make a network request. CI checks/runs both without
credentials. Local integration tests use fake TLS Gateway/REST servers and
fixed fake tokens, never Discord production.

Reconnect/Resume, close-code policy, REST rate-limit coordination, safe request
retries, typed event callbacks, client Actors/cache, and native networking are
not implemented. The package manager is also not implemented; the package is
not published. See [the full support status](../../docs/discord.md). This
software is experimental and not production-ready.
