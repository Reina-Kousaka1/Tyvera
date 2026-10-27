# Standard library reference

The standard library is still a small bootstrap core. APIs below are
implemented and tested; listed future namespaces are not importable yet.

## Implemented bootstrap APIs

These core types and functions are currently available without an import:

| Type | Operations |
| --- | --- |
| `List<T>` | `List<T>()`, `append`, `insert`, `get -> Option<T>`, `set`, `removeAt -> Option<T>`, `contains`, `clear`, `.length`, and `for` over a snapshot of its elements. |
| `Map<K,V>` | `Map<K,V>()`, `put`, `get -> Option<V>`, `remove -> Option<V>`, `containsKey`, `keys`, `values`, `clear`, `.length`, and `for` over a snapshot of its keys. |
| `Byte` | `byteFromInt(Int) -> Result<Byte,String>` checks 0..255; `Byte.toInt()` explicitly converts back. Byte is an immutable scalar and can be printed, compared with Byte, used as a Map key, and sent as an actor message. It has no implicit Int conversion or arithmetic. |
| `Option<T>` | `Some(value)`, `None<T>()`, and pattern matching with `Some(value)`/`None`. |
| `Result<T,E>` | `Ok<T,E>(value)`, `Err<T,E>(error)`, and pattern matching with `Ok(value)`/`Err(error)`. |
| `Duration` | `milliseconds(Int)`, `seconds(Int)`, and `durationFromSeconds(Float) -> Result<Duration,String>` (rounds positive fractional milliseconds up and rejects negative, non-finite, or out-of-range values); checked `+`/`-`, comparisons, and `inMilliseconds()`. Signed millisecond values use checked 64-bit arithmetic. |
| `Timer` | `timerAfter(Duration)`, `isReady`, `remaining`, `cancel`, and `isCancelled`, backed by a monotonic clock. `ActorRef.sendAfter(Duration, handler, ...)` schedules a typed one-shot actor message. Timers never sleep or wake the runtime. |
| Actor replies | `ActorRef.request(handler, ...)` and `requestWithin(Duration, handler, ...)` return `ReplyReceiver<T>` for handlers ending in `ReplySender<T>`. Receivers poll with `tryReceive`, can be cancelled, and receive errors on actor crash/stop or deadline. |
| `Task<T>` | Calls to `async fn` and async I/O operations create typed tasks; bare `Task` means `Task<Void>`. `isDone()`, `isCancelled()`, `cancel()`, `detach()`, `withTimeout(Duration)`, and `tryResult()` expose lifecycle and values in the Reference Runtime. `withTimeout` uses a monotonic deadline and cancels its source; for resource-carrying tasks it transfers the task handle, and `tryResult()` consumes it. `sleepFor(Duration)` returns `Task<Void>`; `runTasks()` drives ready work and returns `Result<Int,String>`. `await` suspends on tasks, task groups, actor replies, or I/O completion. Reference tasks created within an async task are owned children: cancellation/failure cascades, child failure fails owners, and normal owner return joins unfinished children. Actor-created tasks are cancelled on stop, crash, restart, or runtime shutdown. Native Linux experimentally supports async functions, `await`, `sleepFor(Duration)`, `runTasks()`, `cancel()`, `isCancelled()`, `isDone()`, and `withTimeout(Duration)`. Native timeout and sleep deadlines use a monotonic clock; cancellation wakes a sleeping task without busy-waiting. I/O waits use worker threads. Ordinary task bodies are serialized and each run is capped at 64 registered task values. Native owned children cancel with their owner, normal owner completion joins children, and child failure fails the owner. Cancellation is cooperative at statements, loops, and socket polling; a system DNS lookup finishes before its cancellation can be observed. Native `tryResult()`, task groups, and actor ownership remain unsupported. Resource-containing task results move out when awaited. `runTasks()` reports completed native tasks but reference continuation steps, so this diagnostic count is not portable. |
| `TaskGroup` | `taskGroup()` creates a group owned by the current task (or synchronous root code). `add(Task<T>)` registers a direct child once and returns `Bool`; `isDone()` reports whether every registered task is terminal; `cancel()` cancels active members and returns the count. `await group` joins its members and returns no value. Membership does not transfer ownership, and owner return still joins every non-detached child. |
| Text I/O | `readText`, `writeText`, `pathJoin`, `pathExists`, `println`, and `eprintln`; file errors use `Result`. `openText`/`openTextReader`/`openTextWriter` are synchronous; `openTextAsync` and `TextFile`/`TextReader`/`TextWriter` `readAllAsync`, `writeAsync`, `appendAsync`, `flushAsync`, and `closeAsync` use the task scheduler's worker completion path. They reuse the same move-only handles and defer close until an in-flight operation has completed. Synchronous operations remain available and can block. |
| Binary file I/O | `readBytes(String) -> Result<Bytes,String>` and `writeBytes(String,Bytes) -> Result<Bool,String>` preserve bytes without text decoding. Relative paths resolve from the project root; `.tyvera.secrets` is protected. Operations reject payloads over 67,108,864 bytes and normalize missing-file, permission, and other I/O failures to `Err(String)`. |
| `Bytes` | Immutable byte payloads created with `bytesFromString(String)`, `readBytes`, or `ByteBuffer.toBytes()`. Assignment, function calls/returns, tasks, and actor messages preserve value semantics. `length()` reports byte count; `get(index)` and `slice(start,length)` return bounds-checked `Result` values; `toString()` validates UTF-8. |
| `ByteBuffer` | `byteBuffer(length) -> Result<ByteBuffer,String>` creates a zero-filled, shared mutable managed buffer up to 67,108,864 bytes. Assignments, function calls/returns, and task arguments share the buffer; actors reject it to preserve isolation. `length()`, bounds-checked `get(index)`/`set(index,Byte)`, and `toBytes() -> Result<Bytes,String>` expose safe access and immutable snapshots. No raw pointer, unchecked operation, or manual deallocation is exposed. |
| `SocketAddress` | Portable value returned by `resolveHost`, `TcpListener.localAddress`, and `TcpConnection.localAddress`/`remoteAddress`; exposes `host()`, `port()`, `family()` (`IPv4` or `IPv6`), and `toString()`. |
| `TcpConnection` | Move-only TCP byte stream. `read(maxBytes)` returns `Task<Result<Option<Bytes>,String>>`; a positive read may be partial and EOF is `Ok(None)`. `write(Bytes)` returns the number of bytes accepted by the OS and may be partial. `writeAll(Bytes)` waits for readiness until the supplied payload is accepted. `localAddress`, `remoteAddress`, `close`, and `isOpen` expose lifecycle and addresses. Native support is experimental on Linux and uses a private RAII FD handle; in-flight work owns a duplicate FD released when the task ends. One read and one write may be pending at once; a second pending read or write returns a controlled `Err`. Closing the owner shuts down the shared socket and wakes the operation. |
| `TcpListener` | Move-only listener. `accept()` returns `Task<Result<TcpConnection,String>>`; `localAddress`, `close`, and `isOpen` expose its bound address and lifecycle. `tcpListen(host, port, backlog)` accepts ports from 0 to 65535 and backlog from 1 to 4096; port `0` asks the OS to select a local port. Native support is experimental on Linux. |
| `TlsConnection` | Move-only TLS client resource. `tlsConnect(host, port)` returns `Task<Result<TlsConnection,String>>`; `read`, `write`, `writeAll`, address accessors, `close`, and `isOpen` mirror the TCP byte-stream contract. Certificate-chain and hostname/IP verification are enabled; TLS 1.2 or later is required, and no insecure mode is exposed. Native V1 accepts ASCII DNS names and IP literals; other host names return a controlled error. The reference runtime uses its platform TLS provider. Experimental native support is Linux-only and uses OpenSSL trust paths, linked at build time. TLS server mode is not implemented. |
| Async networking | `resolveHost(host, port)`, `tcpConnect(host, port)`, `tlsConnect(host, port)`, `tcpListen(host, port, backlog)`, `TcpListener.accept`, and connection reads/writes return `Task<Result<...,String>>`. Connect ports are from 1 to 65535; resolution ports are from 0 to 65535. The Reference Runtime uses bounded DNS workers and readiness registration. Native Linux uses a 64-task registry, non-blocking sockets, and I/O workers; it permits one pending accept per listener and one pending read/write per connection. Errors use normalized Tyvera strings inside `Result`, not host exception objects. Native socket waits observe task cancellation within the polling interval; cancellation of a blocking system DNS lookup is observed only after the resolver returns. Native `Task.withTimeout(Duration)` uses a monotonic deadline and cancels the pending operation; completion already published takes precedence at the deadline. Cancellation of a system DNS lookup is observed only after the resolver returns. |
| Experimental HTTP/1.1 and WebSocket | `HttpMethod`, `HttpUrl`, `HttpLimits`, `HttpHeaders`, `HttpRequest`, `HttpResponse`, `HttpDecoder`, `HttpRouter`, and move-only `HttpServer`; client requests use DNS/TCP or verified TLS for `https://`. `webSocketUrl(String)` returns a parsed `WebSocketUrl` with `scheme`, `host`, `port`, `target`, and `hostEndsWith(String)`; `webSocketConnect` accepts either a URL string or this parsed value. The bounded codec handles Content-Length, chunked, and close-delimited responses, rejects ambiguous framing, and enforces message limits. The reference runtime provides bounded `ws`/`wss` WebSocket resources. Native Linux experimentally supports typed HTTP client values and GET/POST/PUT/DELETE/custom requests, bounded HTTP/1.1 responses, and a bounded `ws`/verified-`wss` client; server/router/decoder support remains interpreter-only. See [HTTP foundation](http.md) for exact limits. |
| Secrets | `secrets.NAME` reads `.tyvera.secrets` before the process environment and returns `Option<Secret<String>>`; `Secret.reveal()` is explicit. |
| `Json` | `parseJson(String) -> Result<Json,String>`, `jsonStringify(Json) -> Result<String,String>`, typed constructors, and optional accessors. Both runtimes cap documents at 16 MiB and 128 nested containers; native C++17 supports this opaque value API experimentally. Record/ADT codecs and a source `tyvera.json` module are not implemented. |
| Host platform | `hostOS() -> String`, `hostArchitecture() -> String`, and `environment(String) -> Option<Secret<String>>`; values are normalized and environment values remain secret-wrapped in the reference and experimental native runtime. See [platform support](platform.md). |
| Actor runtime | `spawn(Actor(...)) -> ActorRef<Actor>`, `oneForOne`/`oneForAll`/`restForOne(maxRestarts, windowMilliseconds[, SupervisorBackoff]) -> Supervisor`, typed `RestartPolicy` values from `permanentChildPolicy()`/`transientChildPolicy()`/`temporaryChildPolicy()`, `exponentialBackoff(baseMilliseconds, maximumMilliseconds)`, nested `Supervisor.start`, and existing actor lifecycle APIs. Stop order is reverse child start order; restart order is start order. Restarts use monotonic intensity windows and bounded exponential backoff. Runtime lifecycle diagnostics are exposed to Python embedders; Tyvera-level monitor/link APIs are not implemented. |

