# Backend architecture and native compilation

This document separates the reference path, the experimental native compiler
subset, and planned runtime/ABI work. Tyvera remains licensed under GPLv3.

## Implemented pipeline

```text
Tyvera source
  -> lexer and parser
  -> Tyvera AST
  -> module linking and semantic analysis
  -> CheckedProgram
       -> tyvera run -> ReferenceInterpreterBackend -> Python-hosted runtime
       -> tyvera build -> typed IR -> C++17 generator -> host C++ compiler -> executable
```

`compile_checked_source()` and `compile_checked_project()` return a
`CheckedProgram` only after type checking succeeds. It contains the AST,
function signatures, expression types, generic substitutions, match coverage,
resource kinds, move sites, cleanup scopes, and ownership transfers. `tyvera
run` selects `ReferenceInterpreterBackend`; `tyvera build` lowers only the
supported subset to a separate native path. `parser.py` and `typesys.py` do
not import or depend on execution code.

Async I/O intrinsics are checked as typed task results and use the same
`CheckedProgram.resource_types`, move sites, cleanup points, and ownership
transfers as files. `TcpConnection`, `TlsConnection`, and `TcpListener` are move-only resources;
task results carrying them are consumed once. The reference runtime's
`tyvera/io_backend.py` is an internal platform adapter for readiness
registration, worker completion, and wakeup. It does not expose Python socket
objects or Python errors to Tyvera programs.

`CheckedProgram` is still not a complete typed representation. It does not
retain resolved member symbols, full class/interface layouts, or match
lowering facts. The native subset uses the expression types and checked
function signatures already recorded by the type checker. Its IR makes local
binding identity, scalar type, calls, source positions, and structured control
flow explicit. Unsupported nodes fail with a source-located backend diagnostic.

## Typed IR V1 and native task slice

`tyvera/ir.py` defines a small backend-neutral IR: typed scalar literals and
local references, direct function calls, unary/binary operations, local
declarations and assignments, output, returns, lexical blocks, `if`, `while`,
`break`, and `continue`. The typed value subset also includes opaque `Json`,
its parse/stringify constructors and accessors, structural comparison, and
`Json`-keyed maps. Every node retains its Tyvera type and source
location. Locals have unique identifiers, so shadowing and mutation do not
depend on C++ name lookup. `validate_ir()` checks calls, operator/result types,
binding scopes and mutability, entry-point shape, loop-control placement, and
`Task<T>`/`await` placement. `Task<Void>` is the IR form of a void async
function.

`tyvera/lowering.py` consumes only a checked program; it does not re-run type
inference. The IR supports async functions, direct task calls, `await`, `runTasks`,
and the supported TCP/DNS/TLS, HTTP client, and WebSocket client value and
method signatures. Native task cancellation and monotonic deadlines are
implemented for the supported task subset. A limited native actor core is
lowered for actor state, typed fire-and-forget messages, and actor lifecycle
calls. Nested supervisors, actor request/reply, restart policies, and cancellation of
actor-owned Tasks use the native runtime. Actor timers, monitoring/links, task
groups, supervised ownership of move-only Resources, HTTP
servers/routers/decoders, classes, interfaces, and generic functions remain
rejected by `tyvera build`.

## Three separate contracts

### Language semantics

Defines observable Tyvera behavior: integer overflow, numeric promotion,
equality, collection order, actor isolation, task cancellation, diagnostics,
and resource cleanup. It must not depend on the host language or target ABI.

### Runtime services

Provide the operations required by a Tyvera program: allocation and object
identity, strings, collections, actor mailboxes, task scheduling, timers,
diagnostics, I/O, and resource cleanup. These services implement language
semantics; their internal data structures may vary by platform.

### I/O platform boundary

