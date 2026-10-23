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
| 2. Object system | First slice implemented | Classes, fields, `self`, constructors, methods, interfaces, multiple interface implementation, interface dispatch, generic classes and generic functions. Generic methods/interfaces/constraints and inheritance remain open. |
| 3. Modules | First slice implemented | Relative `.tyv` imports, aliases, `pub` exports, module graph linking, visibility/missing/duplicate diagnostics, cycle detection and project-root containment. Local package dependencies are still planned. |
| 4. Standard library | Core slice implemented | Generic collections, `for`, `Option<T>`, `Result<T,E>`, core matching, text I/O/path helpers, local/environment secret providers, typed JSON, signed `Duration`, and monotonic one-shot timers. Separate stdlib modules and timer-driven waiting remain planned. |
| 5. Actor runtime | First slice implemented | Actor declarations, typed immutable message transfer, isolated state, FIFO mailboxes, deterministic round-robin cooperative scheduling, typed one-shot request/reply, cancellation, request deadlines, delayed typed messages, stop/alive lifecycle, and isolated crash reporting. Parallel scheduling and backpressure remain open. |
| 6. Supervisors | First slice implemented | Child lifecycle, crash detection, bounded restart intensity and deterministic `one_for_one` restarts. `one_for_all` and `rest_for_one` remain planned. |
| 7. Networking | Planned | Structured task/async runtime and timer wakeups, then HTTP and WebSocket client support and event dispatch without Promise-specific language semantics. |
| 8. Native backend | Planned | Separate compiler backend, documented value/resource model, efficient value types and a narrow typed FFI. No unsafe native surface before its ownership and ABI rules are testable. |
| 9. Package system | Planned | Reproducible package metadata, dependency resolution and versioning after modules and the standard library stabilize. |
| 10. Application library ecosystem | Planned | Build general-purpose libraries and use larger applications as later conformance and load tests; application protocols stay outside compiler semantics. |

## Near-term sequence

1. Implement typed actor requests, one-shot replies, cancellation, deadlines,
   `Duration`, and delayed mailbox delivery. Next define resumable task state and
   structured `async`/`await` so timers can wake work without polling.
2. Move or wrap collection, I/O, JSON and secret operations behind normal
   `tyvera.*` modules as that library module model stabilizes.
3. Continue improving generic interfaces, constraints and type inference with
   conformance tests before expanding the object model.
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

## General application readiness gap

The object system, relative file modules, generic collections, file I/O,
provider-backed secrets, typed JSON core, supervised actors, typed replies,
cancellation, deadlines and delayed actor messages are usable. Resumable async tasks, timer-driven
wakeups, resource ownership, networking and a native backend are still
missing. The next deliverable is a resumable task state machine that composes
with actor requests and monotonic deadlines, followed by standard-library
module boundaries and networking only after those runtime contracts are
testable.
