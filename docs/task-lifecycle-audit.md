# Task lifecycle milestone audit

Baseline: `e82a77474f8b98b4b8a1462cfc0ed9e74db53184` (Generic Type System V3).
Its four CI jobs passed. The Arena checkout was fast-forwarded from `42ce272`
with user approval; implementation stays on the Arena branch.

## Existing implementation (before changes)

Reviewed reference task records, all task constructors, FIFO pump, continuation
frames, nested await dispatch, groups/detachment, completion/failure/cancellation,
sleep/timeout settlement, I/O completion, resource cleanup, actor stop/restart,
supervision, interpreter shutdown, CLI signal/lifetime handling; also native
TaskControl/TaskState, groups, cancellation and worker draining. Reviewed async,
actor/supervisor, time/reply, platform/shutdown and native detach tests and the
language, runtime, architecture, backend architecture and roadmap documents.

- Creation immediately registers runnable, sleeping, timed_wait or io_wait work.
  There is no observable separate created/scheduled phase.
- Reference tasks use runnable/running/waiting/sleeping/timed_wait/io_wait/joining
  and completed/failed/cancelled. The pump only dispatches runnable entries.
- Parents join children after their bodies return. Failure propagates immediately
  up ownership, cancelling siblings; explicit cancellation only travels down.
- Groups are owner-local views of direct children, not tasks or ownership scopes.
  Nested groups exist through nested owner tasks. A cancelled member counts as
  terminal, so awaiting a cancelled group succeeds unless a member failed.
- Detach removes the direct parent edge and all that owner's group memberships.
  Descendants remain structured under the detached root. Independent actor
  ownership is not removed. A constructor invoked by a Task can create work
  with both actor ownership and a Task parent. Detached failures are scheduler
  failures.
- Timeout wrappers use the ordinary cancellation operation on their source.
  Source terminal state is checked before expiry; terminal sources are preserved.
- runTasks reports newly failed work once per pump; tryResult is an observation,
  not a catch/recovery construct. Native I/O continuations can consume failures.
- Shutdown cancels all unfinished roots including detached work and clears
  queues/timers. The interpreter is one-shot. Host I/O workers are drained.
- Existing interpreter FIFO/fairness tests pass (41 async tests at baseline).
  Native implementation already uses bounded host workers; this milestone must
  not replace either architecture or add threads/event loops.

## Gaps found / verification needed

- `_complete_task` lacks the terminal-state guard present in cancel/fail/I/O
  completion. A return expression can cancel its executing owner and then return,
  overwriting cancellation with completion.
- Reentrant cancellation during async condition/await evaluation may clear frames
  that the continuation then mutates or resurrects into waiting state.
- A synchronous helper can keep executing statements after cancelling its owner;
  it needs the same statement/loop interruption checkpoints as other task work.
- Cancellation leaves stale timer metadata and failed native I/O continuations
  do not share cancellation's I/O unregistration path.
- Top-down join settlement can leave an ancestor in joining after its last
  timer-only descendant completes, causing a false deadlock with no next turn.
- Most nested-group cancellation, simultaneous-failure, detached-descendant and
  exact deadline/completion ordering boundaries are implicit, not covered by
  deterministic state assertions. Add controlled-clock tests rather than a new
  scheduler or public state API.

The normative rules and tested boundary ordering are in runtime.md. This audit
records the starting analysis, not a claim that every native interleaving is
identical to the serial reference scheduler.

## Implemented closure

Terminal completion guards and an internal cooperative interruption unwind now
preserve failure/cancellation across returns and control/await evaluation.
Synchronous statement/loop checks also observe task interruption. Cancellation
and failure share wait disposal (I/O unregister, timeout source cancellation,
clearing deadline and native wait metadata, closing unconsumed wait resources).
Joining owners are settled bottom-up in task registration order, so timer-only
ownership chains reach their final outcome before deadlock detection.

`tests/test_task_lifecycle.py` adds 24 deterministic tests, using injected time
and explicit callback delivery. Language-level tests inspect tasks before
shutdown, so runtime cleanup cannot hide leftover structured work. Existing
async fairness, actor/supervisor, resource and shutdown tests are retained.
No new public states, threads, event loops or language features were added.

## Native fairness check

The complete local run exposed the existing native actor-reply fairness test
failing (`reply delivered` after the spinner's final `19`). Re-running with the
unchanged `e82a774` interpreter and unchanged native backend reproduced the same
failure, despite the green Generic-V3 CI. The native await implementation
released serialized execution before registering the waiter and reserving an
actor-pump handoff. Also, statement checkpoints unlocked, called host yield,
and only then requeued: during that gap a runnable peer could overtake the
absent participant. Publishing the wait/handoff before release and atomically
requeueing under the existing execution mutex close both gaps. Cancelled queued
participants leave the queue without pretending to reacquire the mutex.

The existing 41-execution fairness test passed three consecutive invocations
(123 native runs), as did FIFO replies, timeout-wrapper cancellation and the
concurrent coordinator deadline test. Two generated-runtime ordering assertions
guard registration-before-unlock and requeue-before-release. This changes no
worker limits, introduces no thread, and does not replace the native scheduler.

Local test prerequisites were repaired outside tracked sources: editable
bootstrap installation for subprocess CLI imports, and matching OpenSSL 3.0.20
headers in the excluded local cache, using the system shared libraries. Early
aborted runs lacking those prerequisites are not counted as full-suite results.

## Validation

Final local validation on Python 3.11/Linux x86-64 with the system C++17
compiler: `python -m compileall -q tyvera tests` and
`python -m unittest discover -v` succeeded. The complete run executed 579 tests
in 1088.109 seconds, with no failures, errors or skips. This includes 24 new
reference lifecycle tests, two native ordering tests, and all existing native
fairness, actor/supervisor, resources, networking, deadlines and shutdown tests.
The targeted reference/interoperation set also passed 154 tests, and the native
stress check passed six invocations, including three 41-run fairness checks.

Known boundaries remain cooperative reference execution (no preemption of a
host call), native system DNS/worker drain latency, no aggregate task-error or
catch/recovery scope API, and nonportable native progress counts and wall-clock
interleavings. No license change: Tyvera remains GPLv3.

`examples/resource_sample.txt` was clean in the provided checkout, not locally
modified as in the earlier external working copy. Its content blob remained
`ee40f79ad6bddbee44edd225066d64b8b44e9c3c` throughout; it was never staged or
changed by this milestone.