See [the language reference](language.md#core-collections-and-iteration) for
collection signatures, key restrictions, ordering, mutation and runtime error
behavior, and [JSON values](language.md#json-values) for JSON signatures and
number handling.
Collections are mutable references even when held by a `let` binding. `Option`
and `Result` are implemented core sum types. `readText`/`writeText` resolve
relative paths from the project root and refuse direct access to
`.tyvera.secrets`. Secret values are redacted in diagnostics, and
`println`/`eprintln` reject `Secret<T>`.

## Native compiler support

The experimental native path supports the synchronous core and selected
standard-library values: `Option<T>`, `Result<T,E>`, typed matching for their
variants, `List<T>`, `Map<K,V>`, `Byte`, immutable `Bytes`, `ByteBuffer`,
synchronous file/path operations, and synchronous `TextFile`, `TextReader`,
and `TextWriter` resources. These work only when nested types are part of the
native backend matrix in [Backend Architecture](backend-architecture.md).
List/Map and `ByteBuffer` preserve shared mutation; `Bytes` preserves immutable
value semantics. Native `ByteBuffer` supports checked allocation, length,
`get`, `set`, and immutable `toBytes` snapshots, capped at 67,108,864 bytes.
Native file operations include UTF-8 text, exact binary data, project-root
relative paths, and protection for `.tyvera.secrets`. Synchronous text handles
use the existing move-only ownership rules and deterministic LIFO cleanup.
Async file operations, Actors, generic user-defined functions, and other
unsupported types remain interpreter-only. Native Linux also has experimental
Task/DNS/TCP/TLS, HTTP-client, and WebSocket-client paths described above.
Native HTTP server/router/general decoder and WebSocket server remain
interpreter-only. `tyvera build` reports unsupported calls or types; it never
routes a native program through Python. The private C++ representation is not a public ABI. This
native I/O subset is experimental and CI-tested on Linux x86-64 only.

## Planned namespaces

The module paths below are design targets, not current imports:

| Namespace | Planned APIs |
| --- | --- |
| `tyvera.time` | Importable wrappers around the implemented bootstrap `Duration` and `Timer` primitives. |
| `tyvera.concurrent` | Importable wrappers around implemented task ownership, groups, timeout, and cancellation APIs. |
| `tyvera.net` | Importable wrappers around the implemented TCP primitives. |
| `tyvera.http` | Importable wrappers around the experimental HTTP intrinsics; module packaging remains planned. |
| `tyvera.secrets` | A separately importable provider-backed secret library. |

Cooperative tasks, selector-backed TCP I/O, asynchronous text-file operations,
and DNS lookup are implemented in the reference runtime. The native Linux
subset supports bounded tasks, cooperative task cancellation, DNS/TCP, and
experimental monotonic deadlines through `Task.withTimeout(Duration)`.
Cancellation of an in-progress system DNS lookup is observed after the resolver
returns.
HTTP/1.1 client and server primitives are experimental in the reference
runtime; the native backend currently implements an experimental bounded HTTP
client and WebSocket client on Linux. There is not yet a general user-defined
Stream interface: `TcpConnection` defines the current
byte-stream contract, and async text files reuse their existing resource API.
JSON, collection, I/O and secret operations
are bootstrap intrinsics rather than source `.tyv` modules, so they are not
yet imported through `tyvera.*` namespaces. JSON codecs for records and ADTs
are also not implemented; callers currently inspect or construct `Json`
explicitly.

Actor requests are non-blocking: call `runActors()` to process messages and
then poll `ReplyReceiver.tryReceive()`, or `await` the receiver from an async
function. Await resumes when a reply arrives; successful reply data remains in
the receiver and is consumed once by `tryReceive()`. Cancellation and deadlines
do not remove queued work or interrupt an actor handler. The interpreter's
monotonic clock and sleep function are injectable so timer, timeout and task
tests do not need wall-clock delays.

`ActorRef.sendAfter` schedules delivery against the same monotonic clock.
`runActors()` promotes messages whose deadlines have elapsed; it returns when
no immediate messages remain instead of sleeping until a future timer.
`runTasks()` waits until the next task or awaited actor deadline when all tasks
are suspended, then resumes them. It does not create background threads.

Async functions can return statically checked values. `await task` waits without
binding a value; `let value: T = await task` binds a `Task<T>` or typed reply
payload. `await` is supported in nested blocks, branches, loops and match arms
inside an async function. Each scheduler turn advances one statement or
continuation action; synchronous calls and expressions execute atomically and
cannot be preempted. Tasks created while an async task runs are owned children.
Owner cancellation or failure cancels descendants, child failure fails owners,
and normal owner return joins unfinished children. `Task<T>.detach()` returns
`true` only when called by its direct owner on an active child. Detaching removes
the parent ownership edge and removes the task from any groups owned by that
parent; the task continues as scheduler-root work, and its failure is still
reported by `runTasks()`. Root and terminal tasks cannot be detached. Task and
TaskGroup values cannot be sent through actor messages. `Task<T>.tryResult()` reports pending as
`Ok(None)`, completion as `Ok(Some(value))`, and failure or cancellation as
`Err`; for `Task<Void>` it returns `Result<Bool,String>` (`Ok(false)` pending,
`Ok(true)` completed). Task arguments use ordinary function-call capture and
reference behavior. `runTasks()` reports task failure as `Err` too.

The interpreter runtime runs once. When `main` returns or fails, shutdown
cancels every unfinished task (including detached roots), stops actors and
supervisors, clears queued messages and timers, and fails pending replies.
Detached task failures stay visible through `runTasks()` before shutdown. See
[Runtime semantics](runtime.md) for ownership and scheduling details.


