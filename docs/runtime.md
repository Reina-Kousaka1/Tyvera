# Runtime semantics

Runtime behavior in this document is implemented unless marked planned.
Python is the host for the current reference interpreter; it is not part of
Tyvera's language contract.

## Runtime lifetime

An `Interpreter` instance is one Tyvera runtime. It moves through
`ready -> running -> shutting_down -> closed`. `run()` is one-shot. It closes
the runtime whether `main` returns normally or raises a diagnostic.

The task scheduler is `idle` between `runTasks()` calls and `running` while it
drives work. It returns to `idle` when work completes, deadlocks, or reaches a
safety limit. The scheduler closes with the runtime. A program may call
`runTasks()` more than once while `main` is still running.

At runtime shutdown:

- Every unfinished task is cancelled, including detached roots and tasks that
  were created but never pumped by `runTasks()`.
- Every live actor is stopped. Its mailbox and scheduled messages are cleared,
  and outstanding replies fail with a shutdown error.
- Live supervisors are stopped and their children are stopped.
- No task or actor work survives the interpreter runtime.

Shutdown cleanup does not replace the diagnostic that caused `main` to fail.
Cancellation is cooperative: it clears suspended continuations and cancels
descendants, but cannot interrupt a synchronous function, actor handler, or
expression while it is executing. Discarding a continuation closes all owned
resources in reverse acquisition order; cleanup itself does not suspend.

## Task ownership

| Creator | Owner | Normal completion | Cancellation and failure |
| --- | --- | --- | --- |
| Synchronous code in `main` | Runtime root | The scheduler runs it only when `runTasks()` is called. Runtime shutdown cancels unfinished work. | Runtime shutdown cancels it. A root failure is returned from `runTasks()`. |
| An async task | Creating task | The parent joins every unfinished child before it completes. | Parent cancellation/failure cancels descendants. A child failure fails its owners and cancels sibling work. |
| An actor constructor or message handler | Actor instance | It runs as a root task when `runTasks()` pumps it. | Actor stop, crash, supervisor restart, or runtime shutdown cancels the actor's unfinished tasks. |
| Explicitly detached child | Runtime root | It no longer delays the former parent or appears in that parent's task groups. `runTasks()` still drives it. | It is no longer cancelled by its former owner; runtime shutdown still cancels it. Its failure remains visible in `runTasks()`. |

`TaskGroup` is a view of registered direct children, not an ownership transfer.
Each group has one owner. Nested groups join their own direct members; their
owner task still joins all of its children. Cancelling a member cancels its
descendants. Failing a member fails its owner.

Actor tasks are bound to the actor instance that created them. A supervised
restart cancels tasks from the failed instance before the actor is rebuilt.
Tasks created by an actor-owned task are its structured children. Explicitly
detaching one of those children removes that child from the task owner, so it
can finish independently of the actor; runtime shutdown remains the final
boundary. Resource parameters moved into a task remain owned by that task
until it completes, fails, is cancelled, or runtime shutdown cancels it.

Actor handlers and constructors are synchronous. They may start an async task,
but cannot await it or pump either scheduler from inside the actor turn. The
actor owns such work until stop, failure, restart, or runtime shutdown.

## Scheduling and idle behavior

Tasks execute serially, one continuation action at a time, in FIFO round-robin
order. Synchronous calls and expressions are atomic. If a task is waiting for
an actor reply and actor messages are queued, the task scheduler delivers one
actor message between task turns. This lets reply-producing actors progress
even while other tasks remain runnable. When no task can run, the scheduler
services one queued actor message for a pending reply, then checks deadlines
again.

`runTasks()` waits on the nearest monotonic deadline or a registered I/O
readiness/completion event when no other useful work is runnable. Socket waits
are selector-backed; worker completion wakes the same wait path. The scheduler
does not poll with sleep loops. A task deadlock or an unresolved reply with no
queued actor work, I/O operation, or deadline returns `Err`. The program may
make more work available and call `runTasks()` again; if `main` ends first,
shutdown cancels all remaining tasks. The scheduler has a 100,000-step safety
limit. `runActors()` separately drains actor messages up to its 100,000-message
limit and never waits for a future timer.

`runTasks()` reports task continuation steps on success. Actor message delivery
does not contribute to that count. Neither scheduler provides parallel
execution. Continuous synchronous work can delay all other work because it
cannot be preempted.

The interpreter accepts an injected monotonic clock and sleeper. Runtime and
deadline tests use those hooks to advance time deterministically without
waiting on wall-clock timers.

## Runtime errors

Task failures retain their Tyvera diagnostic and propagate through task
ownership. `tryResult()` observes a task's terminal result; `runTasks()` also
returns `Err` when task work fails, including a detached task failure. An
expected `Result::Err` value is not a task failure. Scheduler deadlock and
safety-limit errors are returned as `Err` and do not interrupt the current
Tyvera statement.

Actor message failures crash only that actor unless a supervisor restarts it.
Stop, crash, restart, request cancellation, and request deadlines settle
outstanding reply receivers. Request cancellation does not remove a queued
message or interrupt a running handler.

