# Backend architecture and native compilation

This document distinguishes implemented boundaries from planned native
compilation. Tyvera remains licensed under GPLv3.

## Implemented pipeline

```text
Tyvera source
  -> lexer and parser
  -> Tyvera AST
  -> module linking and semantic analysis
  -> CheckedProgram
  -> ExecutionBackend
       -> ReferenceInterpreterBackend
```

`compile_checked_source()` and `compile_checked_project()` return a
`CheckedProgram` only after type checking succeeds. It contains the AST,
function signatures, expression types, generic substitutions, match coverage,
resource kinds, move sites, cleanup scopes, and ownership transfers. The CLI selects
`ReferenceInterpreterBackend`; `parser.py` and `typesys.py` do not import or
depend on execution code.

This is a backend boundary, not a complete typed representation. It does not
retain resolved member symbols, full class/interface layouts, match lowering,
or all mutability and control-flow facts required for code generation. Modules
rewrite cross-module references to qualified names, and the interpreter
consumes the resulting AST directly.

## Typed representation and IR decision

An explicit backend-independent typed AST is not added yet. `CheckedProgram`
marks the successful compiler stage and carries useful semantic metadata, but
does not duplicate all checker state or claim that native codegen-ready type
information is present.

A Tyvera IR would be useful once a second backend or a meaningful lowering
transformation exists. Neither condition holds today. Maintaining an IR now
would mean translating the current AST and then translating it back into the
same interpreter behavior without gaining reuse. Before native codegen, Tyvera
needs an authoritative representation that can retain resolved symbols,
class/interface layout, mutability, pattern-match decisions, actor/task
operations, source locations, and the resource ownership facts required by
lowering. Add IR only with the first
consumer that demonstrates those needs; version it with explicit compatibility
rules and tests.

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

### Backend implementation

Consumes checked semantic information, lowers it to a selected code generator,
selects target calling conventions and object formats, emits debug data, and
links a runtime for the chosen target. A backend must not redefine a Tyvera
operator or silently substitute host-language error behavior.

## Runtime ABI contract (planned)

The ABI is not selected or implemented. Before committing to one, specify:

- Stable layouts and ownership rules for scalar, string, collection, object,
  actor, task, error, and resource handles.
- Allocation/deallocation responsibilities and how out-of-memory is reported.
- String encoding, length, and lifetime rules.
- Collection operations and iteration order without exposing host hash-table
  layout.
- Actor and task creation, scheduling, cancellation, joins, timers, and
  runtime shutdown.
- How source locations and typed diagnostics cross generated/runtime calls.
- I/O services, resource close behavior, and cleanup when a task is cancelled.
- A narrow FFI mapping for Tyvera values, errors, and ownership transfer.

The first ABI should be internal and versioned with the runtime. Safe Tyvera
must not expose raw allocation pointers. A future native/unsafe boundary must
make ownership and foreign error conversion explicit. The language-level
value and resource rules are in the [Memory Model](memory-model.md); the native
resource ABI remains planned.

## Native backend options

The comparison below is an architecture assessment, not a performance claim.
It was reviewed on 2026-10-23 against the upstream project documentation
linked at the end of this section.

| Option | Target fit | Optimization and compile time | Debugging | Toolchain, FFI, and runtime integration | GPLv3 and bootstrap fit |
| --- | --- | --- | --- | --- | --- |
| LLVM | Strong fit for all four required targets and object formats: COFF for Windows x64, ELF for Linux x64/ARM64, Mach-O for macOS ARM64. macOS x64 is also available in LLVM's x86 targets. | Broad target optimizers and target selection. Building, packaging, or finding a compatible LLVM toolchain is substantial; Tyvera compile-time impact is unmeasured. | Source-level debug metadata can lower to DWARF and Windows CodeView. Tyvera still has to describe its own source values and locations accurately. | Mature object/linker ecosystem. Cross compilation still needs the target sysroot, libraries, assembler/linker, and system integration. C ABI is available but must not become Tyvera's semantic model. | LLVM states that its Apache-2.0-with-LLVM-exceptions license can be combined with GPLv3. A Python bootstrap can emit IR or invoke a tool initially, but shipping a stable LLVM toolchain is a separate packaging decision. |
| Cranelift | Strong ISA fit for Windows x64, Linux x64, Linux ARM64, and macOS ARM64; the project documents Windows x64 and System V x64 conventions, Linux AArch64, and Apple Silicon support. | Designed as a relatively fast, simpler code generator. No Tyvera compile-time or optimization comparison exists. | Object generation exists; source-level interactive debugging is less established than LLVM's cross-platform debug metadata and needs an early prototype. | AOT native object output exists. Cranelift is a Rust library, so a Python bootstrap would need a Rust helper or a later native Tyvera compiler. Runtime/FFI integration remains Tyvera work. | The upstream crates use Apache-2.0 WITH LLVM-exception. Check exact dependency and redistribution obligations for the full compiler distribution. |
| C transpilation | Depends on a host C compiler. Clang can target these architectures, but target headers, libraries, sysroots, and linkers must be supplied. | Fastest route to a small end-to-end native experiment; optimization quality is delegated to the installed C compiler. No Tyvera timings exist. | C compiler debug info is useful, but Tyvera source-level names and locations need generated mappings such as line directives and carefully preserved temporary names. | C ABI and system libraries are familiar. Requires an external C toolchain and a Tyvera runtime written or linked as C; C++ adds exception/runtime ABI complexity without an immediate benefit. | Compatible toolchain licensing depends on the chosen compiler. Generated native programs must not require Python. Useful as a temporary bootstrap experiment, not yet a user-facing build guarantee. |
| Own native backend | No target is implemented. Each OS/architecture combination needs instruction selection, calling convention, object format, linker/debug integration, and runtime testing. | Full control, but optimization passes, register allocation, instruction selection, and ABI support would become Tyvera's responsibility. | DWARF, CodeView/PDB, and Mach-O debug integration would need to be built and maintained. | Maximum integration control and maximum maintenance burden; a typed FFI and runtime still remain necessary. | No third-party backend license issue, but it does not simplify GPLv3 distribution or the Python-bootstrap transition. Not realistic as the first backend. |

