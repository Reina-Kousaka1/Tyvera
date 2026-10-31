"""Reference lifecycle boundaries: fake time, no threads or real sleeps."""
from __future__ import annotations

import io
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter, _TyveraDuration, _TyveraTask, _TyveraTextFile
from tyvera.source import Location


TERMINAL = {"completed", "failed", "cancelled"}


class TaskLifecycleTests(unittest.TestCase):
    def execute(self, source: str, *, pending_at_shutdown=False):
        now = [0.0]
        waits = []
        def advance(seconds):
            self.assertGreater(seconds, 0)
            waits.append(seconds)
            now[0] += seconds
        program, _ = compile_source(source, "lifecycle.tyv")
        output = io.StringIO()
        before_shutdown = []
        class ObservedInterpreter(Interpreter):
            def _shutdown_runtime(self):
                before_shutdown.extend(task.state for task in self._tasks)
                super()._shutdown_runtime()
        runtime = ObservedInterpreter(program, output, clock=lambda: now[0], sleeper=advance)
        runtime.run()
        if not pending_at_shutdown:
            self.assertTrue(all(state in TERMINAL for state in before_shutdown), before_shutdown)
        else:
            self.assertTrue(any(state not in TERMINAL for state in before_shutdown))
        self.assertTrue(all(t.state in TERMINAL for t in runtime._tasks))
        self.assertFalse(runtime._ready_tasks)
        self.assertFalse(runtime._scheduled_timers)
        for task in runtime._tasks:
            if task.parent is not None:
                self.assertIn(task, task.parent.children)
            self.assertFalse(task.frames)
        return output.getvalue(), runtime, waits

    def runtime(self):
        program, _ = compile_source("fn main() {}", "lifecycle.tyv")
        now = [0.0]
        runtime = Interpreter(program, io.StringIO(), clock=lambda: now[0])
        return runtime, now, Location("lifecycle.tyv", 1, 1)

    def test_return_expression_cannot_resurrect_cancelled_task(self):
        output, runtime, _ = self.execute('''fn cancelOwner(tasks: List<Task<Bool>>) -> Bool {
    match tasks.get(0) {
        Some(task) => return task.cancel()
        None => return false
    }
}
async fn owner(tasks: List<Task<Bool>>) -> Bool { return cancelOwner(tasks) }
fn main() {
    let tasks = List<Task<Bool>>()
    let task = owner(tasks)
    tasks.append(task)
    runTasks()
    println(task.isCancelled())
    println(task.cancel())
}''')
        self.assertEqual(output, "true\nfalse\n")
        self.assertEqual(runtime._tasks[0].state, "cancelled")
        self.assertIsNone(runtime._tasks[0].result)

    def test_sync_helper_stops_at_statement_checkpoint_after_owner_cancel(self):
        output, _, _ = self.execute('''fn interrupt(tasks: List<Task<Void>>) -> Bool {
    match tasks.get(0) {
        Some(task) => { task.cancel(); println("leaked statement") }
        None => {}
    }
    return true
}
async fn owner(tasks: List<Task<Void>>) {
    if interrupt(tasks) { println("leaked branch") }
}
fn main() {
    let tasks = List<Task<Void>>()
    let task = owner(tasks)
    tasks.append(task)
    runTasks()
    println(task.isCancelled())
}''')
        self.assertEqual(output, "true\n")

    def test_cancel_during_await_target_evaluation_does_not_suspend_again(self):
        output, runtime, waits = self.execute('''fn cancelAndReturn(tasks: List<Task<Void>>, pending: Task<Void>) -> Task<Void> {
    match tasks.get(0) {
        Some(task) => { task.cancel() }
        None => {}
    }
    return pending
}
async fn owner(tasks: List<Task<Void>>, pending: Task<Void>) {
    if true { await cancelAndReturn(tasks, pending) }
    println("leaked continuation")
}
fn main() {
    let tasks = List<Task<Void>>()
    let pending = sleepFor(milliseconds(1))
    let task = owner(tasks, pending)
    tasks.append(task)
    runTasks()
    println(task.isCancelled())
}''')
        self.assertEqual(output, "true\n")
        self.assertEqual(runtime._tasks[1].state, "cancelled")

    def test_terminal_completion_is_idempotent(self):
        for state in sorted(TERMINAL):
            with self.subTest(state=state):
                runtime, _, location = self.runtime()
                task = _TyveraTask(None, None, location, state=state, result=17, error="original")
                runtime._complete_task(task)
                runtime._cancel_task(task)
                runtime._fail_task(task, "late failure")
                self.assertEqual((task.state, task.result, task.error), (state, 17, "original"))

    def test_cancel_clears_timer_and_is_idempotent(self):
        runtime, now, location = self.runtime()
        task = runtime._create_sleep_task(_TyveraDuration(10), location)
        runtime._cancel_task(task)
        runtime._cancel_task(task)
        self.assertIsNone(task.due_at)
        now[0] = 99
        runtime._promote_sleeping_tasks()
        self.assertEqual(task.state, "cancelled")
        self.assertIsNone(runtime._next_task_deadline())

    def test_parent_success_joins_unawaited_children_and_grandchildren(self):
        output, runtime, waits = self.execute('''async fn leaf() {
    await sleepFor(milliseconds(2))
    println("leaf")
}
async fn child() { let task = leaf(); println("child body") }
async fn owner() { let task = child(); println("owner body") }
fn main() {
    let task = owner()
    runTasks()
    println(task.isDone())
}''')
        self.assertEqual(output, "owner body\nchild body\nleaf\ntrue\n")
        self.assertTrue(all(t.state == "completed" for t in runtime._tasks))
        self.assertTrue(waits)

    def test_timer_only_join_chain_settles_before_deadlock_detection(self):
        output, runtime, waits = self.execute('''async fn middle() {
    let timer = sleepFor(milliseconds(1))
}
async fn outer() { let child = middle() }
async fn root() { let child = outer() }
fn main() {
    let task = root()
    match runTasks() { Ok(_) => println("joined"); Err(reason) => println(reason) }
    println(task.cancel())
}''')
        self.assertEqual(output, "joined\nfalse\n")
        self.assertTrue(all(task.state == "completed" for task in runtime._tasks))
        self.assertEqual(waits, [.001])

    def test_parent_failure_cancels_waiting_descendants(self):
        output, runtime, waits = self.execute('''async fn child() {
    await sleepFor(seconds(60))
    println("leaked child")
}
async fn owner() {
    let task = child()
    let broken: Float = 1 / 0
}
fn main() {
    let task = owner()
    match runTasks() { Err(_) => println("failed"); Ok(_) => println("missed") }
    println(task.cancel())
}''')
        self.assertEqual(output, "failed\nfalse\n")
        self.assertEqual(runtime._tasks[0].state, "failed")
        self.assertTrue(all(t.state == "cancelled" for t in runtime._tasks[1:]))
        self.assertFalse(waits)

    def test_nested_groups_inner_cancellation_preserves_outer_and_completed_member(self):
        output, runtime, waits = self.execute('''async fn empty() {}
async fn slow() { await sleepFor(seconds(60)); println("leaked") }
async fn middle() {
    let inner = taskGroup()
    let done = empty()
    await done
    inner.add(done)
    inner.add(slow())
    println(inner.cancel())
    println(inner.cancel())
    println(done.cancel())
    await inner
    println("middle")
}
async fn owner() {
    let outer = taskGroup()
    outer.add(middle())
    await outer
    println("outer")
}
fn main() { let task = owner(); runTasks(); println(task.isCancelled()) }''')
        self.assertEqual(output, "1\n0\nfalse\nmiddle\nouter\nfalse\n")
        self.assertFalse(waits)

    def test_nested_groups_inner_failure_fails_ancestors_and_cancels_siblings(self):
        output, runtime, waits = self.execute('''async fn slow() { await sleepFor(seconds(60)); println("leaked") }
async fn broken() { let failure: Float = 1 / 0 }
async fn middle() {
    let inner = taskGroup()
    inner.add(slow())
    inner.add(broken())
    await inner
    println("leaked middle")
}
async fn owner() {
    let outer = taskGroup()
    outer.add(slow())
    outer.add(middle())
    await outer
    println("leaked owner")
}
fn main() {
    let task = owner()
    match runTasks() { Err(_) => println("failed"); Ok(_) => println("missed") }
    match runTasks() { Ok(_) => println("reported once"); Err(_) => println("duplicate") }
}''')
        self.assertEqual(output, "failed\nreported once\n")
        self.assertEqual(runtime._tasks[0].state, "failed")
        self.assertFalse(waits)

    def test_outer_group_cancellation_cancels_inner_group_and_timer(self):
        output, runtime, waits = self.execute('''async fn slow() { await sleepFor(seconds(60)); println("leaked") }
async fn middle(ready: List<Bool>) {
    let inner = taskGroup()
    inner.add(slow())
    ready.append(true)
    await inner
    println("leaked middle")
}
async fn owner() {
    let ready = List<Bool>()
    let outer = taskGroup()
    let task = middle(ready)
    outer.add(task)
    while ready.get(0) != Some(true) {}
    println(outer.cancel())
    await outer
    println(task.isCancelled())
}
fn main() { let task = owner(); runTasks(); println(task.isCancelled()) }''')
        self.assertEqual(output, "1\ntrue\nfalse\n")
        self.assertFalse(waits)

    def test_detached_root_keeps_own_structured_children_and_leaves_all_groups(self):
        output, runtime, _ = self.execute('''async fn leaf() { await sleepFor(milliseconds(2)); println("leaf") }
async fn detached() { let child = leaf(); println("detached body") }
async fn owner() {
    let first = taskGroup()
    let second = taskGroup()
    let task = detached()
    first.add(task)
    second.add(task)
    println(task.detach())
    println(first.cancel())
    await second
    println("owner")
}
fn main() { let task = owner(); runTasks(); println(task.isCancelled()) }''')
        self.assertEqual(output, "detached body\ntrue\n0\nowner\nleaf\nfalse\n")
        detached = runtime._tasks[1]
        self.assertIsNone(detached.parent)
        self.assertTrue(detached.children)
        self.assertTrue(all(t.state == "completed" for t in runtime._tasks))

    def test_detached_failure_does_not_change_completed_parent(self):
        output, runtime, _ = self.execute('''async fn broken() { await sleepFor(milliseconds(1)); let failure: Float = 1 / 0 }
async fn owner() { let task = broken(); task.detach() }
fn main() {
    let task = owner()
    match runTasks() { Err(_) => println("failed"); Ok(_) => println("missed") }
    println(task.isCancelled())
    println(task.cancel())
    match runTasks() { Ok(_) => println("reported once"); Err(_) => println("duplicate") }
}''')
        self.assertEqual(output, "failed\nfalse\nfalse\nreported once\n")
        self.assertEqual(runtime._tasks[0].state, "completed")
        self.assertEqual(runtime._tasks[1].state, "failed")

    def test_cancelled_await_target_fails_waiter_not_target_owner(self):
        output, runtime, waits = self.execute('''async fn waiter(target: Task<Void>) {
    if true { while true { await target; break } }
    println("leaked waiter")
}
async fn cancel(target: Task<Void>) { target.cancel() }
fn main() {
    let target = sleepFor(seconds(60))
    let task = waiter(target)
    let controller = cancel(target)
    match runTasks() { Err(_) => println("failed"); Ok(_) => println("missed") }
    println(target.isCancelled())
}''')
        self.assertEqual(output, "failed\ntrue\n")
        self.assertEqual(runtime._tasks[1].state, "failed")
        self.assertFalse(waits)

    def test_deadline_completion_boundaries(self):
        for completion, deadline, expected in [(1, 2, "completed"), (2, 1, "failed"), (1, 1, "completed")]:
            with self.subTest(completion=completion, deadline=deadline):
                runtime, now, location = self.runtime()
                source = runtime._create_sleep_task(_TyveraDuration(completion), location)
                timed = runtime._create_timeout_task(source, _TyveraDuration(deadline), location)
                now[0] = min(completion, deadline) / 1000
                runtime._promote_sleeping_tasks()
                runtime._settle_timed_tasks()
                self.assertEqual(timed.state, expected)
                self.assertEqual(source.state, "completed" if expected == "completed" else "cancelled")
                now[0] = 100
                runtime._promote_sleeping_tasks()
                runtime._settle_timed_tasks()
                self.assertEqual(timed.state, expected)
                runtime._cancel_task(timed)
                self.assertEqual(timed.state, expected)

    def test_deadline_and_manual_cancel_first_terminal_outcome_wins(self):
        for cancel_first in (True, False):
            with self.subTest(cancel_first=cancel_first):
                runtime, now, location = self.runtime()
                source = runtime._create_sleep_task(_TyveraDuration(10), location)
                timed = runtime._create_timeout_task(source, _TyveraDuration(1), location)
                now[0] = .001
                if cancel_first:
                    runtime._cancel_task(timed)
                runtime._settle_timed_tasks()
                runtime._cancel_task(timed)
                runtime._settle_timed_tasks()
                self.assertEqual(timed.state, "cancelled" if cancel_first else "failed")
                self.assertEqual(source.state, "cancelled")
                self.assertIsNone(timed.timeout_target)
                self.assertIsNone(runtime._next_task_deadline())

    def test_parent_cancel_with_child_deadline_never_produces_late_failure(self):
        runtime, now, location = self.runtime()
        parent = _TyveraTask(None, None, location, state="waiting")
        runtime._tasks.append(parent)
        runtime._current_task = parent
        source = runtime._create_sleep_task(_TyveraDuration(10), location)
        timed = runtime._create_timeout_task(source, _TyveraDuration(1), location)
        runtime._current_task = None
        runtime._cancel_task(parent)
        now[0] = 100
        runtime._promote_sleeping_tasks()
        runtime._settle_timed_tasks()
        self.assertTrue(all(t.state == "cancelled" for t in (parent, source, timed)))
        self.assertIsNone(parent.error)

    def test_multiple_failures_keep_first_parent_cause(self):
        runtime, _, location = self.runtime()
        parent = _TyveraTask(None, None, location, state="joining")
        first = _TyveraTask(None, None, location, parent=parent)
        second = _TyveraTask(None, None, location, parent=parent)
        parent.children.extend([first, second])
        runtime._tasks.extend([parent, first, second])
        runtime._fail_task(first, "first failure")
        runtime._fail_task(second, "second failure")
        self.assertEqual(parent.error, "owned child task failed: first failure")
        self.assertEqual(second.state, "cancelled")
        runtime._complete_task(parent)
        self.assertEqual(parent.state, "failed")

    def test_failure_unregisters_io_once_and_late_completion_is_ignored(self):
        runtime, _, location = self.runtime()
        calls = []
        task = _TyveraTask(None, None, location, state="io_wait", io_cancel=lambda: calls.append("cancel"))
        runtime._fail_task(task, "failure")
        runtime._cancel_task(task)
        runtime._complete_io_task(task, 42)
        self.assertEqual(calls, ["cancel"])
        self.assertEqual((task.state, task.result, task.error), ("failed", None, "failure"))

    def test_cancel_releases_unconsumed_native_wait_resource(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                runtime, _, location = self.runtime()
                stream = io.StringIO()
                resource = _TyveraTextFile(stream, "write")
                task = _TyveraTask(None, None, location, native_wait_result=resource)
                if fail:
                    runtime._fail_task(task, "failed")
                else:
                    runtime._cancel_task(task)
                self.assertTrue(stream.closed)

    def test_reentrant_cancellation_callback_cannot_complete_task(self):
        runtime, _, location = self.runtime()
        task = _TyveraTask(None, None, location, state="io_wait")
        task.io_cancel = lambda: runtime._complete_io_task(task, 42)
        runtime._cancel_task(task)
        self.assertEqual((task.state, task.result), ("cancelled", None))

    def test_cancel_in_async_condition_does_not_recreate_frames(self):
        for condition in ("if interrupt(tasks)", "while interrupt(tasks)"):
            with self.subTest(condition=condition):
                output, runtime, _ = self.execute('''fn interrupt(tasks: List<Task<Void>>) -> Bool {
    match tasks.get(0) { Some(task) => return task.cancel(); None => return false }
}
async fn owner(tasks: List<Task<Void>>) {
    CONDITION { println("leaked") }
}
fn main() {
    let tasks = List<Task<Void>>()
    let task = owner(tasks)
    tasks.append(task)
    runTasks()
    println(task.isCancelled())
}'''.replace("CONDITION", condition))
                self.assertEqual(output, "true\n")

    def test_detached_survives_former_parent_failure_and_cancellation(self):
        for failure in (True, False):
            with self.subTest(failure=failure):
                ending = "let broken: Float = 1 / 0; return false" if failure else "return interrupt(tasks)"
                output, runtime, _ = self.execute('''fn interrupt(tasks: List<Task<Bool>>) -> Bool {
    match tasks.get(0) { Some(task) => return task.cancel(); None => return false }
}
async fn leaf() { await sleepFor(milliseconds(1)); println("survived") }
async fn owner(tasks: List<Task<Bool>>) -> Bool {
    let child = leaf()
    child.detach()
    ENDING
}
fn main() {
    let tasks = List<Task<Bool>>()
    let task = owner(tasks)
    tasks.append(task)
    runTasks()
}'''.replace("ENDING", ending))
                self.assertEqual(output, "survived\n")
                self.assertEqual(runtime._tasks[0].state, "failed" if failure else "cancelled")
                self.assertIsNone(runtime._tasks[1].parent)
                self.assertEqual(runtime._tasks[1].state, "completed")

    def test_shutdown_cancels_detached_root_with_children_without_advancing_time(self):
        output, runtime, waits = self.execute('''actor Gate { on unused() {} }
async fn leaf(gate: ActorRef<Gate>) { let reason: String = await gate.monitor() }
async fn detached(gate: ActorRef<Gate>) { let child = leaf(gate) }
async fn owner(gate: ActorRef<Gate>) { let task = detached(gate); task.detach() }
fn main() {
    let gate = spawn(Gate())
    let task = owner(gate)
    runTasks()
}''', pending_at_shutdown=True)
        self.assertEqual(output, "")
        self.assertEqual(runtime._tasks[0].state, "completed")
        self.assertTrue(all(t.state == "cancelled" for t in runtime._tasks[1:]))
        self.assertFalse(waits)
        runtime, now, location = self.runtime()
        root = _TyveraTask(None, None, location, state="waiting")
        runtime._tasks.append(root)
        runtime._current_task = root
        child = runtime._create_sleep_task(_TyveraDuration(60000), location)
        runtime._current_task = None
        runtime._shutdown_runtime()
        runtime._shutdown_runtime()
        self.assertEqual((root.state, child.state), ("cancelled", "cancelled"))
        self.assertEqual(now[0], 0)
        self.assertEqual(runtime._runtime_state, "closed")


if __name__ == "__main__":
    unittest.main()