The Tyvera-facing contract is typed tasks plus move-only resource handles,
partial byte operations, EOF, cancellation, deadlines, normalized errors,
bounded pending operations, and deterministic cleanup. The reference adapter
uses `selectors.DefaultSelector` for TCP and a bounded worker pool for DNS and
file work. Native C++17 now has an experimental Linux-only async slice for
`Task<T>`, `await`, `runTasks`, DNS, and TCP. Tyvera task bodies are serialized;
blocking DNS/socket waits run in separate workers so they do not hold the task
execution lock. Each `runTasks()` call accepts at most 4,096 registered
native Task values in total, including completed values retained until the
call ends, and at most 64 unfinished values at a time. The unfinished limit
includes tasks registered before `runTasks()` starts. Linux sockets use
private RAII handles. Each in-flight operation duplicates the descriptor;
closing its owner shuts down the shared socket, wakes the wait, and releases
the duplicate on task completion. Resource-returning tasks move their result
out on `await`, so an awaited connection or listener is not retained by the
completed task. Read, write, and accept slots are reserved when operations are
created, making duplicate pending operations fail consistently with the
reference runtime. Expected network failures use the existing typed
`Result<T,String>` contract and normalized strings. Reads are capped at 1 MiB;
`write` can be partial and `writeAll` loops under socket backpressure.
`Task.cancel()`, `isCancelled()`, and `isDone()` are implemented cooperatively.
Tasks created by an async task are owned children: cancellation and failure
cascade through descendants, normal return joins children, and a child failure
fails its owner. Cancellation is checked at Tyvera statement/loop checkpoints
and during bounded socket polling; an in-progress system DNS lookup cannot be
interrupted until the resolver returns. Native `Task.withTimeout(Duration)` uses a
steady monotonic deadline, cancels its source operation, and waits for source
cleanup before reporting timeout; completion published at the deadline wins.
`tryResult()` polls without starting work: non-`Void` tasks return `Result<Option<T>,String>`, while `Task<Void>` returns `Result<Bool,String>`. Ordinary results remain available for repeated observation. Polling a resource task consumes its handle, transfers a completed result once, or cancels a pending acquisition; cancellation cleanup remains cooperative. Native TaskGroups provide owner-bound direct-child membership, cancellation, and joining. Actor-created tasks are tied to their actor instance. Lifecycle changes cancel and drain other owned tasks before clearing or rebuilding actor state; a task that stops its own actor unwinds cooperatively at its next interruption point. Native `runTasks()` currently
reports completed native tasks, while the reference runtime reports
continuation steps; that progress count is not portable yet. The native C++17
subset also implements synchronous file/path/ByteBuffer APIs. Neither backend
exposes host socket objects or memory addresses.

The bootstrap also has a separate host-platform adapter for normalized OS and
architecture detection, monotonic time, secret-wrapped environment lookup,
cooperative signal shutdown, and argv-based child-process management. Ubuntu
x86-64 is the explicit Linux CI reference and continues to run the native C++17
tests. The platform adapter does not make Linux-specific details part of the
language, and it is not linked into generated native programs. CI also runs
the complete Python 3.13/native suite on an Ubuntu 24.04 AArch64 host. See
[Host platform foundation](platform.md).

The networking layer has no user-defined general Stream interface. TCP
establishes a byte-stream contract; text files retain their UTF-8 resource API.
The reference interpreter provides the complete experimental HTTP/1.1 codec,
client/server, and bounded WebSocket client over owned TCP/TLS streams. The
native C++17 backend now has an experimental Linux client slice for HTTP/1.1,
WebSocket, TCP, DNS, and verified TLS. Native HTTP supports typed method, URL,
header, request, and response values plus `httpGet`, `httpPost`, `httpPut`,
`httpDelete`, and `httpSend`; it parses bounded Content-Length, chunked, and
close-delimited responses and closes each connection after one request. The
native WebSocket client supports bounded `ws://` and verified `wss://`
connections, masked client frames, text/binary messages, ping/pong, fragmented
messages, and close handling. HTTP server/router/decoder and WebSocket server
remain reference-only. TLS uses platform trust paths, SNI, and hostname/IP
verification; TLS 1.2 or later is required and no insecure mode is exposed.
Native TLS builds link host OpenSSL libraries required at build and runtime.
Native host names are ASCII DNS names or IP literals; unsupported forms produce
controlled errors. Tyvera does not implement cryptography. HTTP/2, HTTP/3, TLS
server mode, database protocols, and other application protocols remain
separate work.

### Backend implementation

Consumes checked semantic information, lowers it to a selected code generator,
selects target calling conventions and object formats, emits debug data, and
links a runtime for the chosen target. A backend must not redefine a Tyvera
operator or silently substitute host-language error behavior.

## Native runtime and ABI boundary

Native Backend V1 emits C++17 and invokes a host compiler. Its internal
generated-code representation is:

- `Int`: `std::int64_t` with checked signed 64-bit arithmetic.
- `Float`: finite IEEE-754 binary64 (`double`); overflow and division by zero
  become Tyvera runtime diagnostics.
