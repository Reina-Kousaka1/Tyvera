# Tyvera bootstrap architecture

## Current pipeline

A source file passes through the frontend and selected backend:

1. `lexer.py` converts characters into tokens with source locations.
2. `parser.py` builds the Tyvera-specific AST in `ast.py`.
3. `typesys.py` collects and validates declarations, expressions, calls,
   constructors, interface contracts and the entry point before execution.
4. `compiler.py` returns a `CheckedProgram` after semantic analysis. The
   current top-level function signature table is retained; inferred expression
   types and most resolved semantic facts are not.
5. `backend.py` defines the `ExecutionBackend` boundary. The CLI currently
   selects `ReferenceInterpreterBackend`, which delegates to `interpreter.py`.

`source.py` owns shared file/line/column locations and user-facing diagnostics.
`modules.py` resolves relative imports, checks visibility and links source
modules before the shared type checker runs. `project.py` reads the small
manifest and creates starter projects. `cli.py` provides `new`, `run`, `check`
and `--version`. The implementation uses Python's standard library only.

## Design decisions

- **Own source model:** Grammar, type names, operators, AST and diagnostics are
  Tyvera-specific. No Java, Elixir, C++, TypeScript or Python parser/runtime is
  embedded.
- **Check before running:** The complete reachable module graph is parsed and type-checked before
  `main` can print anything. Static errors are deterministic and cannot be
  hidden by earlier execution effects.
- **Defined numeric widths:** `Int` uses a checked signed 64-bit range and
  `Float` uses finite binary64 values. The interpreter reports overflow rather
  than inheriting Python's arbitrary-precision integer behavior.
- **Interpreter first:** The AST interpreter makes `.tyv` programs executable
  without a native compiler, VM or external build tools. Lexer, AST, checker
  and CLI boundaries leave room for a future bytecode or native backend. This
  implementation makes no C++-level performance or native access claim.
- **Python is the reference implementation:** Python is the bootstrap
  implementation, current interpreter host, development tool, and conformance
  reference for Tyvera behavior. It is not part of the language contract or
  the planned native program runtime. Python-specific dependencies remain at
  collection hashing, JSON conversion, object lifetime, exceptions, and host
  I/O boundaries; see the [Memory Model](memory-model.md).
- **Execution follows semantic analysis:** The lexer, parser, module loader,
  and type checker have no execution-backend dependency. The CLI passes a
  `CheckedProgram` to `ExecutionBackend`; no native implementation exists.
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
- Generic function, class, record, enum and newtype signatures retain type
  parameters in the checked type representation. Calls infer parameters from
  actual arguments or accept explicit type arguments; recursive substitution
  validates nested applications before execution. Generic functions support
  interface bounds with statically checked constrained method calls. Generic
  methods/interfaces and bounds on generic type declarations are not supported.
  The interpreter erases type arguments in runtime values; dispatch still uses
  statically selected nominal methods.
