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
- **No formatter yet:** Statement separation and a compact indentation convention are documented, but a formatter would lock in style before the grammar has stabilized. It should be introduced with the next syntax expansion and its own idempotence tests.

## Current limits

The 0.1 implementation accepts one source file, has only primitive types and local immutable bindings, and has no modules or package manager. Calls are synchronous and execute in one process. These limits are reflected in the language reference rather than hidden behind unimplemented commands.