- `Bool`: C++ `bool`; `String`: `std::string` containing UTF-8 bytes; `Void`:
  C++ `void`.
- `Byte`: `std::uint8_t`; `Bytes`: an immutable Tyvera byte value represented
  privately as `std::vector<std::uint8_t>`.
- `ByteBuffer`: private shared storage for zero-initialized managed bytes,
  capped at 67,108,864 bytes. `length`, `get`, `set`, and `toBytes` preserve
  the checked Tyvera API; snapshots copy into immutable `Bytes`.
- `Json`: a private tagged value with exact `Int`, finite `Float`, UTF-8
  `String`, array, and insertion-ordered object payloads. The native parser
  rejects malformed input, duplicate keys, invalid surrogate escapes,
  out-of-range integers, non-finite numbers, documents over 16 MiB, and nesting
  beyond 128 containers with the same `Result` error as the reference runtime.
  Constructors and accessors preserve Tyvera's typed API; object equality is
  structural and independent of insertion order.
- `TextFile`, `TextReader`, and `TextWriter`: private stream handles. Checked
  move analysis still permits one Tyvera owner. Generated cleanup scopes close
  handles in reverse acquisition order; C++ RAII is an implementation detail.
- I/O errors use `Result<T,String>`. C++ exceptions are caught at the runtime
  boundary and normalized to Tyvera errors or source-located diagnostics.
- Relative file paths use the project directory embedded by `tyvera build`.
  Canonical path checks protect `.tyvera.secrets` through direct, traversal,
  absolute, and symlink paths.
- `Option<T>` and `Result<T,E>` use `std::optional<T>` and an indexed
  `std::variant<T,E>` in generated code. `List<T>` and `Map<K,V>` use private
  shared runtime storage so assignment and calls preserve Tyvera's shared
  collection-reference semantics.
- Direct generated functions pass these scalar values by value. The
  executable links the platform C++ runtime and small generated support code.
- Runtime failures retain source file, line, and column and become a controlled
  stderr diagnostic plus a nonzero process exit.

This is a private V1 code-generation contract, not a stable public C ABI or an
FFI. These representations are private compiler details, not a public ABI.
Generated executables do not embed or invoke Python. Synchronous file and
binary I/O, `ByteBuffer`, `Json`, text-file resources, and experimental Linux
async TCP/DNS/TLS, HTTP-client, and WebSocket-client paths are supported. Native
JSON parse, construction, access, comparison, and encoding are supported for
the typed subset; automatic record/enum codecs and an importable `tyvera.json`
module remain unimplemented. The
experimental native actor/supervisor subset supports stateful actor
declarations, `spawn`, typed fire-and-forget messages, `stop`, `isAlive`,
`runActors()`, nested supervisors, all three restart strategies and child
policies, restart-intensity windows, and monotonic exponential backoff.
Actor fields cannot own Resources or Task handles; handler-created Tasks are
owned by their actor instance and drained on lifecycle transitions. Non-generic immutable records and
user-defined enums now lower as nominal native values, including construction,
field access, equality, enum matching, and imported package declarations.
Generic user-defined types, tuples, classes, actor timers,
HTTP servers/routers/decoders, and WebSocket servers remain interpreter-only.
Native monitor/demonitor and symmetric link/unlink share the reference
lifecycle rules, including transitive crash propagation and grouped supervisor
restarts.
Native actor turns and delayed restarts are driven by `runActors()`; pending
request replies can also drive one actor turn from `runTasks()`. Actor-owned
Tasks are cancelled on stop, crash, restart, and runtime shutdown. TaskGroups
preserve owner binding. The Task scheduler also services monotonic supervisor
restart deadlines while other tasks wait; exact task-body interleaving remains
backend-dependent. There is no
`unsafe` or FFI declaration syntax. Any later FFI must define ownership,
lifetimes, mutability, and error conversion explicitly; a native crash cannot
become a recoverable Tyvera `Result`. See the [Memory Model](memory-model.md)
for Tyvera's value and resource rules.

## Native backend options

The comparison below is an architecture assessment, not a performance claim.
It was reviewed on 2026-10-23 against the upstream project documentation
linked at the end of this section.