- `List<T>` and `Map<K,V>` expose Tyvera-specific typed APIs through private
  bootstrap value records. The checker owns their method signatures and
  iteration element types. A `for` evaluates its collection once and the
  runtime iterates an immutable snapshot, so mutation during iteration is
  deterministic. Map storage uses a Tyvera-owned hash/equality contract and
  the checker accepts only recursively hashable value types or stable identity
  references as keys. No Python hash value is observable. See the
  [Memory Model](memory-model.md#implemented-reference-behavior).
- `Option<T>`, `Result<T,E>`, enums, records, tuples and newtypes are typed
  values. Equality is value-based for immutable structures, collection-based
  for lists/maps, and identity-based for classes and runtime handles. `match`
  checks nested pattern types, arm scopes, subsumed cases and recursive
  exhaustiveness across finite sum and product patterns. CheckedProgram records
  the recursive exhaustiveness result and top-level fully covered cases. These
  ordinary error values remain separate from runtime diagnostics and actor
  crashes.
- Actor declarations have a checked message protocol and private state. The
  bootstrap scheduler owns FIFO mailboxes and schedules one message per actor
  per round. It is cooperative and deterministic, not parallel. Messages are
  restricted to transferable immutable primitives, nested `Option`/`Result`
  values, durations and actor references; ordinary mutable objects cannot be
  shared. A final `ReplySender<T>` handler parameter is the one controlled
  single-assignment capability exception. `ActorRef.request` creates its
  receiver and enqueues the typed request. Receivers poll without blocking,
  support cancellation and deadlines, and are failed on actor crash or stop.
  Handler diagnostics crash only their actor and clear its mailbox. An
  unsupervised crash is reported with source location by `runActors()` as
  `Result<Int,String>` while other actors continue. A `one_for_one` Supervisor
  can restart only the failed child from its initial constructor arguments;
  restart intensity is bounded by a monotonic time window and a maximum of
  1,000 attempts. Exhaustion stops the supervisor's children and is reported as
  `Err`. Successful recovery is returned as `Ok`; the child's old state and
  queued messages are discarded. Mailboxes and each scheduler drain have a
  100,000 message limit; a nonterminating handler can still block the
  cooperative loop.
- **Time is observable and injectable:** `Duration` uses checked signed 64-bit
  milliseconds. `Timer` stores a monotonic deadline and can be queried or
  cancelled. `ActorRef.sendAfter` uses it to schedule one typed actor message;
  `runActors()` promotes due messages but never sleeps or wakes itself. The
  interpreter's clock injection keeps time-dependent tests deterministic.
- JSON uses a Tyvera-owned opaque `Json` value and checked constructors,
  accessors and `Result`-returning parse/encode intrinsics. The bootstrap uses
  Python's standard JSON codec internally, but rejects duplicate keys,
  non-finite values, out-of-range integers and invalid surrogate values; host
  dictionaries and arbitrary Tyvera instances never cross the language API.
- Text I/O returns `Result` values and path operations are explicit core
  functions. `SecretProvider` reads project-local secrets first and falls back
  to the process environment. The interpreter wraps loaded values in a
  redacted `Secret<String>`; `println`/`eprintln` reject the wrapper, and only
  `reveal()` removes it. Generic text I/O blocks the configured local secrets
  file from reads and writes.
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

Each module is parsed separately and its references are resolved to stable
module-qualified names before the existing semantic checker sees the linked
program. Imports are relative `.tyv` files, aliases are explicit, `pub` is
required for cross-module access, and paths cannot escape the project root.
Actors participate in that linker and can be exported and imported. The
manifest currently has only `name` and `entry`; dependencies are not yet
supported. Ordinary functions and I/O execute synchronously; actor handlers
run only when the cooperative scheduler is explicitly pumped. The supervisor
API currently supports only `one_for_one`.

The task scheduler uses explicit block and loop continuation frames rather
than Python coroutines. It supports nested `await`, structured child ownership,
owner-local task groups, explicit detachment, typed deadlines, and detached
failure reporting. The runtime is cooperative and single-threaded. Actors own
tasks created by their constructors and handlers; stop, crash, restart, and
runtime shutdown cancel that actor-owned work. Runtime shutdown closes actors,
supervisors, timers, and all unfinished tasks. When a task awaits an actor
reply, the scheduler interleaves a bounded actor turn with task steps to avoid
starving that reply. Synchronous calls and individual expressions remain
atomic. Full lifecycle rules are in the [Runtime documentation](runtime.md).

## Native backend direction

No native backend or Tyvera IR exists. The CLI now hands a `CheckedProgram` to
`ExecutionBackend`, with `ReferenceInterpreterBackend` as the only
implementation. The current representation retains the checked AST and
top-level function signatures, not the expression-level type and ownership
facts that a native backend will need. A shared IR is deferred until a second
consumer or a real lowering step establishes its purpose. The runtime ABI,
initial target plan, native backend comparison, and `tyvera build` acceptance
criteria are in [Backend Architecture](backend-architecture.md). Tyvera's
value and resource ownership rules, including the current `TextFile` slice,
are documented in the [Memory Model](memory-model.md).

## Local bootstrap benchmarks

`benchmarks/benchmark_bootstrap.py` provides non-gating measurements for
lexing, parsing, type checking, the full check pipeline, interpretation, a
500-element list workload, a 500-message actor scheduler workload, and a
500-child task-group workload. Run it after installing the editable package:

```sh
python benchmarks/benchmark_bootstrap.py --iterations 10
```

It reports median wall-clock time and Python version. Results are exploratory
and environment-dependent; they are not correctness tests or CI thresholds.
Keep benchmark workloads stable when comparing revisions.

