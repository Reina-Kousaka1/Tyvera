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
  `pub` exports. Resolution will be deterministic and independent of the
  current working directory.
- **Standard library:** small, layered packages for core values, collections,
  text, errors, I/O, and task/channel primitives. Platform services stay
  behind portable interfaces.

## Front-end crate boundaries

The current Rust crate keeps source spans and diagnostics independent from
syntax. `lexer` owns tokenization, `ast` owns syntax data, and `parser` builds
the AST using a Pratt expression parser. `typecheck` validates a single-module
subset and records expression types. `ir` lowers the scalar subset and `native`
uses Cranelift to execute it. Future `resolve`, expanded `types`, AOT
`codegen`, and `runtime` stages must not reach back into source text to infer
facts that an earlier stage should retain.

## Current status and gates

The branch starts from the clean reset commit, preserving the repository's
history and GPLv3 license. The current implementation includes the documented
syntax, lexer, parser, AST, parse/lex CLI, and an initial type checker for
single-module programs. The checker resolves local declarations, aliases,
struct fields, enum constructors, function calls, local bindings, and core
expression types. It does not resolve imported modules or generic declarations.
A Cranelift JIT runs the supported scalar subset (integer, float and Boolean
values, direct calls, local assignment, branches, and loops) with `fn main() ->
Int` as the entry point. `tyvera object` emits relocatable objects for that
subset. `tyvera build` links standalone Windows x64 console executables through
the installed MSVC linker and Windows SDK. Other target/linker combinations and
the language runtime are not implemented yet.

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
