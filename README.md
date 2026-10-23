# Tyvera

Tyvera is an independent, statically typed programming language with a design direction informed by Java's structure, Elixir's resilient concurrency model, and C++'s performance and system-level capabilities. It is not an extension or fork of those languages. TypeScript is not part of the current Tyvera design.

## Core language bootstrap

The repository contains a working bootstrap toolchain: a lexer, parser and AST, static type checker, source-located diagnostics, and interpreter. It uses Python only as the current implementation host; Tyvera syntax and semantics are defined separately.

Supported and tested: `Int`, `Float`, `Bool`, `String`, `Void`; functions and checked returns; lexical block scopes and nested shadowing; immutable `let` and mutable `var`; assignment; arithmetic, comparisons and short-circuit `&&`/`||`; `if`/`else if`/`else`; and `while` with `break`/`continue`. Non-`Void` functions cannot fall through without returning a correctly typed value. `Unit`, `and`, and `or` remain compatibility spellings.

Requirements: Python 3.10 or newer. The toolchain has no third-party runtime dependencies.

```sh
python -m pip install -e .
tyvera run hello.tyv
```

Expected output:

```text
Hello from Tyvera!
```

Check a program without running it:

```sh
tyvera check hello.tyv
tyvera check examples/score.tyv
tyvera run examples/score.tyv
```

Run the test suite from the repository root:

```sh
python -m unittest discover -v
```

The detailed supported grammar and type rules are in [the language reference](docs/language.md). The staged toolchain and recorded architecture decisions are in [the architecture note](docs/architecture.md).

## Direction

The next planned milestone is the Java-inspired object and type system: classes and interfaces, followed later by generics. Pattern matching, isolated actors/processes, message passing, supervision and fault recovery, and controlled native/performance-oriented functionality remain later roadmap items. None of these future features are implemented yet.
