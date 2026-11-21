# Tyvera

Tyvera is an independent, statically typed programming language being rebuilt
from the ground up. Its design draws on B, ABC, Modula-3, and Go without aiming
for source or runtime compatibility with them.

## Current implementation

The repository currently contains a tested front end: a UTF-8 lexer,
byte-based source spans, diagnostics, an abstract syntax tree, a Pratt parser,
an initial single-module type checker, and a small `lex`/`parse`/`check`
command-line tool. It accepts the grammar in
[`docs/language-v0.md`](docs/language-v0.md).

This is **not yet a complete compiler**. Name resolution across imported
modules, generic type checking, executable code generation, a runtime, memory
management, concurrency, and the standard library remain to be implemented.
`tyvera parse` checks syntax only. `tyvera check` validates the supported
single-module type rules; it does not claim that a program is runnable.

## Build and use

Requires Rust and Cargo (edition 2024 support).

```powershell
cargo test
cargo run -- parse examples/hello.tyv
cargo run -- check examples/hello.tyv
cargo run -- lex examples/hello.tyv
```

The command-line tool also supports `tyvera --help` and `tyvera --version`.

## Design and status

- [`docs/language-v0.md`](docs/language-v0.md): current syntax draft and
  language commitments
- [`docs/design.md`](docs/design.md): architecture decisions and implementation
  gates
- [`LICENSE`](LICENSE): GNU GPL version 3

The rewrite branch preserves the repository's Git history and GPLv3 license,
but its source tree starts from the reset commit rather than the legacy
implementation. See the design document for the remaining work before Tyvera
can be considered a native compiler and runtime.
