# Tyvera language redesign — B + ABC + Modula-3 + Go

Status: **experimental design proposal; not implemented**.

This proposal changes Tyvera's intended language influences. It does not claim that the current parser, type checker, interpreter, or native backend implement these constructs. The existing language and tests remain authoritative until individual changes are implemented and verified.

## Direction

Tyvera remains an independent, statically typed language rather than a dialect or amalgamation of its inspirations.

| Influence | Intended contribution | Explicit non-goal |
| --- | --- | --- |
| B | Small, predictable core and systems-oriented simplicity | Reintroducing B's untyped memory model |
| ABC | Readable expressions and approachable collections | Adopting ABC syntax wholesale or losing static types |
| Modula-3 | Explicit modules, interfaces, nominal contracts, exceptions | Requiring the historic Modula-3 runtime |
| Go | Lightweight concurrency, typed channels, straightforward build workflow | Go syntax or runtime compatibility |

The former Java/Elixir/C++ design framing is superseded **for this experimental proposal only**. Existing actor/supervisor functionality is not removed merely because the inspiration changes. No TypeScript is added.

## Memory and safety

1. Managed memory is the default for objects and collections; the runtime owns reclamation.
2. Explicitly scoped low-level memory operations may be introduced under an `unsafe` capability, but must not bypass checked safe-code boundaries.
3. A raw allocation has one explicit owner and deterministic release; double-free and use-after-free must not be possible from safe code.
4. Managed references cannot point into manually freed regions without a checked lifetime mechanism.
5. Existing move-only file, TCP, and HTTP resources keep their ownership semantics until a reviewed migration exists.
6. The implementation strategy for GC, FFI, pinning, and allocator ABI is open; no particular collector is promised yet.

## Modules and types

- Preserve static checking before execution, nominal interfaces, generic functions, records, enums, Option/Result and explicit imports as migration baselines.
- Explore Modula-3-style module interfaces and implementation separation without assuming old source syntax is retained.
- Explore ABC-inspired readable collection syntax while preserving deterministic typing and diagnostics.
- Define compatibility/migration rules for existing `.tyv` programs before replacing syntax.

## Concurrency

- Preserve existing typed actor messages, actor isolation, supervision and structured tasks as functional baselines.
- Investigate Go-inspired typed channels and select-like waiting as additional primitives, not replacements by default.
- Specify cancellation, backpressure, fairness, shutdown, and cross-task mutable-state safety before implementation.
- Do not claim goroutines or parallel execution: the current reference tasks are cooperative.

## Illustrative syntax only (not valid current Tyvera)

```text
module Molly

interface Service {
    fn start() -> Result<Void, Error>
}

fn main() -> Void {
    let inbox = channel<String>(capacity: 32)
    spawn {
        inbox.send("hello")
    }
    println(await inbox.receive())
}
```

## Implementation sequence and acceptance gates

1. **Baseline:** record existing CLI behavior, examples, test counts, CI state, branch/PR state and native-backend support. No regressions.
2. **Specification:** decide grammar, module boundaries, memory safety and channel semantics. Document incompatible changes explicitly.
3. **Frontend:** implement lexer/parser/AST changes behind explicit experimental syntax or a version gate; add positive and negative parser tests.
4. **Type checker:** add module contracts, channel types, and safety restrictions with compile-fail tests.
5. **Reference runtime:** implement channel scheduling, cancellation and memory rules; add deterministic conformance tests.
6. **Native backend:** only advertise supported constructs after generated executables pass cross-backend tests; do not silently fall back to Python.
7. **Migration:** update examples, docs, tooling and release notes after compatibility decisions; keep legacy code functioning until a documented break is approved.

## Repository safety

- This is a design-only first step; no runtime, compiler, existing tests, or production services are modified here.
- Do not merge PR #1, force-push, delete branches, or publish a release as part of this proposal.
- Future implementation requires incremental commits with actual test results and explicit reporting of unsupported features.
