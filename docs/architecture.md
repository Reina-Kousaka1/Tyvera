# Tyvera bootstrap architecture

## Current pipeline

A source file passes through four explicit stages:

1. `lexer.py` converts characters into tokens with source locations.
2. `parser.py` builds the AST in `ast.py`.
3. `typesys.py` validates declarations, expressions, calls, and the entry point before execution.
4. `interpreter.py` evaluates only a successfully checked AST.

`source.py` owns shared file/line/column locations and user-facing diagnostics. `cli.py` provides the `tyvera run` and `tyvera check` commands. The implementation uses Python's standard library only.

## Initial design choices

- **Own source model:** The grammar, type names, operators, AST, and diagnostics are Tyvera-specific. No Java, Elixir, C++, or TypeScript parser/runtime is embedded.
- **Check before running:** The complete file is parsed and type-checked before `main` can print anything. This makes static errors deterministic and prevents a partially executed program from hiding a later type error.
- **Defined numeric widths:** `Int` uses a checked signed 64-bit range and `Float` uses finite binary64 values. The interpreter reports overflow rather than inheriting Python's arbitrary-precision integer behavior or silently exposing host-specific results.
- **Interpreter first:** An AST interpreter makes the smallest useful `.tyv` program executable without requiring a native compiler, VM, or external build tools. The lexer, AST, checker, and CLI boundaries leave room for a future bytecode or native backend. This implementation does not claim C++-level performance or native access.
- **Host language is an implementation detail:** Python is used only to bootstrap the first toolchain because it is available in the execution environment and has no runtime package dependencies here. Tyvera programs do not inherit Python syntax, libraries, or dynamic typing.
- **Concurrency and native systems features are deferred:** Processes/actors, message passing, supervision, fault isolation, classes/interfaces, generics, and controlled native access are explicit design directions, not placeholder APIs in 0.1. The current AST keeps functions and expression/statement nodes distinct so these can be added without changing the CLI contract.
- **No formatter yet:** Statement separation and a compact indentation convention are documented. A future formatter needs its own round-trip and idempotence tests once the core grammar is stable.

## Current limits

The implementation accepts one source file, primitive types, functions, lexical
blocks, immutable and mutable bindings, conditions and loops. It has no modules
or package manager. Calls are synchronous and execute in one process. These
limits are reflected in the language reference.

## Core control-flow decisions

- Dedicated AST nodes represent assignment, scoped blocks, branches, loops and
  loop controls. The precedence parser remains expression-only; assignment is a
  statement. `else if` is an alternative containing a nested conditional.
- Checker environments contain typed bindings with mutability. Nested scopes
  resolve nearest-first and prohibit duplicates only in the current scope;
  parameters are immutable bindings in the function body scope.
- Return analysis composes sets of possible exits (`next`, `return`, `break`,
  `continue`). It handles literal conditions and endless loops but does not
  infer arbitrary expression values. This analysis is independent of runtime
  exceptions or host control flow and can be reused before a compiler backend.
- Runtime environments have the same lexical topology but hold values rather
  than types. Assignment updates the nearest owning scope. Each iteration gets
  a fresh body environment. Internal return/loop signals unwind nested blocks;
  loop signals are caught only by the nearest loop, returns only by a function.
- `Void` is canonical; the initial `Unit` spelling is normalized during type
  checking. Existing numeric promotion, finite Float and checked 64-bit Int
  rules remain explicit Tyvera semantics rather than host defaults.
- Future classes/interfaces can add declarations, type information and AST
  nodes at these boundaries without embedding interpreter behavior in their
  semantics.