| Option | Target fit | Optimization and compile time | Debugging | Toolchain, FFI, and runtime integration | GPLv3 and bootstrap fit |
| --- | --- | --- | --- | --- | --- |
| LLVM | Strong fit for all four required targets and object formats: COFF for Windows x64, ELF for Linux x64/ARM64, Mach-O for macOS ARM64. macOS x64 is also available in LLVM's x86 targets. | Broad target optimizers and target selection. Building, packaging, or finding a compatible LLVM toolchain is substantial; Tyvera compile-time impact is unmeasured. | Source-level debug metadata can lower to DWARF and Windows CodeView. Tyvera still has to describe its own source values and locations accurately. | Mature object/linker ecosystem. Cross compilation still needs the target sysroot, libraries, assembler/linker, and system integration. C ABI is available but must not become Tyvera's semantic model. | LLVM states that its Apache-2.0-with-LLVM-exceptions license can be combined with GPLv3. A Python bootstrap can emit IR or invoke a tool initially, but shipping a stable LLVM toolchain is a separate packaging decision. |
| Cranelift | Strong ISA fit for Windows x64, Linux x64, Linux ARM64, and macOS ARM64; the project documents Windows x64 and System V x64 conventions, Linux AArch64, and Apple Silicon support. | Designed as a relatively fast, simpler code generator. No Tyvera compile-time or optimization comparison exists. | Object generation exists; source-level interactive debugging is less established than LLVM's cross-platform debug metadata and needs an early prototype. | AOT native object output exists. Cranelift is a Rust library, so a Python bootstrap would need a Rust helper or a later native Tyvera compiler. Runtime/FFI integration remains Tyvera work. | The upstream crates use Apache-2.0 WITH LLVM-exception. Check exact dependency and redistribution obligations for the full compiler distribution. |
| C/C++ transpilation | Depends on a host compiler; cross targets still need matching sysroots, libraries, and linkers. | Fast route to a small executable slice; machine code and optimization are delegated to the installed compiler. No Tyvera performance results exist. | Compiler debug information is available, but source-level mappings need deliberate work. | C has a smaller runtime surface; C++ offers standard strings and a straightforward private helper runtime. Both need an external toolchain. | Toolchain licensing depends on the compiler. Generated executables must not require Python. |
| Own native backend | No target is implemented. Each OS/architecture combination needs instruction selection, calling convention, object format, linker/debug integration, and runtime testing. | Full control, but optimization passes, register allocation, instruction selection, and ABI support would become Tyvera's responsibility. | DWARF, CodeView/PDB, and Mach-O debug integration would need to be built and maintained. | Maximum integration control and maximum maintenance burden; a typed FFI and runtime still remain necessary. | No third-party backend license issue, but it does not simplify GPLv3 distribution or the Python-bootstrap transition. Not realistic as the first backend. |

### Target matrix

| Target | LLVM | Cranelift | C/C++ transpilation |
| --- | --- | --- | --- |
| Windows x86-64 | Supported target/object path; Windows toolchain and runtime linking still required. | x86-64 backend and Windows x64 calling convention documented. | Possible with MSVC or Clang plus Windows headers/libraries; not guaranteed by Tyvera. |
| Linux x86-64 | Supported target/object path. | x86-64 System V backend documented. | CI-tested with the host C++17 compiler. |
| Linux ARM64 | Supported AArch64/ELF target path. | AArch64 Linux convention documented. | Possible with a matching cross toolchain/sysroot. |
| macOS ARM64 | Supported AArch64/Mach-O target path. | Apple Silicon support documented. | Possible with an Apple-targeting toolchain and SDK. |
| macOS x86-64 | LLVM has x86 Mach-O support; retain only if the user base justifies it. | Not required; validate Darwin ABI/object support before promising it. | Possible with a matching Apple toolchain and SDK. |

Target instruction support is not the same as a complete cross compiler. Each
target also needs its runtime, linker, system libraries, debug format, and CI
coverage. Mobile and WebAssembly are not required targets, but the semantic
and backend boundaries should not hard-code the four initial targets.

## Backend choice for V1

Native Backend V1 uses C++17 source generation followed by a host C++ compiler.
This is the smallest path from the Python frontend and checked IR to real
machine code. It avoids shipping LLVM or a Rust helper and keeps the native
runtime separate from the Python interpreter. Checked integer operations,
finite Float behavior, Tyvera string output, and source-located runtime errors
are implemented in generated support code. It requires a C++17 compiler at
build time; no speed or optimization claim is made.

