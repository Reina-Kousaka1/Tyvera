# Tyvera memory model

This document describes the implemented bootstrap memory rules and the
remaining design work for later backends.

## Implemented reference behavior

- `Int`, `Float`, `Bool`, `Byte`, `Bytes`, `String`, and `Duration` are
  immutable values. `Bytes` assignment, function arguments, and returns have
  value semantics; slices and `ByteBuffer.toBytes()` produce independent
  immutable values.
- Classes, `List<T>`, and `Map<K,V>` are mutable reference values. Passing one
  through a function or storing it in another binding shares the same object.
  `let` prevents rebinding; it does not freeze the referenced object.
- Records, tuples, enums, and newtypes are immutable tagged values. Record and
  newtype immutability is shallow: a contained list or map remains mutable.
  `type` aliases are transparent and do not create runtime wrappers.
- Lists, maps, records, tuples, enums, newtypes, `Option`, `Result`, and `Json`
  use Tyvera-defined structural equality. Class and runtime-handle equality is
  identity-based. Scoped resources cannot be compared as public values. Mixed
  `Int`/`Float` equality compares the exact represented numeric values; Bool is
  a separate type.
- `Json` is an immutable tagged value. Assignment, function arguments, and
  returns preserve value semantics. Arrays preserve element order; objects
  preserve insertion order for `objectKeys()` and stringification, while
  structural equality ignores object key order. Native storage may share
  private nested values because Tyvera exposes no mutation operation on `Json`.
- Map storage uses a Tyvera-defined stable hash/equality contract. Keys are
  restricted to recursively hashable values and stable identity references;
  mutable collections and values containing them cannot be keys.
