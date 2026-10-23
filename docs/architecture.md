# Tyvera bootstrap architecture

## Current pipeline

A source file passes through four explicit stages:

1. `lexer.py` converts characters into tokens with source locations.
2. `parser.py` builds the Tyvera-specific AST in `ast.py`.
3. `typesys.py` collects and validates declarations, expressions, calls,
   constructors, interface contracts and the entry point before execution.
4. `interpreter.py` evaluates only a successfully checked AST, including
   Tyvera-owned object instances, actor references, mailbox scheduling and
   method dispatch.

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
- **Host language is an implementation detail:** Python hosts the first
  toolchain. Tyvera programs do not inherit Python syntax, libraries, dynamic
  typing or object model.
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
- Generic function and class signatures retain type parameters in the checked
  type representation. Calls infer parameters from actual arguments or accept
  explicit type arguments; recursive substitution validates nested
  applications before execution. Generic methods, interfaces and constraints
  are not supported. The interpreter erases type arguments in object records;
  runtime dispatch still uses the statically selected nominal class methods.
- `List<T>` and `Map<K,V>` expose Tyvera-specific typed APIs through private
  bootstrap value records. The checker owns their method signatures and
  iteration element types. A `for` evaluates its collection once and the
  runtime iterates an immutable snapshot, so mutation during iteration is
  deterministic.
- `Option<T>` and `Result<T,E>` are tagged Tyvera values. `match` checks
  literal and variant pattern types, arm scopes, duplicate coverage, and
  exhaustiveness for `Bool`, `Option` and `Result`. These ordinary error values
  remain separate from runtime diagnostics and actor crashes.
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
API currently supports only `one_for_one`. The task scheduler is cooperative,
single-threaded and statement-stepped: an async function call creates a Task,
and direct top-level `await` statements suspend it while other tasks run. Nested
control-flow blocks and synchronous calls are atomic. Tasks preserve their
statically declared result type; cancellation is not propagated through
parent/child ownership. `runTasks()` drives deadlines and may wait for timers,
but async I/O, HTTP, WebSockets and
external secret stores are not implemented. Request deadlines do not remove
queued messages or interrupt handlers.
These boundaries are reflected in the
[language reference](language.md) and [roadmap](roadmap.md).

## Task runtime boundary

The first task slice uses explicit statement continuations: each Task stores
its function frame and top-level statement index. This keeps the bootstrap
interpreter independent of Python coroutine semantics. `await` is restricted
to direct function-body statements because nested branches and loops currently
execute recursively and do not expose resumable frames. Typed task results work
at supported await points; nested awaits, structured cancellation, child-task
ownership, cancellation propagation, and non-blocking I/O remain design work.
`runTasks()` advances tasks round-robin and waits only when every active task is suspended on a
monotonic deadline or actor reply. It can report deadlock when no runnable or
timed work can make progress.

## Native backend direction

The C++ influence is an architectural goal, not a current performance or
interop claim. A separate compiler pipeline should retain the lexer, parser,
AST, semantic analysis and diagnostics, then lower checked programs to a
Tyvera IR shared by the interpreter and a future native backend. No IR is
introduced yet: the current interpreter consumes the checked AST directly,
and there is no second backend that would justify maintaining another
representation. At the native milestone, define explicit value layouts,
checked integer/float semantics, deterministic resource handles and a narrow,
typed FFI boundary before selecting an ABI. Evaluate LLVM or a comparable
backend against these requirements without making it a bootstrap dependency.
Safe Tyvera should not expose raw pointers; any future unsafe/native boundary
must be explicit and isolate ownership and foreign error mapping. Keep the
interpreter as a conformance reference while native output is introduced.

## Local bootstrap benchmarks

`benchmarks/benchmark_bootstrap.py` provides non-gating measurements for
lexing, parsing, type checking, the full check pipeline, interpretation, a
500-element list workload and a 500-message actor scheduler workload. Run it
after installing the editable package:

```sh
python benchmarks/benchmark_bootstrap.py --iterations 10
```

It reports median wall-clock time and Python version. Results are exploratory
and environment-dependent; they are not correctness tests or CI thresholds.
Keep benchmark workloads stable when comparing revisions.
