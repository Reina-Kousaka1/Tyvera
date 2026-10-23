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
expression while it is executing.

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
boundary.

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

`runTasks()` waits only until the nearest monotonic task, request, or scheduled
message deadline when no other useful work is runnable. A task deadlock or an
unresolved reply with no queued actor work or deadline returns `Err`. The
program may make more work available and call `runTasks()` again; if `main`
ends first, shutdown cancels all remaining tasks. The scheduler has a
100,000-step safety limit. `runActors()` separately drains actor messages up to
its 100,000-message limit and never waits for a future timer.

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

## Not implemented

True parallelism, async I/O, networking, resource scopes, deterministic
cleanup, and native execution remain planned. See the
[memory model](memory-model.md) and
[backend architecture](backend-architecture.md).
