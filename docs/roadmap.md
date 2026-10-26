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
| 7. Async I/O & TCP | V1 implemented | Selector-backed TCP connect/listen/accept/read/write, worker-backed DNS and async text-file I/O, cancellation, deadlines, move-only network resources, address/error values, and bounded write behavior. TLS and protocols beyond HTTP/1.1 remain planned. |
| 8. HTTP & Web Backend Foundation | V1 implemented; experimental | Bounded HTTP/1.1 codec, typed request/response values, TCP client and concurrent server, keep-alive, static routing, query/path parameters, JSON core integration, deadlines, and ownership cleanup. Streaming, dynamic handlers, TLS, and HTTP/2/3 remain planned. |
| 9. Native backend | V1 expanded; experimental | Typed IR and C++17 host compilation support the synchronous scalar/control-flow core, `Option`/`Result` matching, typed `List`/`Map`, `Byte`/`Bytes`, bounded `ByteBuffer`, synchronous text/binary/path I/O, and owned text-file resources with deterministic cleanup. Linux x86-64 is CI-tested; objects, generic functions, Actors, Tasks, async I/O, networking, HTTP, and FFI remain interpreter-only or planned. |
| 10. Linux platform foundation | First bootstrap slice implemented; experimental | Ubuntu x86-64 CI validation, normalized OS/architecture detection, secret-wrapped environment access, monotonic host time, cooperative SIGINT/SIGTERM shutdown, and an internal argv-based child-process adapter. ARM64 is recognized but not CI-tested; native platform runtime and packaged distributions remain planned. |
| 11. Package system | Planned | Reproducible package metadata, dependency resolution and versioning after modules and the standard library stabilize. The Discord source example is repository-local, not package-manager support. |
| 12. Application library ecosystem | First source example | An experimental local Discord package decodes typed `READY`, `MESSAGE_CREATE`, and unknown Gateway dispatch JSON. TLS, WebSockets, live Gateway lifecycle, REST transport, rate-limit enforcement, and client actors are not implemented. General-purpose libraries and application protocols remain outside compiler semantics. |

## Near-term sequence

1. Stabilize and review the current Linux platform and Supervisor V2 branch;
   verify Ubuntu CI and the remaining restart/shutdown edge cases before
   expanding the runtime.
2. Prepare the native async/task/actor runtime boundary only after this branch
   has been reviewed and integrated.
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
True parallelism, TLS, HTTP streaming, WebSockets, non-network resource families,
standard-library modules, and a native runtime for the full language remain
planned. The native backend's synchronous core and selected `Option`/`Result`,
collection, `Byte`, and `Bytes` paths are implemented experimentally. HTTP/1.1
V1 is experimental and implemented. Resource scopes
close on normal exit, return, errors, task cancellation, loop control, actor
failure, and supervisor restart. Runtime ownership, shutdown, detached task
failures, timed wrappers, and actor reply fairness have explicit semantics and
tests. The portable value and equality contract is implemented in the
bootstrap. HTTP V1 has a separate feature branch; later milestones require an
explicit scope decision before work begins.

