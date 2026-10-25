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
| 6. Supervisors | First slice implemented | Child lifecycle, crash detection, bounded restart intensity and deterministic `one_for_one` restarts. `one_for_all` and `rest_for_one` remain planned. |
| 7. Async I/O & TCP | V1 implemented | Selector-backed TCP connect/listen/accept/read/write, worker-backed DNS and async text-file I/O, cancellation, deadlines, move-only network resources, address/error values, and bounded write behavior. TLS and protocols beyond HTTP/1.1 remain planned. |
| 8. HTTP & Web Backend Foundation | V1 implemented; experimental | Bounded HTTP/1.1 codec, typed request/response values, TCP client and concurrent server, keep-alive, static routing, query/path parameters, JSON core integration, deadlines, and ownership cleanup. Streaming, dynamic handlers, TLS, and HTTP/2/3 remain planned. |
| 9. Native backend | Planned; backend boundary implemented | `CheckedProgram` and `ExecutionBackend` currently select the Python reference interpreter. Native code generation, runtime ABI, and `tyvera build` remain unimplemented. Initial targets are Windows x86-64, Linux x86-64, Linux ARM64, and macOS ARM64. |
| 10. Package system | Planned | Reproducible package metadata, dependency resolution and versioning after modules and the standard library stabilize. |
| 11. Application library ecosystem | Planned | Build general-purpose libraries and use larger applications as later conformance and load tests; application protocols stay outside compiler semantics. |

## Near-term sequence

1. Review and merge HTTP & Web Backend Foundation V1 when approved; choose the
   next architecture block explicitly after that review. No subsequent branch
   is started automatically.
2. Move or wrap collection, I/O, JSON, and secret operations behind normal
   `tyvera.*` modules as the module model stabilizes; continue generic interface
   and constraint work with conformance tests.
3. Extend generic constraints to type declarations; keep adding conformance
   cases for recursive patterns, structural types and backend-independent
   equality.
4. Keep code generation independent from the Python reference interpreter.
   Prototype shortlisted backends against one shared program corpus before
   selecting a native toolchain or implementing `tyvera build`.

## Native and C++-inspired direction

The future native path should preserve the same source grammar, AST, type
checking and diagnostics while replacing only execution/code generation.
Integer, Float, Bool, left-to-right evaluation, Unicode scalar string equality,
tagged-value equality, and portable Map keyability are specified in the
bootstrap language contract. String indexing, a native resource ABI, and FFI
error mapping still need contracts and implementations.
The implemented backend boundary and the comparison of LLVM, Cranelift, C
transpilation, and a custom backend are in
[Backend Architecture](backend-architecture.md). Implemented ownership rules are
in the [Memory Model](memory-model.md). Benchmark only after semantics and
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
standard-library modules, and a native backend remain planned. HTTP/1.1 V1 is
experimental and implemented. Resource scopes
close on normal exit, return, errors, task cancellation, loop control, actor
failure, and supervisor restart. Runtime ownership, shutdown, detached task
failures, timed wrappers, and actor reply fairness have explicit semantics and
tests. The portable value and equality contract is implemented in the
bootstrap. HTTP V1 has a separate feature branch; later milestones require an
explicit scope decision before work begins.