## Supervisor V2

A supervisor owns its children in start order. Children may be actors or other
supervisors; a nested supervisor can have only one parent. The implemented
strategies choose the restart set as follows:

| Strategy | A child fails | Stop order | Restart order |
| --- | --- | --- | --- |
| `oneForOne` | Failed child only | That child | That child |
| `oneForAll` | Every child | Reverse start order | Start order |
| `restForOne` | Failed child and all children started after it | Reverse start order within that suffix | Original start order within that suffix |

`permanent` children restart after failures and normal stop; `transient`
children restart after failures only; `temporary` children never restart. A
temporary child affected by a sibling's restart is stopped and omitted from the
new child set. A temporary actor crash is returned from `runActors()` as `Err`
but does not fail its supervisor. A normal stop of a permanent child requests
the configured restart strategy; a normal transient/temporary stop does not.

Each supervisor has a shared sliding restart-intensity window across its
children. The monotonic clock is sampled when a restart is scheduled, and
entries strictly older than the window are discarded; an entry on the exact
boundary still counts. If the restart limit is exceeded, that supervisor is
marked failed, its live subtree stops in reverse order, and the failure is
offered to its parent. A parent then applies its own child policy and strategy
to the failed nested supervisor. A root escalation is reported by `runActors()`
as `Err`.

`exponentialBackoff(base, maximum)` uses deterministic doubling by restart
number, capped at `maximum`. Delayed restarts are scheduler deadlines, not
sleep loops. `runActors()` waits for a pending restart when asked to drain the
actor system; `runTasks()` services the same deadlines while continuing other
runnable work. The clock and sleeper are injectable for tests.

The embedding API can receive structured `RuntimeEvent` values for actor and
supervisor start, stop, crash, restart, escalation, dead letter, and runtime
shutdown. Crash reasons have a typed kind and optional source location; event
sink exceptions are ignored. Dead-letter diagnostics are retained in a bounded
ring of 1,024 entries. A send to an inactive actor also returns a controlled
runtime diagnostic. There is no Tyvera-level link/monitor API yet.

## Host signal and process shutdown

`tyvera run` converts SIGINT/SIGTERM into a cooperative shutdown request.
Handlers only record the first signal and wake the I/O selector. The runtime
observes the request at statement, loop, task, actor, and I/O safe points;
it does not interrupt synchronous code mid-expression. Shutdown cancels tasks,
stops root supervisor trees in reverse child order, stops remaining unsupervised
actors, closes resources through normal scope cleanup, cancels timers, and
waits for the bounded I/O worker pool. Resource cleanup failure is appended to
the original shutdown diagnostic. Exit status is 130 for SIGINT and 143 for
SIGTERM; ordinary Tyvera diagnostics remain status 1. See [host platform
support](platform.md) for the host boundary and its limits.

## Resource cleanup

`TextFile`, `TextReader`, `TextWriter`, `TcpConnection`, `TcpListener`, and
`HttpServer`
handles are move-only. `Option`, `Result`, and resource-returning `Task` values
may carry them; other aggregate and collection types reject them. Owned
handles close in reverse acquisition order at block exit, function return,
runtime failure, `break`, `continue`, task completion or failure, cancellation,
actor-handler failure, and supervised restart. Explicit `close()` makes later
cleanup a no-op. Cleanup errors retain source locations and are added to an
existing diagnostic without replacing its primary cause. Detached tasks keep
their own resource handles until completion or runtime shutdown. A completed
resource result is transferred once through `await` or consuming `tryResult()`;
an unclaimed task result is closed during owner or runtime cleanup. A
resource-returning task is itself move-only while pending, so leaving its scope
cancels it. `tryResult()` consumes that task; if it is still pending, it cancels
the operation and returns `Ok(None)` so a resource cannot be produced without
an owner.

Async file operations use the same move-only file handles as synchronous file
I/O. A handle permits one in-flight worker operation. Scope cleanup marks a
busy handle for close and the worker completion path closes it after the
operation returns. Cancellation stops a queued worker operation where
possible; a worker operation already running completes before its owned file
is closed. Runtime shutdown waits for the bounded worker pool to drain.

TCP connect, accept, read, and write are scheduler I/O operations. A waiting
task is in `io_wait`; the platform adapter registers selector readiness and
returns the awakened operation to the scheduler. Cancellation unregisters the
wait. Closing a connection also cancels any pending read/write with a
controlled `Result::Err`. The task scheduler can continue other tasks while a
socket is waiting. `read(maxBytes)` returns a partial byte chunk or `None` at
EOF; one read is capped at 1 MiB. `write` reports partial progress and
`writeAll` retains one supplied buffer and waits for each further writable
event, so Tyvera does not add an unbounded producer queue.

