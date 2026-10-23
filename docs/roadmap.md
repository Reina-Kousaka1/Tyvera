# Tyvera roadmap

Tyvera is an independent, statically typed programming language with
Java-inspired structure, Elixir-inspired concurrency and fault tolerance, and
a C++-inspired focus on performance and controlled system-level access.
Discord is a future library/SDK use case, not a compiler or language feature.
Roadmap items below are plans unless marked implemented.

| Stage | Status | Scope and exit condition |
| --- | --- | --- |
| 1. Core language | Implemented | Primitive types, functions, checked returns, lexical scopes, mutable/immutable bindings, operators, conditionals, `while`, `break` and `continue`, covered by tests. |
| 2. Object system | First slice implemented | Classes, fields, `self`, constructors, methods, interfaces, multiple interface implementation, interface dispatch, generic classes and generic functions. Generic methods/interfaces/constraints and inheritance remain open. |
| 3. Modules | First slice implemented | Relative `.tyv` imports, aliases, `pub` exports, module graph linking, visibility/missing/duplicate diagnostics, cycle detection and project-root containment. Local package dependencies are still planned. |
| 4. Standard library | Core slice implemented | Generic collections, `for`, `Option<T>`, `Result<T,E>`, core matching, text I/O/path helpers, local/environment secret providers and typed JSON parse/access/encode. Separate stdlib modules, timers and networking remain planned. |
| 5. Actor runtime | First slice implemented | Actor declarations, typed immutable message transfer, isolated state, FIFO mailboxes, deterministic round-robin cooperative scheduling, stop/alive lifecycle, and isolated crash reporting. True parallel scheduling, replies, cancellation, configurable capacity and backpressure remain open. |
| 6. Supervisors | Planned | Child lifecycle, crash detection, restart and an initial `one_for_one` policy, separate from ordinary recoverable errors. |
| 7. Networking | Planned | Async runtime abstraction, timers, HTTP and WebSocket client support, and event dispatch without Promise-specific language semantics. |
| 8. Native backend | Planned | Separate compiler backend, documented value/resource model, efficient value types and a narrow typed FFI. No unsafe native surface before its ownership and ABI rules are testable. |
| 9. Package system | Planned | Reproducible package metadata, dependency resolution and versioning after modules and the standard library stabilize. |
| 10. Discord SDK | Planned | A normal Tyvera library over modules, JSON, environment access, async I/O, WebSockets and actor lifecycle. No Discord-specific compiler behavior. |

## Near-term sequence

1. Add an actor supervisor with deterministic `one_for_one` restarts and
   restart limits, preserving the current explicit cooperative scheduling.
2. Define reply channels and cancellation on top of typed messages, then
   introduce timer-backed async tasks before HTTP/WebSocket support.
3. Move or wrap collection, I/O, JSON and secret operations behind normal
   `tyvera.*` modules as that library module model stabilizes.
4. Keep backend work independent from the Python interpreter. Establish
   language-level conformance programs before implementing native codegen.

## Native and C++-inspired direction

The future native path should preserve the same source grammar, AST, type
checking and diagnostics while replacing only execution/code generation. First
specify value representation, integer overflow, floating-point behavior,
resource ownership and FFI error mapping. Then prototype a native backend with
explicit value layouts and resource handles. Benchmark only after semantics
and conformance tests match the interpreter. Avoid untyped pointers,
unbounded foreign object ownership and direct exposure of C++ exceptions as
Tyvera errors.

## Current Discord-readiness gap

The object system, relative file modules, generic collections, file I/O,
provider-backed secrets, typed JSON core and cooperative actor/mailbox slice
are usable. Tyvera cannot yet perform asynchronous I/O or connect over
HTTP/WebSocket, so it cannot yet implement or run a real Discord bot. The next
deliverable is deterministic actor supervision, followed by reply channels,
timers and an async I/O model before HTTP/WebSocket and a Discord SDK.