### Target matrix

| Target | LLVM | Cranelift | C transpilation |
| --- | --- | --- | --- |
| Windows x86-64 | Supported target/object path; Windows toolchain and runtime linking still required. | x86-64 backend and Windows x64 calling convention documented. | Possible with MSVC or Clang plus Windows headers/libraries; not guaranteed by Tyvera. |
| Linux x86-64 | Supported target/object path. | x86-64 System V backend documented. | Possible with a matching C toolchain/sysroot. |
| Linux ARM64 | Supported AArch64/ELF target path. | AArch64 Linux convention documented. | Possible with a matching cross toolchain/sysroot. |
| macOS ARM64 | Supported AArch64/Mach-O target path. | Apple Silicon support documented. | Possible with an Apple-targeting toolchain and SDK. |
| macOS x86-64 | LLVM has x86 Mach-O support; retain only if the user base justifies it. | Not required; validate Darwin ABI/object support before promising it. | Possible with a matching Apple toolchain and SDK. |

Target instruction support is not the same as a complete cross compiler. Each
target also needs its runtime, linker, system libraries, debug format, and CI
coverage. Mobile and WebAssembly are not required targets, but the semantic
and backend boundaries should not hard-code the four initial targets.

## Recommendation

Keep the final backend choice open. LLVM is the strongest first candidate for
the long-term target and debug-information requirements. Cranelift is a serious
alternative if compile latency, code size, or a Rust-hosted compiler becomes a
priority. A C transpiler is useful for a short native-runtime proof because it
can keep the current Python frontend, but its external-toolchain dependency
makes it an incomplete `tyvera build` implementation. A custom machine-code
backend is not justified for the first native milestone.

The next decision should follow small prototypes against the same Tyvera
conformance programs. Measure compile latency and binary size, inspect source
debugging on each target, verify runtime/FFI behavior, and build without Python
on the target. No speed comparison is claimed today.

## `tyvera build` and differential testing

`tyvera build` is planned to produce a real native executable from the project
entry point. It must fail clearly when the target toolchain is unavailable,
and a successful build must run without Python on the target. The command is
not implemented and must not be reported as implemented until an executable
backend exists.

Differential testing is also planned. A shared program corpus should run
through each backend and compare stdout, stderr, exit status, normalized
diagnostics, runtime errors, and other documented observable behavior.
Addresses, host tracebacks, and timing are not semantic outputs. The reference
interpreter is the first oracle; there is no second backend and no
differential test infrastructure yet.

## Upstream references

- [LLVM supported build/host platforms](https://llvm.org/docs/GettingStarted.html)
- [LLVM code generation and object formats](https://llvm.org/docs/CodeGenerator.html)
- [LLVM source-level debug information](https://llvm.org/docs/SourceLevelDebugging.html)
- [Clang cross-compilation requirements](https://clang.llvm.org/docs/CrossCompilation.html)
- [Cranelift target and calling-convention support](https://github.com/bytecodealliance/wasmtime/blob/main/cranelift/README.md)
- [Cranelift object and JIT crates](https://github.com/bytecodealliance/wasmtime/blob/main/cranelift/docs/index.md)
- [LLVM license and GPLv3 combination statement](https://github.com/llvm/llvm-project/blob/main/llvm/docs/DeveloperPolicy.md#copyright-license-and-patents)
- [Cranelift crate license declaration](https://github.com/bytecodealliance/wasmtime/blob/main/cranelift/umbrella/Cargo.toml)
