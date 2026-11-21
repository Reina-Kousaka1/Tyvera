# Tyvera architecture decisions

Tyvera is an independent language influenced by B, ABC, Modula-3, and Go.
Those languages are design references, not compatibility targets.

## Selected direction

- **Implementation language:** Rust, edition 2024. The compiler itself is a
  native executable; the scalar machine-code backend uses Cranelift.
- **Compilation pipeline:** UTF-8 source and byte spans → tokens → AST →
  name/module resolution → static type checking → typed IR → native backend.
  Each stage owns its diagnostics and consumes explicit input from the prior
  stage.
- **Native backend:** Cranelift is the code-generation backend. The JIT runs a
  typed scalar IR in memory, and the AOT path emits host-format relocatable
  object files. A minimal Windows x64 linker path produces console
  executables; other targets and runtime support remain future work.
- **Runtime memory:** non-moving generational tracing for ordinary safe heap
  objects, deterministic scoped ownership for external/linear resources, and
  explicit unsafe boundaries for raw allocation, pinning, and FFI.
- **Concurrency:** typed tasks and channels, structured child-task ownership,
  cancellation propagation, and a cooperative scheduler before any parallel
  scheduler is considered.
- **Modules:** one module declaration per source file, explicit imports, and
  `pub` exports. The resolver loads transitive source files relative to their
  importers in dependency-first order, rejects cycles and duplicate module
  names, and confines resolved paths to the entry directory tree. Calls to
  public functions with module-independent signatures are type-checked;
  imported types, constants, re-exports, and code generation across modules
  remain pending.
- **Standard library:** small, layered packages for core values, collections,
  text, errors, I/O, and task/channel primitives. Platform services stay
  behind portable interfaces.

## Front-end crate boundaries

The current Rust crate keeps source spans and diagnostics independent from
syntax. `lexer` owns tokenization, `ast` owns syntax data, and `parser` builds
the AST using a Pratt expression parser. `resolver` loads source files and
records import edges. `typecheck` validates local declarations and eligible
public cross-module function calls. `ir` lowers checked source graphs to
unique function symbols, and `native` uses Cranelift for JIT or object code.
The graph path still lacks imported types, constants, re-exports, and runtime
values.

## Current status and gates

The branch starts from the clean reset commit, preserving the repository's
history and GPLv3 license. The current implementation includes the documented
syntax, lexer, parser, AST, source graph loader, local type checker, and
cross-module checking for public functions with module-independent signatures.
A Cranelift JIT and the object/AOT paths lower all modules to unique function
symbols and support the scalar subset (integer, float and Boolean values,
direct calls, local assignment, branches, and loops), with root `fn main() ->
Int` as the entry point. A Windows x64 linker path produces console
executables. Other target/linker combinations and the integrated language
runtime are not implemented yet.

`src/runtime/gc.rs` now contains an early precise, non-moving heap prototype:
typed fields, generation-checked handles, a remembered set for old-to-young
stores, minor/full collection, and survivor promotion. The native IR does not
allocate these objects yet, and deterministic ownership for external resources
has not been implemented.

`src/resolver.rs` loads and parses a source import graph. The type checker and
scalar native back end resolve eligible public function calls across that
graph. Use `tyvera modules <entry.tyv>` to inspect dependency order.

Before native compilation is called complete, Tyvera must type-check programs,
lower them to a documented typed IR, link the runtime, generate a native
executable, and pass checked-in end-to-end tests on supported targets.

## Deferred decisions

- Generic declaration syntax and constraints
- Interface/trait method dispatch and contracts
- Error propagation syntax and ABI
- Exact tracing-collector safepoints and concurrent collection policy
- The syntax and static guarantees of resource scopes and `unsafe`
- Channel buffering, close behavior, fairness, and cancellation races
- Package manifest and cross-module build graph
- FFI ABI and target support matrix
