# Tyvera bootstrap architecture

## Current pipeline

A source file passes through four explicit stages:

1. `lexer.py` converts characters into tokens with source locations.
2. `parser.py` builds the Tyvera-specific AST in `ast.py`.
3. `typesys.py` collects and validates declarations, expressions, calls,
   constructors, interface contracts and the entry point before execution.
4. `interpreter.py` evaluates only a successfully checked AST, including
   Tyvera-owned object instances and method dispatch.

`source.py` owns shared file/line/column locations and user-facing diagnostics.
`cli.py` provides `tyvera run` and `tyvera check`. The implementation uses
Python's standard library only.

## Design decisions

- **Own source model:** Grammar, type names, operators, AST and diagnostics are
  Tyvera-specific. No Java, Elixir, C++, TypeScript or Python parser/runtime is
  embedded.
- **Check before running:** The complete file is parsed and type-checked before
  `main` can print anything. Static errors are deterministic and cannot be
  hidden by earlier execution effects.
- **Defined numeric widths:** `Int` uses a checked signed 64-bit range and
  `Float` uses finite binary64 values. The interpreter reports overflow rather
  than inheriting Python's arbitrary-precision integer behavior.
- **Interpreter first:** The AST interpreter makes `.tyv` programs executable
  without a native compiler, VM or external build tools. Lexer, AST, checker
  and CLI boundaries leave room for a future bytecode or native backend. This
  implementation makes no C++-level performance or native access claim.
- **Host language is an implementation detail:** Python hosts the first
  toolchain. Tyvera programs do not inherit Python syntax, libraries, dynamic
  typing or object model.
- **No formatter yet:** Statement separation and indentation conventions are
  documented. A future formatter needs round-trip and idempotence tests.

## Core language and object model

- Dedicated AST nodes represent assignment, scoped blocks, branches, loops,
  loop controls, classes, fields, interfaces, member access and method calls.
  Assignment remains a statement; the precedence parser is expression-only.
- Checker environments contain typed bindings and mutability. Nested scopes
  resolve nearest-first and prohibit duplicates only in the current scope.
- Return analysis composes possible exits (`next`, `return`, `break`,
  `continue`) independently of runtime exceptions or host control flow.
- Interface implementation is nominal: a class lists one or more interfaces,
  and required method names, parameter types and return types must match
  exactly. A conforming class is assignable to its interface. There is no
  class inheritance or structural duck typing.
- Constructors are named `init` and return `Void`. Classes with fields must
  define one. A path-sensitive constructor analysis ensures fields have been
  initialized before a successful return; it checks immutable assignment,
  early field reads and premature escape of `self`.
- Runtime instances use a Tyvera-owned value record containing the nominal type
  and fields. Method dispatch consults Tyvera class declarations. Python
  classes are not exposed as language values.
- Runtime environments mirror the lexical scope topology but hold values.
  Internal return and loop signals unwind nested blocks and are caught only by
  their owning function or loop.
- `Void` is canonical; `Unit` is normalized as a compatibility spelling.
  Numeric promotion, finite `Float` and checked 64-bit `Int` rules are explicit
  Tyvera semantics rather than host defaults.

The compiler currently accepts one source file per program. Calls execute
synchronously in one process. Modules, collections, generics, error handling,
pattern matching, actors, supervisors, async I/O, JSON and environment access
are not implemented. These boundaries are reflected in the
[language reference](language.md) and [roadmap](roadmap.md).

## Native backend direction

The C++ influence is an architectural goal, not a current performance or
interop claim. Keep a future compiler backend separate from the interpreter
and define Tyvera's value, integer-overflow, floating-point and resource
semantics before selecting a target ABI. A native layer should use explicit
ownership or resource handles, value types with documented layout rules and a
narrow, typed FFI boundary. Avoid exposing raw C++ object layouts or host
exceptions as Tyvera semantics. Prototype a backend and ABI after modules,
generics and the core library stabilize; retain the interpreter as the
executable reference implementation during that transition.
