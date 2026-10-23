# Tyvera roadmap

Tyvera is an independent, statically typed programming language with
Java-inspired structure, Elixir-inspired concurrency and fault tolerance, and
a C++-inspired focus on performance and controlled system-level access.
Discord is a future library/SDK use case, not a compiler or language feature.
Roadmap items below are plans unless marked implemented.

| Stage | Status | Scope and exit condition |
| --- | --- | --- |
| 1. Core language | Implemented | Primitive types, functions, checked returns, lexical scopes, mutable/immutable bindings, operators, conditionals, `while`, `break` and `continue`, covered by tests. |
| 2. Object system | First slice implemented | Classes, fields, `self`, constructors, methods, interfaces, multiple interface implementation and interface dispatch. Next: generics, then review visibility and inheritance requirements before adding them. |
| 3. Modules | Next | Multi-file compilation, explicit imports, public symbols, duplicate/missing symbol diagnostics and deterministic circular-import handling. |
| 4. Standard library | Planned | Typed `List<T>` and `Map<K,V>`, `for` iteration, environment access, and JSON parse/encode APIs with explicit error results. |
| 5. Actor runtime | Planned | Isolated process state, typed messages, mailboxes, sequenced message handling and lifecycle, with deterministic runtime tests. |
| 6. Supervisors | Planned | Child lifecycle, crash detection, restart and an initial `one_for_one` policy, separate from ordinary recoverable errors. |
| 7. Networking | Planned | Async runtime abstraction, timers, HTTP and WebSocket client support, and event dispatch without Promise-specific language semantics. |
| 8. Native backend | Planned | Separate compiler backend, documented value/resource model, efficient value types and a narrow typed FFI. No unsafe native surface before its ownership and ABI rules are testable. |
| 9. Package system | Planned | Reproducible package metadata, dependency resolution and versioning after modules and the standard library stabilize. |
| 10. Discord SDK | Planned | A normal Tyvera library over modules, JSON, environment access, async I/O, WebSockets and actor lifecycle. No Discord-specific compiler behavior. |

## Near-term sequence

1. Add multi-file modules and imports. This is the immediate next milestone
   because both a standard library and a Discord SDK need independently
   compiled, reusable modules.
2. Implement generic classes and functions, then typed collections and `for`.
3. Add an explicit error-result model and JSON/environment APIs before
   networking, so missing configuration and parse failures remain ordinary,
   testable program outcomes.
4. Define async operations and actor message semantics before any Discord
   gateway client. Add supervisors only after actor lifecycle and failure
   behavior have deterministic tests.
5. Keep backend work independent from the Python interpreter. Establish
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

The object system is usable for domain models, but Tyvera cannot yet import
libraries, store collections, read a token from the environment, parse JSON,
perform asynchronous I/O, or connect over HTTP/WebSocket. It therefore cannot
yet implement or run a real Discord bot. The next deliverable is the module
system, followed by generics and standard-library foundations; an SDK should
come only after networking and error semantics work as ordinary libraries.
