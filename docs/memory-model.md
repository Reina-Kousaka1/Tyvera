# Tyvera memory model

This document describes the implemented bootstrap memory rules and the
remaining design work for later backends.

## Implemented reference behavior

- `Int`, `Float`, `Bool`, `String`, and `Duration` are immutable values.
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
- Map storage uses a Tyvera-defined stable hash/equality contract. Keys are
  restricted to recursively hashable values and stable identity references;
  mutable collections and values containing them cannot be keys.
- Actor state is isolated. Actor messages accept the types described in the
  [language reference](language.md#actors). Transferable value payloads are
  copied; an `ActorRef` continues to identify the same actor.
- Tasks share references captured in their arguments. Tasks are serial and
  cooperative, so they do not race in parallel, but their interleaving can
  make shared mutation order observable.
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
ownership. It does not require a general borrow checker.

### Values and references

| Category | V1 direction | Status |
| --- | --- | --- |
| `Int`, `Float`, `Bool`, `Duration` | Immutable scalar values; assignment copies the value. | Implemented; signed 64-bit and finite binary64 rules are specified. |
| `String` | Immutable Unicode scalar sequence; equality compares exact sequence, without normalization. | Implemented for source and file I/O; indexing and grapheme APIs remain open. |
| `Json`, `Option<T>`, `Result<T,E>`, enums, newtypes, tuples | Immutable tagged values with structural equality; nested references retain their own identity/value behavior. | Implemented in the bootstrap; native representation remains planned. |
| Records | Immutable nominal value records; fields compare structurally. Nested mutable references remain shared. | Implemented, including generic types, destructuring, and safe derived map keys. |
| Class instances, lists, maps | Classes use stable identity; lists and maps compare by contents. Assignment copies references; collection contents remain shared. | Implemented in the bootstrap. |
| Actor references, tasks, reply handles, timers, supervisors | Opaque identity handles. Copying a handle does not copy the actor, task, or operation it names. | Implemented in the interpreter; public runtime ABI is planned. |
| Resource handles | One live owner, explicit transfer, deterministic release. | Implemented for text files, TCP connections/listeners, and HTTP servers, including resource-bearing task results, path-sensitive moves, and task/scope cleanup. |

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
and file operations. A future native backend must implement the same
observable behavior with the platform's completion or readiness facilities.

`Bytes` is immutable and does not own an external resource. `SocketAddress`
is a portable value containing a host string, a 16-bit port, and `IPv4` or
`IPv6` family. `TcpConnection`, `TcpListener`, and `HttpServer` are move-only.
An HTTP server owns its listener and the connection tasks it accepts.
`shutdown()` stops accepting and drains active connection tasks under their
configured deadlines; `close()` cancels the server tree and closes accepted
sockets. Active connections are capped by `HttpLimits.maxConnections`. Each
TCP connection permits one pending read and one pending write; `read(maxBytes)`
caps one allocation and may return fewer bytes, while `write(Bytes)` reports
partial progress and `writeAll(Bytes)` waits before continuing. The runtime
does not keep an unbounded write queue. Cancellation removes a readiness wait;
the owning scope closes the socket as it unwinds. DNS and in-flight file work
already running in a worker is allowed to complete, and shutdown drains those
workers before closing the runtime.

No general user-defined Stream interface is exposed yet. TCP is the first
byte-stream implementation; text files retain text-oriented methods. The
experimental HTTP/1.1 codec/client/server operate on bounded `Bytes` messages
over owned TCP connections. HTTP request/response values are ordinary values;
`HttpServer` and its listener/connections remain move-only resources. TLS can
later adapt the owned byte-stream boundary using a vetted platform
cryptography library. TLS and protocols beyond HTTP/1.1 remain planned.

### Lifetimes and runtime ownership

Ordinary heap values remain reachable while Tyvera references them; the
collector or allocator is a runtime implementation choice. Task, actor, and
resource lifetimes instead have explicit owners and shutdown rules. The
implemented task and actor ownership rules are in the
[runtime document](runtime.md). File and stream resources follow the scope
rules above. HTTP server ownership reuses the same move-only handles and
structured task cleanup; it does not add a separate lifetime system.
