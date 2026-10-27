# Tyvera roadmap

Tyvera is an independent, statically typed programming language with
Java-inspired structure, Elixir-inspired concurrency and fault tolerance, and
a C++-inspired focus on performance and controlled system-level access.
Tyvera's core is being developed independently of any one application domain;
larger real applications will be useful stress tests after the language and
runtime foundations are stable. Roadmap items below are plans unless marked
implemented.

| Stage | Status | Scope and exit condition |
| --- | --- | --- |
| 1. Core language | Implemented | Primitive types, functions, checked returns, lexical scopes, mutable/immutable bindings, operators, conditionals, `while`, `break` and `continue`, covered by tests. |
| 2. Object and value types | First slice implemented | Classes, fields, `self`, constructors, methods, interfaces and dispatch, plus generic immutable records, enums, tuples, newtypes, transparent aliases, interface-constrained generic functions, recursive exhaustiveness for finite sum/product patterns, portable value equality, and statically hashable Map keys. Generic methods/interfaces, bounds on generic type declarations, match guards and inheritance remain open. |
| 3. Modules | First slice implemented | Relative `.tyv` imports, aliases, `pub` exports, module graph linking, visibility/missing/duplicate diagnostics, cycle detection and project-root containment. Local package dependencies are still planned. |
| 4. Standard library | Core slice implemented | Generic collections, `for`, `Option<T>`, `Result<T,E>`, core matching, text I/O/path helpers, move-only `TextFile`/`TextReader`/`TextWriter` ownership with general move transfer and deterministic LIFO cleanup, local/environment secret providers, typed JSON, signed `Duration`, monotonic one-shot timers, timer-backed sleep tasks, cooperative parent-scoped lifecycle, owner-local `TaskGroup` joins/cancellation, explicit child detachment, actor-owned task cancellation, one-shot runtime shutdown and typed task deadlines. Separate stdlib modules remain planned. |
| 5. Actor runtime | First slice implemented | Actor declarations, typed immutable message transfer, isolated state, FIFO mailboxes, deterministic round-robin cooperative scheduling, typed one-shot request/reply, cancellation, request deadlines, delayed typed messages, stop/alive lifecycle, and isolated crash reporting. Parallel scheduling and backpressure remain open. |
| 6. Supervisors | V2 foundation implemented; experimental | Nested supervisors, `one_for_one`/`one_for_all`/`rest_for_one`, permanent/transient/temporary policies, monotonic restart intensity, capped exponential backoff, typed crash/runtime events, bounded dead-letter diagnostics, and deterministic tree shutdown. User-defined monitor/link APIs and native supervisor runtime remain planned. |
| 7. Async I/O & TCP | Reference V1 implemented; experimental | Selector-backed reference TCP/DNS, async text-file I/O, cancellation, deadlines, move-only resources, and bounded writes. Native C++17 has an experimental Linux DNS/TCP/TLS-client slice using bounded `Task` workers, RAII descriptors, verified OpenSSL trust paths and hostname/IP checks, and cooperative cancellation through owned task trees and socket waits. Native `Task.withTimeout(Duration)` uses monotonic deadlines; Windows/macOS networking remain planned. |
| 8. HTTP & Web Backend Foundation | Reference V1 and native client slice implemented; experimental | Reference bounded HTTP/1.1 codec/client/server, verified HTTPS client, static routing, JSON integration, deadlines, and ownership cleanup. Native Linux has a typed, bounded HTTP/1.1 client and verified HTTPS path. Native server/router/general codec, streaming, TLS server mode, dynamic handlers, and HTTP/2/3 remain planned. |
| 9. Native backend | V1 expanded; experimental | Typed IR and C++17 host compilation support the synchronous scalar/control-flow core, `Option`/`Result` matching, typed `List`/`Map`, `Byte`/`Bytes`, bounded `ByteBuffer`, synchronous text/binary/path I/O, owned text-file resources, and a limited Linux async/DNS/TCP/TLS-client path with RAII resources and cooperative task cancellation. Linux x86-64 is CI-tested; objects, generic functions, Actors, Supervisors, native HTTP/WebSocket servers, general HTTP decoding, and FFI remain interpreter-only or planned. |
| 10. Linux platform foundation | First bootstrap slice implemented; experimental | Ubuntu x86-64 CI validation, normalized OS/architecture detection, secret-wrapped environment access, monotonic host time, cooperative interpreter/native SIGINT/SIGTERM shutdown, and an internal argv-based child-process adapter. Native DNS/TCP is an experimental Linux slice; a Tyvera child-process API and ARM64 CI remain planned. |
| 11. Ubuntu packaging | Experimental Debian package validation | Debian metadata builds the Python bootstrap package; Ubuntu 24.04 CI is configured for install, revision upgrade, reinstall, remove, purge, and preservation of a user project and local secret file. No public `.deb`, signed source upload, PPA, repository, or dependency resolver exists. |
| 12. Application library ecosystem | Experimental live slice | Local Discord source package has a fixed-origin HTTPS REST request primitive, typed rate-limit metadata, bounded explicit retries for 429 responses, typed Gateway intents/events, and a bounded reconnect/Resume loop with heartbeat ACK tracking and close-code policy. Shared REST bucket/global coordination, event callbacks, Actors/cache, sharding, and native execution of the Discord package remain unimplemented. Native HTTP/WebSocket client primitives do not yet make imported Tyvera packages or Discord's actor-based client native-supported. |

