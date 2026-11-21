# Tyvera reboot: design charter

Status: initial design, not a language specification or implementation.

## Goals

1. Independent statically typed language with a small coherent core.
2. Readable syntax and explicit modular interfaces.
3. Typed, structured concurrency (channels/tasks), with defined cancellation and shutdown.
4. Managed memory by default; explicit, restricted low-level memory operations.
5. Native execution as a long-term goal; no backend selected yet.
6. Portable tooling and reproducible tests, including Linux/ARM64 and Windows as targets.

## Inspirations, not dependencies

- B: minimalism and system-level perspective, **not** its untyped semantics.
- ABC: approachable source notation and collection ergonomics.
- Modula-3: module boundaries, nominal interfaces, static type contracts.
- Go: concurrency and practical tooling, **not** source or binary compatibility.

## Decisions pending

- Exact grammar and file/module naming
- Primitive types, generics, sum types and error model
- GC design, FFI boundaries, unsafe-memory ownership and pinning
- Channel semantics, task scheduling and data-race prevention
- Compiler pipeline, IR, native code generation and bootstrapping language
- Package/build conventions and standard-library surface

## First implementation milestones

1. Publish grammar and semantic test cases.
2. Implement new lexer and parser, including source-location diagnostics.
3. Implement new AST and type checker.
4. Implement an executable reference runtime or compiler, with conformance tests.
5. Add typed tasks/channels, memory safety and platform APIs incrementally.

All old implementation files are intentionally absent from this reboot branch. Do not import legacy code as the basis for the rewrite. Preserve the GPLv3 license and Git history.