Expected DNS, socket and file failures are `Result<T,String>` values with
normalized messages; host exceptions are not part of Tyvera's API. Task
timeouts reuse `Task.withTimeout(Duration)`: expiry cancels the I/O task and
fails the timeout wrapper with `"task timed out"`. The `runTasks()` caller can
observe that state with `tryResult()`. The reference adapter currently uses
selectors and a bounded worker pool, but those mechanisms do not define the
language contract.

The native Linux subset has a separate implementation boundary. `async fn`,
`await`, and `runTasks()` use a bounded registry of at most 4,096 Task values
per call. Completed values remain registered until the call ends; at most 64
registered tasks can be unfinished at once. This includes tasks registered
before `runTasks()` starts. Task bodies are serialized; each pending DNS or
socket operation runs in a worker so another Tyvera task can proceed. DNS uses
the platform resolver and cannot be interrupted while that call is in progress.
Sockets are
non-blocking and wait through `poll`; only one accept, read, or write may be
pending on a given resource at a time. A read can be partial and EOF is
`Ok(None)`. `writeAll` retains one bounded `Bytes` value while it waits.
Closing a socket resource shuts down its descriptor; an active operation owns a
duplicate descriptor and completes with a controlled resource-closed result.
Resource-containing task results move out when awaited, and scope cleanup uses
the native RAII resource handle.

The experimental HTTP server creates one owned task per accepted connection,
with a configured cap from `HttpLimits.maxConnections`. Request header and body
reads share one monotonic request deadline, so sending occasional bytes does
not extend a slow-client deadline indefinitely. Writes use the same configured
per-operation timeout and the existing bounded `writeAll` readiness path. A
read timeout produces a controlled HTTP 408 response; other network and parse
errors close that connection without crashing the server task. `shutdown()`
closes the listener and joins active connection tasks; `close()` cancels the
server task tree. Runtime or actor cancellation follows the ordinary owned
task/resource cleanup path. See [HTTP foundation](http.md) for the protocol
limits and client lifecycle.

## Native runtime boundary

The experimental native compiler executes a checked synchronous core plus
selected value features: `Option`/`Result` and matching, `List`/`Map`, `Byte`,
immutable `Bytes`, bounded `ByteBuffer`, opaque `Json` parsing/construction/
access/stringification, synchronous text/binary/path I/O, and owned text-file
resources. JSON rejects documents over 16 MiB and nesting beyond 128
containers identically in both runtimes. On Linux it also provides an experimental async
task subset with DNS and TCP connect/listen/accept/read/write, RAII socket
handles, and cooperative SIGINT/SIGTERM shutdown. The C++ runtime remains
private and exposes no public ABI. Native builds reject unsupported calls;
they never fall back to the reference interpreter. See
[Backend Architecture](backend-architecture.md).

## Not implemented

The reference scheduler remains cooperative and single-threaded. The native
Linux subset uses OS worker threads for tasks and pending network I/O, but
serializes Tyvera task bodies so shared mutable values are not accessed in
parallel. Native task cancellation is cooperative at statement/loop and socket
poll checkpoints; owned children are cancelled on owner cancellation/failure
and joined before owner completion. A blocking native DNS lookup cannot be
interrupted mid-call. Native task groups and actor-owned tasks remain
unsupported. The experimental native C++17 actor subset supports typed
fire-and-forget messages, FIFO delivery per actor, round-robin actor turns,
`stop`, `isAlive`, bounded mailboxes, and `runActors()` error results. The
native supervisor subset supports nested trees, `oneForOne`, `oneForAll`,
`restForOne`, all child restart policies, restart-intensity windows, and
monotonic exponential backoff. Restarts stop affected children in reverse
start order and reconstruct them in start order. Backoff deadlines and actor
turns are serviced by `runActors()`; native `runTasks()` does not yet pump
actors or settle their delayed restarts. Native `request` and `requestWithin`
produce typed ReplyReceivers; readiness, cancellation, timeout observation, and
`tryReceive()` work when the caller explicitly pumps messages with
`runActors()`. Awaiting a ReplyReceiver, `sendAfter` timers, monitoring/links,
actor-owned Tasks, and supervised actors owning Resources or Tasks remain
unsupported natively. Generated actor references use shared
runtime handles; queued callbacks hold weak target references, and runtime
shutdown clears mailboxes, supervisors, and actor state to break reference
cycles. The native runtime retains spawned actors until process shutdown even
when user `ActorRef` values leave scope. Calling `runActors()` or `runTasks()`
from a native actor handler raises the same controlled runtime failure as the
reference runtime. The Linux native backend also has experimental HTTP and
WebSocket client subsets; native HTTP server, router/general codec, and
WebSocket server remain unsupported. Native Linux TLS client operations use
the same task cancellation and monotonic-deadline behavior as the supported
TCP operations.
Native `runTasks()` reports completed task values; the reference runtime
reports continuation steps, so that progress count is not portable. The
reference runtime provides TCP, verified TLS/HTTPS, bounded WebSockets, async
text-file operations, and experimental HTTP/1.1. See the
[memory model](memory-model.md) and
[backend architecture](backend-architecture.md).

