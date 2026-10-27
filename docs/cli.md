# Tyvera CLI reference

## Supported commands

| Command | Behavior |
| --- | --- |
| `tyvera --version` | Print the installed toolchain version. |
| `tyvera new NAME` | Create `NAME/tyvera.toml`, `src/main.tyv`, a second imported module, `.gitignore` and `.tyvera.secrets.example`. Refuses to replace an existing path. |
| `tyvera check PATH` | Load and type-check a project directory or `.tyv` source file without running `main`. |
| `tyvera run PATH` | Load, type-check and run a project directory or `.tyv` source file. |
| `tyvera build PATH [-o OUTPUT]` | Compile the supported native subset to a host executable using an installed C++17 compiler. |

For a project directory, `PATH` must contain `tyvera.toml`. For a source file,
Tyvera uses the nearest parent manifest when present; otherwise the file's
parent directory is the project root. The manifest supports only two quoted
string fields today:

```toml
name = "my_app"
entry = "src/main.tyv"
```

Without an explicit path, `run` and `check` use the current directory as a
project. Package commands, `test`, and `fmt` are not implemented yet.

## Native build V1 (experimental)

`tyvera build program.tyv` emits a native executable named after the source
file in the current directory (`.exe` on Windows). `-o`/`--output` selects a
different path. For a project directory, the executable uses the project
directory name. A successful build produces machine code through a host
compiler; the output does not launch Tyvera's Python interpreter and does not
need Python at runtime. The host must have `clang++`, `g++`, `c++`, or `cl`
available, or the `CXX` environment variable must name a C++17 compiler.

The native subset currently includes synchronous functions over `Int`,
`Float`, `Bool`, `String`, `Byte`, `Bytes`, `Option`, `Result`, `List`, and
`Map`, with typed matching for `Option`/`Result`; local `let`/`var`,
assignments, calls, returns, arithmetic, comparisons, boolean short-circuiting,
`if`/`else`, `while`, `for` over collection snapshots, `break`/`continue`,
`println`, and `eprintln`. Native programs also expose normalized host facts,
monotonic milliseconds, and cooperative SIGINT/SIGTERM shutdown. Binary file
I/O, `ByteBuffer`, resources, generic functions, Tasks, Actors, TCP, DNS, TLS,
WebSockets, HTTP, and user-defined object/value types remain unsupported by
native builds. A source program that type-checks but
uses a feature outside the subset fails with a source-located backend
diagnostic and produces no executable. The full support table and runtime
boundary are in [Backend Architecture](backend-architecture.md).

V1 targets the host platform selected by its C++ compiler. Linux x86-64 is the
CI-tested native target. Other host compilers use the same generated C++17
boundary but are not yet covered by project CI. Cross-compilation, build
profiles, native FFI, and a stable public ABI are not implemented.

