# Tyvera

Tyvera is an independent, statically typed programming language with a design direction informed by Java's structure, Elixir's resilient concurrency model, and C++'s performance and system-level capabilities. It is not an extension or fork of those languages. TypeScript is not part of the current Tyvera design.

## Core language bootstrap

The repository contains a working bootstrap toolchain: a lexer, parser and AST, static type checker, source-located diagnostics, and interpreter. It uses Python only as the current implementation host; Tyvera syntax and semantics are defined separately.

Supported and tested: `Int`, `Float`, `Bool`, `String`, `Void`; generic functions/classes; typed `List<T>` and `Map<K,V>`; `Option<T>`/`Result<T,E>` and statement-level pattern matching; `for` iteration; checked text-file I/O and path helpers; local/environment-backed `Secret<String>` access with explicit reveal; checked returns and lexical scopes; `let`/`var`; arithmetic, comparisons, `if`/`else`, and `while` with `break`/`continue`. Classes support constructors, fields, `self`, methods and checked initialization. Interfaces support exact contracts, multiple implementation and dynamic dispatch. Multi-file projects support explicit relative imports and `pub` exports. `Unit`, `and`, and `or` remain compatibility spellings.

Requirements: Python 3.10 or newer. The toolchain has no third-party runtime dependencies.

```sh
python -m pip install -e .
tyvera run hello.tyv
```

Expected output:

```text
Hello from Tyvera!
```

## Projects and modules

Tyvera supports small multi-file projects. `tyvera new my_app` creates a
`tyvera.toml` manifest and two `.tyv` modules. `tyvera check` and `tyvera run`
accept a project directory or source file. Imports use explicit relative paths
and aliases; only `pub` declarations are visible across modules:

```tyv
import "models/user.tyv" as models

fn main() {
    let user: models.User = models.User("Charlotte")
    println(user.name)
}
```

The loader checks public visibility, missing files/symbols, duplicate names,
cyclic imports and project-root boundaries. `.tyvera.secrets` is gitignored;
`secrets.NAME` reads it before falling back to the process environment.

```sh
tyvera new my_app
tyvera check my_app
tyvera run my_app
tyvera --version
```

Check a program without running it:

```sh
tyvera check hello.tyv
tyvera check examples/score.tyv
tyvera run examples/score.tyv
tyvera check examples/user.tyv
tyvera run examples/user.tyv
tyvera check examples/collections.tyv
tyvera run examples/collections.tyv
```

Run the test suite from the repository root:

```sh
python -m unittest discover -v
```

The detailed supported grammar and type rules are in [the language reference](docs/language.md). Current and planned library APIs are in [the standard library reference](docs/standard-library.md). CLI commands are listed in [the CLI reference](docs/cli.md). The staged toolchain and recorded architecture decisions are in [the architecture note](docs/architecture.md). Planned stages toward a general-purpose bot runtime are in [the roadmap](docs/roadmap.md).

## Direction

The object-system slice, multi-file modules, generics, collections, optional
and result values, pattern matching, text I/O and local/environment-backed
secrets are implemented. Actor runtime, supervision, async/network I/O, JSON,
external secret stores and native backends remain
planned and are not represented as working features. See the
[roadmap](docs/roadmap.md) for sequence and scope.