- Actor state is isolated. Actor messages accept the types described in the
  [language reference](language.md#cooperative-actors). Transferable value
  payloads are copied; an `ActorRef` continues to identify the same actor.
  `Byte` and immutable `Bytes` are transferable values; mutable `ByteBuffer`
  is not an actor message type.
- Tasks share references captured in their arguments. Tasks are serial and
  cooperative, so they do not race in parallel, but their interleaving can
  make shared mutation order observable.
- `ByteBuffer` is a shared mutable, runtime-managed reference with a 64 MiB
  maximum allocation. Assignment, function arguments, returns, and task
  arguments share that reference; mutations through an alias are visible.
  Tasks are cooperative and serial, so this sharing is observable through
  interleaving but does not execute in parallel. Actor boundaries reject
  `ByteBuffer` to preserve actor state isolation. Allocation and snapshot
  failures, and all indexed failures, return `Result`; `toBytes()` creates an
  independent immutable value. `Bytes` owns no external resource.
- `TextFile`, `TextReader`, `TextWriter`, `TcpConnection`, `TcpListener`, and
  `HttpServer`
  are identity resources with one move-only owner. `Option`, `Result`, and the
  `Task` handles produced by resource-returning intrinsics may carry resources;
  other aggregates and collections cannot. Async and synchronous file
  operations share the same file handles. The interpreter's Python garbage
  collector manages its own implementation objects; that allocation strategy
  is not a Tyvera guarantee.

The bootstrap's internal 64-bit hash algorithm is not exposed as a language
value. Backends may choose different hash bytes as long as equality, key
stability, and lookup behavior remain the same. A portable native Map ABI is
still future work.

## Memory Model V1

V1 keeps ordinary value and reference behavior while giving resources explicit
ownership. It does not require a general borrow checker. These rules describe
Tyvera semantics and the reference runtime; the experimental native backend
implements the supported subset noted below.

### Values and references

| Category | V1 direction | Status |
| --- | --- | --- |
| `Int`, `Float`, `Bool`, `Duration` | Immutable scalar values; assignment copies the value. | Implemented; signed 64-bit and finite binary64 rules are specified. |
| `Byte` | Immutable unsigned 8-bit scalar, explicitly converted to/from `Int` with range checking. | Reference runtime and the native `byteFromInt`/`toInt`/equality/output slice are implemented. |
| `String` | Immutable Unicode scalar sequence; equality compares exact sequence, without normalization. | Implemented for source and file I/O; indexing and grapheme APIs remain open. |
| `Bytes` | Immutable byte value; equality compares contents. Assignment, calls, returns, and actor messages preserve value semantics. Binary slices and indexed reads return checked results. | Reference runtime and native `bytesFromString`, equality, length, get, slice, UTF-8 decoding, and binary file I/O are implemented. |
| `ByteBuffer` | Shared mutable reference to zero-initialized, runtime-managed bytes; access is bounds checked and snapshots are immutable. | Reference runtime and native allocation/access/snapshot are implemented with a 64 MiB cap. Raw pointers, unchecked access, custom allocators, and manual deallocation are not exposed. |
| `Json` | Immutable tagged value; arrays are ordered and object equality is structural independent of insertion order. Parse/encode are bounded at 16 MiB and 128 nested containers. | Reference and experimental native C++17 parser, constructors, accessors, comparison, and compact encoding share these limits; user-defined record/ADT codecs are not implemented. |
| `Option<T>`, `Result<T,E>` | Immutable tagged values with structural equality; nested references retain their own identity/value behavior. | Implemented in the bootstrap and native backend for types in the native support subset. |
| Enums, newtypes, tuples, records | Typed value types; records are immutable nominal values and fields compare structurally. | Implemented in the bootstrap; non-generic immutable records and enums also have experimental native lowering. Newtypes, tuples, and generic nominal layouts remain interpreter-only. |
| Class instances, lists, maps | Classes use stable identity; lists and maps compare by contents. Assignment copies references; collection contents remain shared. | Lists and maps are implemented natively for supported element types; classes remain interpreter-only. |
| Actor references, tasks, reply handles, timers, supervisors | Opaque identity handles. Copying a handle does not copy the actor, task, or operation it names. | Interpreter implements the full experimental lifecycle. Native C++17 supports experimental actors, request/reply, monitoring/links, Supervisor V2, owned Tasks, and owner-bound TaskGroups. Actor message timers are actor-bound and cancel on lifecycle transitions; Task detachment drops only the parent Task edge, preserves any actor binding, and leaves the detached Task registered for bounded runtime shutdown. No public runtime ABI exists. |
| Resource handles | One live owner, explicit transfer, deterministic release. | Implemented for text files, TCP connections/listeners, and HTTP servers. Native file resources use checked moves and synchronous LIFO cleanup. Experimental native Linux TCP uses checked move-only connection/listener handles with a private RAII descriptor; in-flight operations use duplicated descriptors and closing the owner shuts down the shared socket. Native task cancellation cascades through owned children and socket waits; DNS cancellation is observed after the system lookup returns. Native `Task.withTimeout(Duration)` uses monotonic deadlines and waits for source cleanup. |

For mutable ordinary references, `==` compares identity. Value equality
compares scalar and immutable tagged values structurally, recursively applying
the nested value's rule. Lists and maps are mutable but intentionally compare
by current contents; they cannot be map keys. Class, actor, task, timer,
supervisor and reply identities use runtime identity tokens rather than Python
object addresses. Tyvera exposes neither Python's `id()`, `hash()`, nor object
layout. The numerical hash representation remains a bootstrap detail.

### Mutability and sharing

Binding mutability and object mutability are separate. `let` makes a binding
non-reassignable. A mutable reference held by that binding can still be changed
through its declared operations. A `var` binding permits rebinding. Class field
mutability remains controlled by the existing field rules.

Tasks may share class/list/map references while they run on the cooperative
single-threaded scheduler. This is deterministic for a fixed schedule, but it
does not promise isolation from logical interference. Actor state remains
private to its actor; actors communicate through typed messages and explicit
reply capabilities. If Tyvera later adds parallel tasks, shared mutation will
need a separately designed synchronization boundary such as an explicit
shared-state type.

No deep copy occurs implicitly. Actor message transfer is a narrow, checked
exception for immutable payloads and actor handles. Ordinary collection and
class values cannot be transferred into actor state.

### Move and resource ownership

Resource handles are move-only. `move name` transfers the sole close
responsibility and makes the prior binding unusable. The checker reports
double moves, use after move, and moves that occur on only some control-flow
paths. Ordinary values and ordinary references keep their current
copy/reference behavior; Tyvera does not make the whole language affine.
Resource names cannot be copied as ordinary expressions, compared, or used as
Map keys.

The resource-scope contract is:

1. `openText`, `openTextReader`, and `openTextWriter` create one owning handle
   inside `Result<Resource,String>`. `using` may take that result and create a
   scoped owner; owned results may also be moved into another binding or
   function.
2. Scope exit closes still-owned handles in reverse acquisition order.
3. Cleanup also runs on return, runtime failure, `break`, `continue`, task
   failure, cancellation, actor failure, supervisor restart, and unwinding
   from a nested block.
4. Explicit `close()` releases the handle once; it returns `false` on a second
   close, and scope cleanup observes that it is already closed.
5. Cleanup does not suspend. If cleanup fails during an existing failure, the
   original failure remains primary and cleanup failures are retained as
   secondary diagnostics.
6. Resources cannot cross actor-message boundaries or be stored in actor
   state. Actor handlers may acquire local resources; the handler scope closes
   them before a supervisor restarts a failed actor.

`using name = openText(path, mode) { ... }` preserves the original scoped
`TextFile` API. `openTextReader(path)` creates a read-only `TextReader`, while
`openTextWriter(path, mode)` creates a `TextWriter` in write or append mode.
Their original operations remain synchronous. The async file operations use
the same owned handles and defer close until in-flight worker operations
finish. `tcpConnect` and `tcpListen` return task results that own a connection
or listener; `accept` transfers a newly accepted connection through the same
`Result` carrier. A task result holding a resource must be bound and consumed
once by `await` or `tryResult()`; unclaimed results are closed during task,
scope, or runtime cleanup. A pending resource-returning task is move-only and
is cancelled when its scope ends; consuming `tryResult()` on a pending task
also cancels it and returns `Ok(None)`. Owned resources can move through function
parameters and into task parameters. A task owns its moved resources until
completion, failure, cancellation, actor stop/restart, or runtime shutdown;
detaching the task leaves ownership with that task. Resources cannot be stored
in collections, tuples, records, classes, newtypes, enums, actor state, or user
generic instantiations. `Option` and `Result` can carry resources and
participate in exhaustive ownership-preserving matches. No explicit borrowing
syntax is needed for receiver method calls; a Rust-style lifetime system is
not part of V1.

### Async I/O and platform boundary

The Tyvera contract describes task suspension, readiness wakeup, cancellation,
deadline behavior, partial byte reads/writes, EOF, backpressure, ownership, and
controlled `Result` errors. It does not expose Python sockets, asyncio,
exceptions, file objects, or OS address structs. The current reference
adapter uses a selector for TCP readiness and a bounded worker pool for DNS
and file operations. Native Backend V1 implements synchronous text and binary
file/path operations, plus an experimental Linux async-task/DNS/TCP subset.
Native socket readiness uses non-blocking descriptors and worker operations;
native cooperative cancellation follows owned-task trees and socket waits.
Monotonic deadlines and owner-bound TaskGroups are supported natively; async file operations remain interpreter-only.
Native operations must continue to match the same
observable Tyvera behavior with the platform's readiness facilities.

### Native executable subset

Native V1 compiles the synchronous scalar/control-flow subset described in
[Backend Architecture](backend-architecture.md), plus native `Option` and
`Result` values and nested constructor/literal/binding patterns. `List` and
`Map` support their typed core methods and snapshot iteration. Native `Byte`
supports `byteFromInt`, `toInt`, equality, and output; immutable `Bytes`
supports `bytesFromString`, equality, `length`, bounds-checked `get`/`slice`,
and UTF-8-checked `toString`. Native V1 also implements bounded `ByteBuffer`
allocation/access/snapshots; `readText`, `writeText`, `readBytes`, `writeBytes`,
`pathJoin`, `pathExists`; and synchronous `TextFile`, `TextReader`, and
`TextWriter` operations with checked move transfer and LIFO cleanup. Relative
I/O paths use the project root, and `.tyvera.secrets` is protected through
canonical direct, traversal, absolute, and symlink paths. The generated C++17
runtime preserves checked numeric behavior, controlled I/O results, and
source-located runtime errors. Executables do not invoke or embed Python.
The native backend has an experimental Linux slice for `Task<T>`, `await`,
`runTasks`, DNS, TCP, and cooperative task cancellation. Native monotonic task deadlines are implemented for the supported task subset.
Native actors, Supervisor V2, monitoring/links, and TaskGroups retain the
reference ownership boundaries. Non-generic records/enums are native values.
Generic functions/nominal types, classes, tuples, actor timers, HTTP
servers/routers/general decoders, WebSocket servers, and FFI remain
interpreter-only or planned. Native `Task.tryResult()` copies ordinary completed
values, but consumes a resource task: a completed handle is moved out exactly
once, while pending acquisition is cancelled and cleaned up by its owning
operation. No public native ABI is implemented.

`Bytes` is an immutable Tyvera value. Native values are represented internally
by vectors and passed by value, preserving observable value semantics.
`List<T>` and `Map<K,V>` are shared mutable references: assignment and
function calls preserve aliasing, mutation is visible through aliases, and
equality compares contents. List/Map iteration takes a snapshot before the
first loop body runs, matching the reference interpreter. Native collection
storage is private and does not define a public ABI or a complexity guarantee.

`Bytes` is immutable and does not own an external resource. Binary file
operations and `ByteBuffer` allocation/access are bounded to 67,108,864 bytes;
file, allocation, snapshot, and index failures are reported through `Result`. `ByteBuffer` uses
runtime-managed storage and exposes neither pointers nor unchecked slices.
`SocketAddress`
is a portable value containing a host string, a 16-bit port, and `IPv4` or
`IPv6` family. `TcpConnection`, `TcpListener`, and `HttpServer` are move-only.
An HTTP server owns its listener and the connection tasks it accepts.
`shutdown()` stops accepting and drains active connection tasks under their
configured deadlines; `close()` cancels the server tree and closes accepted
sockets. Active connections are capped by `HttpLimits.maxConnections`. Each
TCP connection permits one pending read and one pending write; a listener
permits one pending accept. The native implementation reserves each slot when
the operation is created, matching the reference runtime's rejection of a
second pending operation. `read(maxBytes)` caps one allocation and may return
fewer bytes, while `write(Bytes)` reports partial progress and `writeAll(Bytes)`
waits before continuing. The runtime does not keep an unbounded write queue.
The reference runtime can cancel a readiness wait; native `Task.cancel()` uses
cooperative checks at statement and socket-poll boundaries. Owned child tasks are
cancelled with their owner and joined before cleanup completes. Native close
shuts down the shared socket handle and wakes in-flight duplicated descriptors,
which are released when their bounded worker operation exits. A resource-returning
native Task transfers its result on `await`, rather than retaining a second owner
in the completed task. Native DNS uses the system resolver in a worker and
cannot be interrupted mid-lookup; cancellation is observed once it returns.

No general user-defined Stream interface is exposed yet. TCP is the first
byte-stream implementation; text files retain text-oriented methods. The
experimental HTTP/1.1 codec/client/server operate on bounded `Bytes` messages
over owned TCP connections. HTTP request/response values are ordinary values;
`HttpServer`, `TlsConnection`, and `WebSocket` are move-only resources in the
reference interpreter. Native Linux also supports an experimental move-only
`TlsConnection` client using OpenSSL trust paths, TLS 1.2 or later, and
hostname/IP verification. It owns the TCP handle and TLS session; scope cleanup
closes the TLS session before the socket, while cancellation releases pending
operation descriptors. The native backend also has bounded move-only HTTP and
WebSocket client resources with typed task results, verified TLS for HTTPS and
WSS, and cleanup through the same task/socket ownership boundary. HTTP servers,
routers and general decoders, WebSocket servers, TLS server mode, session Resume,
and protocols beyond HTTP/1.1 remain unsupported natively.

Native FFI is not implemented. A future ABI may pass `Byte` as an unsigned
8-bit scalar and immutable `Bytes` as a borrowed pointer/length span valid only
for the call. Mutable `ByteBuffer` may cross only through an explicit mutable
borrow with exclusive access and may not be retained after the call. Owned
resources require explicit transfer and release rules. Foreign status/errors
must be translated at the boundary; process faults such as segmentation
violations are not recoverable as Tyvera `Result` values. The safe language
currently defines no raw pointer or `unsafe` syntax.

### Lifetimes and runtime ownership

Ordinary heap values remain reachable while Tyvera references them; the
collector or allocator is a runtime implementation choice. Task, actor, and
resource lifetimes instead have explicit owners and shutdown rules. The
implemented task and actor ownership rules are in the
[runtime document](runtime.md). File and stream resources follow the scope
rules above. HTTP server ownership reuses the same move-only handles and
structured task cleanup; it does not add a separate lifetime system.