## Near-term sequence

1. Expand native HTTP/WebSocket differential and protocol-boundary coverage,
   then keep the Linux client implementation aligned with the reference
   contract.
2. Integrate Linux signal-shutdown with supervised task/resource cleanup and
   design a native supervisor runtime only when actor/task ownership can retain
   the existing semantics; keep host APIs private.
3. Move or wrap collection, I/O, JSON, and secret operations behind normal
   `tyvera.*` modules as the module model stabilizes; continue generic interface
   and constraint work with conformance tests.
4. Extend generic constraints to type declarations; keep adding conformance
   cases for recursive patterns, structural types and backend-independent
   equality.
5. Keep native lowering independent from the Python reference interpreter and
   expand the supported subset only with differential conformance tests. Revisit
   LLVM or Cranelift if broader target/debug requirements justify the additional
   toolchain complexity.

## Native and C++-inspired direction

The native path preserves the existing source grammar and type checker while
lowering the supported subset from checked metadata into backend-neutral IR.
Integer, Float, Bool, evaluation order, strings, and control flow are compared
against the reference interpreter for the V1 corpus. A native resource ABI,
FFI error mapping, and runtime services for the remaining language are still
planned. The backend evaluation and exact current boundary are in
[Backend Architecture](backend-architecture.md); ownership rules are in the
[Memory Model](memory-model.md). Benchmark only after broader semantics and
conformance tests match the interpreter.

## General application readiness gap

The object system, relative file modules, generic collections, file I/O,
provider-backed secrets, typed JSON core, supervised actors, typed replies,
cooperative async tasks with typed return values, parent-scoped cancellation,
deadlines, delayed actor messages, explicit task groups, detached child tasks
and typed task timeouts are usable. Await resumes inside nested blocks,
branches, loops and match arms. Move-only text and TCP resources support
`Option`/`Result` carriers, task transfer, control-flow checking, and
deterministic cleanup. Async text-file operations, DNS, selector-backed TCP,
backpressure, and cancellation are implemented in the reference runtime.
True parallelism, HTTP streaming, native HTTP/WebSocket services, non-network
resource families, standard-library modules, and a native runtime for the full
language remain planned. The reference runtime's verified TLS client and bounded
WebSocket client are implemented experimentally; the native backend's
synchronous core, selected `Option`/`Result` and collection paths, `Byte` and
`Bytes`, and experimental Linux DNS/TCP/TLS paths remain interpreter-independent.
HTTP/1.1 V1 is experimental and implemented. Resource scopes
close on normal exit, return, errors, task cancellation, loop control, actor
failure, and supervisor restart. Runtime ownership, shutdown, detached task
failures, timed wrappers, and actor reply fairness have explicit semantics and
tests. The portable value and equality contract is implemented in the
bootstrap. HTTP V1 has a separate feature branch; later milestones require an
explicit scope decision before work begins.


