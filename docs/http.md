# HTTP and web foundation

HTTP V1 is an experimental standard-library foundation above the existing
TCP/task/resource runtime. It defines Tyvera-owned values and HTTP/1.1 byte
framing; Python sockets, exceptions, and HTTP objects are not exposed to a
Tyvera program. The reference interpreter uses the existing TCP readiness
adapter.

## Implemented

- `HttpMethod`, `HttpUrl`, `HttpLimits`, `HttpHeaders`, `HttpRequest`,
  `HttpResponse`, `HttpDecoder`, `HttpRouter`, and move-only `HttpServer`.
- Incremental HTTP/1.0 and HTTP/1.1 parsing with CRLF validation, bounded
  request line/header/body sizes, repeated headers, query values, and
  pipelined messages. HTTP/1.1 requests require exactly one `Host` header.
- Request bodies use `Content-Length`; duplicate or invalid lengths and
  request `Transfer-Encoding` are rejected, including ambiguous framing.
  The client response decoder supports bounded `Content-Length`, chunked, and
  close-delimited response bodies; request-side chunked bodies are not
  implemented.
- Typed request/response constructors and encoders, HEAD response-body
  suppression, case-insensitive headers, and a small router with static
  responses, `:parameter` path segments, 404, and 405 with `Allow`.
- A local server that accepts concurrent TCP connections, processes requests
  sequentially per connection, honors HTTP/1.0/1.1 connection persistence,
  applies the configured operation timeout, and closes each bounded response
  through `TcpConnection.writeAll`.
- Graceful `HttpServer.shutdown()` stops new accepts and lets active exchanges
  finish or reach their configured timeout. `HttpServer.close()` cancels the
  server task and its accepted connection tasks and closes owned sockets.
- Portable URL/address strings and IPv4/IPv6-compatible host values; network
  failures remain normalized `Result` strings.

## Tyvera API

These bootstrap intrinsics are available without imports:

```tyv
httpMethod(name: String) -> Result<HttpMethod, String>
httpUrl(value: String) -> Result<HttpUrl, String>
httpLimits(requestLineBytes: Int, headerBytes: Int,
           headerCount: Int, bodyBytes: Int,
           maxConnections: Int) -> Result<HttpLimits, String>
httpDefaultLimits() -> HttpLimits
httpHeaders() -> HttpHeaders
httpRequest(method: HttpMethod, target: String, headers: HttpHeaders,
            body: Bytes) -> Result<HttpRequest, String>
httpResponse(status: Int, headers: HttpHeaders,
             body: Bytes) -> Result<HttpResponse, String>
httpTextResponse(status: Int, body: String) -> Result<HttpResponse, String>
httpJsonResponse(status: Int, value: Json) -> Result<HttpResponse, String>
httpRouter() -> HttpRouter
httpRequestDecoder(limits: HttpLimits) -> HttpDecoder
httpResponseDecoder(limits: HttpLimits, requestMethod: HttpMethod) -> HttpDecoder
httpEncodeRequest(request: HttpRequest) -> Result<Bytes, String>
httpEncodeResponse(response: HttpResponse, keepAlive: Bool) -> Result<Bytes, String>
httpServer(move listener, router, limits, timeout) -> Result<HttpServer, String>
httpGet(url: HttpUrl, timeout: Duration) -> Task<Result<HttpResponse, String>>
httpPost(url: HttpUrl, headers: HttpHeaders, body: Bytes,
         timeout: Duration) -> Task<Result<HttpResponse, String>>
httpPut(url: HttpUrl, headers: HttpHeaders, body: Bytes,
        timeout: Duration) -> Task<Result<HttpResponse, String>>
httpDelete(url: HttpUrl, timeout: Duration) -> Task<Result<HttpResponse, String>>
httpSend(url: HttpUrl, request: HttpRequest,
         timeout: Duration) -> Task<Result<HttpResponse, String>>
```

