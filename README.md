# Tyvera

Tyvera is an independent, statically typed programming language with a design direction informed by Java's structure, Elixir's resilient concurrency model, and C++'s performance and system-level capabilities. It is not an extension or fork of those languages. TypeScript is not part of the current Tyvera design.

## First runnable milestone

The repository contains a working 0.1 bootstrap toolchain for a small, explicit subset of Tyvera: a lexer, parser and AST, static type checker, source-located diagnostics, and interpreter. It uses Python only as the current implementation host; Tyvera syntax and semantics are defined separately.

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
```

Run the test suite from the repository root:

```sh
python -m unittest discover -v
```

The detailed supported grammar and type rules are in [the language reference](docs/language.md). The staged toolchain and recorded architecture decisions are in [the architecture note](docs/architecture.md).

## Direction

The language is intended to grow toward explicit static types, classes and interfaces, generics, pattern matching, isolated actors/processes, message passing, supervision and fault recovery, and controlled native/performance-oriented functionality. Those features are not claimed to work in 0.1; the current reference describes exactly what the toolchain accepts.