LLVM remains a candidate for broader target selection, optimization, and debug
information. Cranelift remains a candidate if an in-process Rust code generator
becomes practical. A custom machine-code backend remains unjustified. Revisit
the choice as the native feature set grows; using C++ as the bootstrap target
does not make C++ part of Tyvera's language.

## Native support boundary

| Area | Status in Native V1 |
| --- | --- |
| `Int`, `Float`, `Bool`, `String`, `Void`; locals and assignments | Implemented in typed IR and C++17 backend. |
| Synchronous functions, calls, returns, recursion | Implemented. |
| Arithmetic, numeric comparisons, exact mixed `Int`/`Float` comparison, boolean short-circuiting | Implemented with Tyvera overflow, finite-Float, division, and remainder rules. |
| `if`/`else`, `while`, lexical scopes, `break`, `continue` | Implemented. |
| `println`, `eprintln` | Implemented for Int, Float, Bool, String, and Byte. |
| `Option<T>`, `Result<T,E>` | Implemented for native-supported payload types, including constructors, typed calls/returns, equality, and nested values. |
| Pattern matching | Implemented for scalar literal patterns and nested Option/Result constructor patterns with binding/wildcard cases; source exhaustiveness rules remain enforced by the checker. |
| `List<T>`, `Map<K,V>` | Implemented for native-supported element/key/value types, including mutation, lookup, removal, equality, length, and snapshot iteration. Map lookup preserves Tyvera equality and insertion-order iteration semantics. |
| `Byte`, immutable `Bytes` | `byteFromInt`, `Byte.toInt`, Byte equality/output, `bytesFromString`, Bytes equality, length, bounds-checked `get`/`slice`, and UTF-8-checked `toString` are implemented. |
| `ByteBuffer` | Shared mutable 64 MiB bounded buffer; checked allocation, length, get/set, and immutable `toBytes` snapshots use `Result` errors. |
| Synchronous file/path I/O | `readText`, `writeText`, `readBytes`, `writeBytes`, `pathJoin`, and `pathExists`; relative paths use the project root and `.tyvera.secrets` is protected. |
| Text resources | `TextFile`, `TextReader`, `TextWriter`, `using`, move transfer, `Result`/function transfer, synchronous methods, modes, close/isOpen, and deterministic LIFO cleanup are implemented. |
| Non-generic records and user-defined enums | Experimental native nominal values: immutable records, tagged enum payloads, constructors, field access, equality, and checked nested matching. Linked package declarations are lowered together. Generic nominal types and recursive layouts are rejected with a backend diagnostic. |
| Tuples, classes, interfaces, generic functions | Interpreter-only. |
| `Task<T>`, async functions, `await`, `sleepFor`, `runTasks()` | Experimental native subset; ordinary task bodies are serialized, while timer and I/O waits use worker tasks. Cooperative task/child cancellation and monotonic `withTimeout(Duration)`/`sleepFor(Duration)` deadlines are supported. Actor-created tasks are cancelled on actor stop, crash, supervised restart, and runtime shutdown. Other owned tasks drain before actor state is cleared or rebuilt; a task stopping its own actor unwinds cooperatively at its next interruption point. `tryResult()` and owner-bound TaskGroups are supported; `detach()` remains interpreter-only. |
| DNS, TCP, and TLS client | Experimental on native Linux: `resolveHost`, `tcpConnect`, `tcpListen`, `accept`, connection read/write, and verified `tlsConnect` with hostname/IP checks, all as typed `Task<Result<...,String>>` operations. RAII handles close sockets and TLS sessions. |
| HTTP/WebSocket client | Experimental native Linux subset: typed HTTP values and GET/POST/PUT/DELETE/custom request client; bounded HTTP/1.1 responses; bounded `ws`/verified `wss` client. Native HTTP server/router/general codec and WebSocket server remain interpreter-only. |
| Linked source packages | Experimental; supported declarations from relative multi-file imports, including non-generic records/enums, lower with the program. Selected repository-local Discord decoder, Gateway, and REST paths compile and run natively against fake local TLS services without Discord-specific backend lowering. A supervised `GatewayRunner` with an actor-owned Gateway task is covered by a local fake-Gateway integration test; shared REST rate-limit coordination has bounded per-major-scope FIFO tickets and global/bucket waiting. Reusable event callbacks and a general REST service actor remain unimplemented. |
| Actors and `ActorRef<T>` | Experimental native C++17 subset: actor declarations/state, `spawn`, typed fire-and-forget and request/reply (`request`, `requestWithin`, `ReplySender.send`, and `ReplyReceiver` readiness/cancel/`tryReceive`), `stop`, `isAlive`, and bounded `runActors()`. Per-actor FIFO mailboxes use round-robin turns; unresolved replies fail on stop, crash, or shutdown. Actor constructors and handlers can start Tasks bound to that actor instance; other owned tasks drain before actor state is cleared or rebuilt; a task stopping its own actor unwinds cooperatively at its next interruption point. Native async tasks can await a reply, which lets `runTasks()` pump a queued actor turn. |
| Supervisors | Experimental native support for nested trees, `oneForOne`/`oneForAll`/`restForOne`, permanent/transient/temporary policies, monotonic restart windows, deterministic exponential backoff, reverse stop order, and start-order restart. Backoff deadlines progress in `runActors()`. While a task awaits a typed reply, `runTasks()` pumps one queued actor turn and releases serialized task execution until the reply settles; an unresolved unbounded reply with no queued actor work produces a controlled deadlock error. Actor fields cannot contain Resources or Tasks; handler-created Tasks are actor-owned and cancelled on stop, crash, supervised restart, and runtime shutdown. Other owned tasks drain before state is cleared or rebuilt; the current self-stopping task unwinds at its next interruption point. |
| Awaiting a `ReplyReceiver<T>` | Experimental native support inside async functions. `runTasks()` pumps queued actor turns for pending replies; timeout, cancellation, stop/crash failure, and no-work deadlock are controlled. Native monitor/demonitor and link/unlink are supported; `sendAfter` remains interpreter-only. |
| `hostOS()`, `hostArchitecture()`, `monotonicMilliseconds()`, `environment(String)` | Native C++17 runtime and reference implementations; environment results remain `Option<Secret<String>>`, and monotonic time is used for elapsed-time decisions. Project-local `.tyvera.secrets` is still read only by the reference secret provider. |
| Native SIGINT/SIGTERM shutdown | Generated C++17 records the first signal and unwinds at statement/loop safe points; scope resources use RAII cleanup. |
| Structured runtime events | The reference embedding API emits bounded typed events and dead letters. No equivalent public native event sink or stable embedding ABI is exposed yet. |
| Native FFI, stable public ABI, cross-compilation | Planned; no syntax or runtime contract is exposed. |