`HttpDecoder.feed(Bytes)` accepts arbitrary fragments. `nextRequest()` and
`nextResponse()` return `Result<Option<...>, String>`; `finish()` marks EOF so
close-delimited responses and truncated messages can be distinguished. Request
values expose `method`, `target`, `path`, `version`, `headers`, `body`,
`header`, `query`, `queryValues`, `routeParameter`, `remoteAddress`, and
`isKeepAlive`. A server-parsed request carries its peer as a portable
`SocketAddress`; a request constructed without a connection returns `None`.
Headers are a mutable, ordered, case-insensitive multimap. Header values are
bounded to the ISO-8859-1 byte range; request targets use ASCII with
percent-encoded UTF-8. `HttpUrl` accepts ASCII DNS presentation names (IDNs
must use punycode) and bracketed IPv6 literals; fragments, userinfo, and IPv6
scope identifiers are rejected.

`HttpRouter.add(method, path, response)` registers a static response. It does
not retain a callback: Tyvera does not yet have first-class function values.
Programs that need per-request logic can use the typed decoder, inspect a
request, construct a response, and encode it over `TcpConnection`. `dispatch`
sets decoded path parameters on the request.

`HttpRequest.json()` parses a UTF-8 body with the existing `Json` parser and
returns `Result<Json, String>`. `httpJsonResponse` serializes an explicit
`Json` value and sets `application/json; charset=utf-8`; no automatic record or
ADT reflection is performed. `httpSend` accepts a custom typed request.
Convenience client operations use the same request/response models and existing
DNS/TCP tasks. The client replaces `Host` with the authority derived from its
`HttpUrl`, sends `Connection: close`, applies the requested timeout, and parses
bounded Content-Length, chunked, or close-delimited responses. The reference
client also accepts `https://` URLs through a verified TLS connection. It uses
the platform trust store, SNI, and hostname verification by default; there is
no verification-disable option and redirects are not followed. The HTTP server
remains plain TCP.

`HttpServer` takes ownership of a listener explicitly. `start()` is one-shot
and returns `Task<Result<Bool, String>>`; the task completes after shutdown and
after its owned connection tasks finish. The timeout applies to each pending
read/write operation. A request-read timeout produces HTTP 408 and closes that
connection; a write timeout closes the connection. Shutdown closes the
listener and stops accepts; close is the force-cancellation operation.

See [`examples/http_server.tyv`](../examples/http_server.tyv) for a real local
HTTP server/client exchange with two concurrent clients. The
[`examples/websocket_client.tyv`](../examples/websocket_client.tyv) example is
opt-in and connects only to a local echo server on `127.0.0.1:8765`.

## Experimental and planned

The HTTP/1.1 protocol core, static router, and reference server are implemented
and tested, but the bootstrap API is experimental. Current bounds from
`HttpLimits` are 256–65,536 bytes per request line, 1,024–262,144 bytes for
headers, 1–1,024 headers, and 0–16 MiB for a message body. The default limits
are 8 KiB, 32 KiB, 100 headers, 1 MiB, and 128 active connections. The
connection cap is configurable up to 4,096; accepted overflow connections are
closed. One in-flight read and one in-flight write are permitted by the
underlying connection. The request deadline covers the full header/body
receive, so occasional bytes cannot extend a slow-client deadline indefinitely.
The same duration bounds each response write and HTTP client
connect/write/read operation. The decoder buffers at most
one configured message at a time; responses are materialized as one bounded
byte payload, so there is no streaming response API yet.

There is no dynamic route handler, middleware, request/response streaming,
compression, automatic JSON-to-record codec, or importable `tyvera.http`
module. Existing `Json` values can be parsed or serialized explicitly. The
reference WebSocket client uses the same verified TLS connection for `wss://`
and has bounded framing; networking is still unsupported by the native
backend. HTTP/2, HTTP/3, TLS server mode, native networking, databases, and
application-specific protocols remain outside this milestone.

The reference implementation uses a backend-independent byte codec and the
existing internal TCP/task adapter. A future native runtime can replace the
adapter while preserving request/response framing, limits, timeout behavior,
ownership, and normalized errors. No Python exception or socket object is part
of Tyvera's observable API.