Unsupported features never fall back to the interpreter. The full source is
type-checked before lowering, and the first unsupported construct produces a
source-located diagnostic without creating an executable.

## `tyvera build` and differential testing

`tyvera build PATH [-o OUTPUT]` is implemented. It lowers after successful
semantic analysis, emits temporary C++17, invokes `CXX` or a discovered
`clang++`, `g++`, `c++`, or `cl`, and replaces the output only after the host
compiler succeeds. Missing toolchains and unsupported source constructs
produce controlled diagnostics. CI builds and runs the native examples on
Linux x86-64 with Python removed from the executable's `PATH`.

End-to-end tests compare stdout, stderr, and exit status against
`ReferenceInterpreterBackend`. The local corpus covers TCP echo, DNS
success/failure, HTTP GET/POST with chunked and malformed responses, verified
HTTPS using a local certificate, and WebSocket echo/close. It also covers
located runtime failures, IR validation, unsupported-feature diagnostics, and
the CLI. This is a focused compiler corpus, not full-language differential
testing. Native networking clients are not claimed for Windows or macOS.
Addresses, host tracebacks, and timing are not semantic outputs.

## Upstream references

- [LLVM supported build/host platforms](https://llvm.org/docs/GettingStarted.html)
- [LLVM code generation and object formats](https://llvm.org/docs/CodeGenerator.html)
- [LLVM source-level debug information](https://llvm.org/docs/SourceLevelDebugging.html)
- [Clang cross-compilation requirements](https://clang.llvm.org/docs/CrossCompilation.html)
- [Cranelift target and calling-convention support](https://github.com/bytecodealliance/wasmtime/blob/main/cranelift/README.md)
- [Cranelift object and JIT crates](https://github.com/bytecodealliance/wasmtime/blob/main/cranelift/docs/index.md)
- [LLVM license and GPLv3 combination statement](https://github.com/llvm/llvm-project/blob/main/llvm/docs/DeveloperPolicy.md#copyright-license-and-patents)
- [Cranelift crate license declaration](https://github.com/bytecodealliance/wasmtime/blob/main/cranelift/umbrella/Cargo.toml)

