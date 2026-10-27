from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from tyvera.compiler import compile_project, compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class AsyncRuntimeTests(unittest.TestCase):
    def run_source(self, source: str, *, clock=None, sleeper=None) -> str:
        program, _ = compile_source(source, "async-runtime.tyv")
        output = io.StringIO()
        Interpreter(program, output, clock=clock, sleeper=sleeper).run()
        return output.getvalue()

    def reject(self, source: str, message: str) -> Diagnostic:
        with self.assertRaisesRegex(Diagnostic, message) as caught:
            compile_source(source, "invalid-async-runtime.tyv")
        self.assertEqual(caught.exception.stage, "type")
        return caught.exception

    def test_async_function_is_deferred_until_run_tasks_and_awaits_sleep(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn later() {
    println("before")
    await sleepFor(milliseconds(5))
    println("after")
}
fn main() {
    let task = later()
    println(task.isDone())
    match runTasks() {
        Ok(steps) => println(steps > 0)
        Err(_) => println("scheduler error")
    }
    println(task.isDone())
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "false\nbefore\nafter\ntrue\ntrue\n")
        self.assertGreaterEqual(now[0], 0.005)

    def test_task_waits_for_child_and_scheduler_round_robins_statements(self) -> None:
        now = [4.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn child() {
    println("child-start")
    await sleepFor(milliseconds(1))
    println("child-end")
}
async fn parent() {
    let child_task = child()
    await child_task
    println("parent-end")
}
async fn first() {
    println("first-1")
    println("first-2")
}
async fn second() {
    println("second-1")
    println("second-2")
}
fn main() {
    let parent_task = parent()
    let first_task = first()
    let second_task = second()
    runTasks()
    println(parent_task.isDone())
    println(first_task.isDone())
    println(second_task.isDone())
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "first-1\nsecond-1\nchild-start\nfirst-2\nsecond-2\nchild-end\nparent-end\ntrue\ntrue\ntrue\n")

    def test_async_task_can_await_actor_reply_and_read_typed_result(self) -> None:
        output = self.run_source('''actor Counter {
    on read(reply: ReplySender<Int>) { reply.send(23) }
}
async fn query(counter: ActorRef<Counter>) {
    let response = counter.request(read)
    await response
    match response.tryReceive() {
        Ok(value) => match value {
            Some(number) => println(number)
            None => println("missing")
        }
        Err(reason) => println(reason)
    }
}
fn main() {
    let counter = spawn(Counter())
    let task = query(counter)
    match runTasks() {
        Ok(_) => println(task.isDone())
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "23\ntrue\n")

    def test_async_functions_return_statically_typed_task_values(self) -> None:
        now = [1.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn calculate(left: Int, right: Int) -> Int {
    await sleepFor(milliseconds(1))
    return left + right
}
async fn report() -> Int {
    let calculation: Task<Int> = calculate(20, 22)
    match calculation.tryResult() {
        Ok(result) => match result {
            Some(_) => println("unexpected early result")
            None => println("pending")
        }
        Err(reason) => println(reason)
    }
    let answer: Int = await calculation
    println(answer)
    return answer
}
fn main() {
    let report_task: Task<Int> = report()
    match runTasks() {
        Ok(_) => println("tasks complete")
        Err(reason) => println(reason)
    }
    match report_task.tryResult() {
        Ok(result) => match result {
            Some(answer) => println(answer)
            None => println("pending")
        }
        Err(reason) => println(reason)
    }
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "pending\n42\ntasks complete\n42\n")

    def test_awaited_reply_can_initialize_a_typed_binding(self) -> None:
        output = self.run_source('''actor Counter {
    on read(reply: ReplySender<Int>) { reply.send(31) }
}
async fn query(counter: ActorRef<Counter>) {
    let answer: Int = await counter.request(read)
    println(answer)
}
fn main() {
    let counter = spawn(Counter())
    let task = query(counter)
    runTasks()
    println(task.isDone())
}''')
        self.assertEqual(output, "31\ntrue\n")

    def test_async_task_result_annotation_is_statically_checked(self) -> None:
        self.reject('''async fn value() -> Int { return 1 }
async fn caller() {
    let text: String = await value()
}
fn main() { caller() }''', "expected String, found Int")

    def test_cancellation_skips_queued_task_and_is_reported_once(self) -> None:
        output = self.run_source('''async fn work() { println("must not run") }
fn main() {
    let task = work()
    println(task.cancel())
    println(task.cancel())
    println(task.isCancelled())
    match task.tryResult() {
        Ok(_) => println("unexpected success")
        Err(_) => println("cancelled")
    }
    runTasks()
}''')
        self.assertEqual(output, "true\nfalse\ntrue\ncancelled\n")

    def test_task_cancellation_never_runs_remaining_statements(self) -> None:
        now = [5.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn delayed() {
    if true {
        await sleepFor(milliseconds(10))
        println("must not run")
    }
}
async fn cancel(target: Task) {
    println(target.cancel())
}
fn main() {
    let target = delayed()
    let canceller = cancel(target)
    runTasks()
    println(target.isCancelled())
    println(canceller.isDone())
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "true\ntrue\ntrue\n")

    def test_task_cancellation_cascades_to_owned_children(self) -> None:
        output = self.run_source('''async fn child() {
    await sleepFor(milliseconds(10))
    println("child escaped")
}
async fn owner() {
    let child_task = child()
    println("owner body complete")
}
async fn cancel(target: Task) {
    await sleepFor(milliseconds(1))
    target.cancel()
}
fn main() {
    let owner_task = owner()
    let canceller = cancel(owner_task)
    runTasks()
    println(owner_task.isCancelled())
    println(canceller.isDone())
}''')
        self.assertEqual(output, "owner body complete\ntrue\ntrue\n")

    def test_normal_owner_exit_joins_unfinished_children(self) -> None:
        output = self.run_source('''async fn child() {
    await sleepFor(milliseconds(10))
    println("child finished")
}
async fn owner() -> Task {
    let child_task = child()
    return child_task
}
fn main() {
    let owner_task = owner()
    runTasks()
    match owner_task.tryResult() {
        Ok(result) => match result {
            Some(child_task) => {
                println(child_task.isDone())
                println(child_task.isCancelled())
            }
            None => println("missing child")
        }
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "child finished\ntrue\nfalse\n")

    def test_owned_child_failure_fails_owner_and_cancels_siblings(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn failingChild() {
    await sleepFor(milliseconds(1))
    println(1 / 0)
}
async fn slowSibling() {
    await sleepFor(milliseconds(20))
    println("sibling escaped")
}
async fn owner() {
    let failing = failingChild()
    let sibling = slowSibling()
    await sleepFor(milliseconds(50))
    println("owner escaped")
}
fn main() {
    let task = owner()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("child failure propagated")
    }
    match task.tryResult() {
        Ok(_) => println("unexpected owner result")
        Err(_) => println("owner failed")
    }
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "child failure propagated\nowner failed\n")
        self.assertGreaterEqual(now[0], 0.001)

    def test_task_sleep_does_not_consume_unrelated_actor_timer(self) -> None:
        now = [2.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''actor Tick {
    on tick() { println("actor tick") }
}
async fn delayed() {
    await sleepFor(milliseconds(2))
    println("task done")
}
fn main() {
    let ticker = spawn(Tick())
    ticker.sendAfter(milliseconds(1), tick)
    let task = delayed()
    runTasks()
    println(task.isDone())
    runActors()
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "task done\ntrue\nactor tick\n")

    def test_async_function_can_be_exported_and_imported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lib.tyv").write_text('''pub async fn greet() {
    println("from module")
}
''', encoding="utf-8")
            entry = root / "main.tyv"
            entry.write_text('''import "lib.tyv" as lib
fn main() {
    let task = lib.greet()
    runTasks()
    println(task.isDone())
}
''', encoding="utf-8")
            program, _ = compile_project(entry, root)
            output = io.StringIO()
            Interpreter(program, output, project_root=root).run()
            self.assertEqual(output.getvalue(), "from module\ntrue\n")

    def test_request_timeout_fails_awaited_task_without_sleeping_real_time(self) -> None:
        now = [10.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''actor Quiet {
    on ping(reply: ReplySender<Int>) { }
}
async fn query(worker: ActorRef<Quiet>) {
    let response = worker.requestWithin(milliseconds(4), ping)
    await response
    println("must not run")
}
fn main() {
    let worker = spawn(Quiet())
    let task = query(worker)
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("timeout propagated")
    }
    match task.tryResult() {
        Ok(_) => println("unexpected task result")
        Err(_) => println("task failed")
    }
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "timeout propagated\ntask failed\n")
        self.assertGreaterEqual(now[0], 10.004)

    def test_run_tasks_does_not_repeat_a_failure_reported_by_an_earlier_run(self) -> None:
        output = self.run_source('''async fn invalid() {
    let zero = 1 / 0
}
async fn later() {
    println("later task ran")
}
fn main() {
    let first = invalid()
    match runTasks() {
        Ok(_) => println("unexpected first success")
        Err(_) => println("first run failed")
    }
    let second = later()
    match runTasks() {
        Ok(_) => println("second run succeeded")
        Err(_) => println("stale failure")
    }
}''')
        self.assertEqual(output, "first run failed\nlater task ran\nsecond run succeeded\n")

    def test_task_failure_from_recursive_scheduler_call_is_contained(self) -> None:
        output = self.run_source('''async fn invalid() {
    runTasks()
}
fn main() {
    let task = invalid()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("task failure")
    }
    println(task.isDone())
}''')
        self.assertEqual(output, "task failure\ntrue\n")

    def test_runtime_diagnostic_while_evaluating_await_fails_only_the_task(self) -> None:
        output = self.run_source('''actor Worker {
    on ping(reply: ReplySender<Int>) { reply.send(1) }
}
async fn query(worker: ActorRef<Worker>) {
    await worker.request(ping)
}
fn main() {
    let worker = spawn(Worker())
    let task = query(worker)
    worker.stop()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("await diagnostic contained")
    }
    match task.tryResult() {
        Ok(_) => println("unexpected task result")
        Err(_) => println("task failed")
    }
}''')
        self.assertEqual(output, "await diagnostic contained\ntask failed\n")

    def test_await_is_supported_inside_nested_if_and_lexical_scope(self) -> None:
        output = self.run_source('''async fn value() -> Int {
    await sleepFor(milliseconds(1))
    return 40
}
async fn choose(flag: Bool) -> Int {
    if flag {
        let answer: Int = await value()
        return answer + 2
    } else {
        let fallback: Int = await value()
        return fallback + 1
    }
}
fn main() {
    let task = choose(true)
    match runTasks() {
        Ok(_) => match task.tryResult() {
            Ok(result) => match result {
                Some(answer) => println(answer)
                None => println("pending")
            }
            Err(reason) => println(reason)
        }
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "42\n")

    def test_await_in_while_resumes_and_continue_targets_the_async_loop(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn loopWork() {
    var index: Int = 0
    while index < 3 {
        index = index + 1
        if index == 2 {
            await sleepFor(milliseconds(1))
            continue
        }
        let value: Int = await delayed(index)
        println(value)
    }
}
async fn delayed(value: Int) -> Int {
    await sleepFor(milliseconds(1))
    return value
}
fn main() {
    let task = loopWork()
    match runTasks() {
        Ok(_) => println(task.isDone())
        Err(reason) => println(reason)
    }
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "1\n3\ntrue\n")
        self.assertGreaterEqual(now[0], 0.003)

    def test_await_in_match_and_for_supports_break_from_nested_arm(self) -> None:
        output = self.run_source('''async fn twice(value: Int) -> Int {
    await sleepFor(milliseconds(1))
    return value * 2
}
async fn visit() {
    let values = List<Int>()
    values.append(1)
    values.append(2)
    values.append(3)
    for value in values {
        match value {
            1 => {
                let doubled: Int = await twice(value)
                println(doubled)
            }
            2 => {
                await sleepFor(milliseconds(1))
                break
            }
            _ => println("unreachable")
        }
    }
    println("after loop")
}
fn main() {
    let task = visit()
    runTasks()
    println(task.isDone())
}''')
        self.assertEqual(output, "2\nafter loop\ntrue\n")

    def test_await_remains_invalid_in_synchronous_nested_control_flow(self) -> None:
        self.reject('''fn main() {
    if true {
        await sleepFor(milliseconds(1))
    }
}''', "only valid inside an async function")
        self.reject('''fn work() {
    if true {
        await sleepFor(milliseconds(1))
    }
}
fn main() { work() }''', "only valid inside an async function")

    def test_async_entry_point_is_rejected(self) -> None:
        self.reject('''async fn main() { }
''', "entry point 'main' must be synchronous")

    def test_await_requires_a_task_or_typed_reply_receiver(self) -> None:
        self.reject('''async fn invalid() {
    await milliseconds(1)
}
fn main() { invalid() }''', "expects Task<T>, TaskGroup, or ReplyReceiver<T>")

    def test_task_group_joins_only_registered_direct_children(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn work(label: String) {
    await sleepFor(milliseconds(2))
    println(label)
}
async fn owner() {
    let group = taskGroup()
    let first = work("first")
    let second = work("second")
    println(group.add(first))
    println(group.add(first))
    group.add(second)
    println(group.isDone())
    await group
    println(group.isDone())
}
fn main() {
    let owner_task = owner()
    runTasks()
    println(owner_task.isDone())
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "true\nfalse\nfalse\nfirst\nsecond\ntrue\ntrue\n")
        self.assertGreaterEqual(now[0], 0.002)

    def test_task_group_cancellation_cancels_active_members_and_descendants(self) -> None:
        output = self.run_source('''async fn slow() {
    await sleepFor(milliseconds(100))
    println("must not run")
}
async fn owner() {
    let group = taskGroup()
    let task = slow()
    group.add(task)
    println(group.cancel())
    println(task.isCancelled())
    await group
}
fn main() {
    let task = owner()
    runTasks()
    println(task.isDone())
}''')
        self.assertEqual(output, "1\ntrue\ntrue\n")

    def test_detached_child_outlives_its_owner(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn survivor() {
    await sleepFor(milliseconds(1))
    println("survivor ran")
}
async fn owner() {
    let task = survivor()
    println(task.detach())
}
fn main() {
    let task = owner()
    runTasks()
    println(task.isDone())
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "true\nsurvivor ran\ntrue\n")

    def test_detach_removes_task_from_group_and_is_owner_only(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn child() {
    await sleepFor(milliseconds(1))
    println("child")
}
async fn owner() {
    let group = taskGroup()
    let task = child()
    group.add(task)
    println(task.detach())
    println(group.isDone())
    await group
}
fn main() {
    let task = owner()
    runTasks()
    println(task.isDone())
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "true\ntrue\nchild\ntrue\n")

    def test_root_or_terminal_task_cannot_be_detached(self) -> None:
        output = self.run_source('''async fn empty() { }
fn main() {
    let task = empty()
    println(task.detach())
    runTasks()
    println(task.detach())
}''')
        self.assertEqual(output, "false\nfalse\n")

    def test_detached_task_failure_is_reported_by_scheduler(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn broken() {
    await sleepFor(milliseconds(1))
    var value: Int = 9223372036854775807
    value = value + 1
}
async fn owner() {
    let task = broken()
    task.detach()
}
fn main() {
    let task = owner()
    match runTasks() {
        Ok(_) => println("missed failure")
        Err(_) => println("detached failure reported")
    }
    println(task.isDone())
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "detached failure reported\ntrue\n")

    def test_nested_task_groups_join_at_each_owner_boundary(self) -> None:
        output = self.run_source('''async fn leaf() {
    await sleepFor(milliseconds(1))
    println("leaf")
}
async fn middle() {
    let group = taskGroup()
    group.add(leaf())
    await group
    println("middle")
}
async fn outer() {
    let group = taskGroup()
    group.add(middle())
    await group
    println("outer")
}
fn main() {
    let task = outer()
    match runTasks() {
        Ok(_) => println(task.isDone())
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "leaf\nmiddle\nouter\ntrue\n")

    def test_actor_reply_waiter_is_not_starved_by_ready_tasks(self) -> None:
        output = self.run_source('''async fn spin() {
    var index: Int = 0
    while index < 20 {
        println(index)
        index = index + 1
    }
}
actor Worker {
    on ping(reply: ReplySender<Int>) {
        println("handled")
        reply.send(7)
    }
}
async fn query(worker: ActorRef<Worker>) {
    let answer: Int = await worker.request(ping)
    println(answer)
}
fn main() {
    let worker = spawn(Worker())
    let spinner = spin()
    let request = query(worker)
    match runTasks() {
        Ok(_) => println("complete")
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output.splitlines()[0], "handled")
        self.assertLess(output.index("handled\n"), output.index("19\n"))
        self.assertIn("7\n", output)

    def test_actor_stop_cancels_tasks_started_by_its_handler(self) -> None:
        output = self.run_source('''async fn background() {
    await sleepFor(milliseconds(1))
    println("must not run")
}
actor Worker {
    on start() { let pending = background() }
}
fn main() {
    let worker = spawn(Worker())
    worker.start()
    runActors()
    worker.stop()
    runTasks()
    println(worker.isAlive())
}''')
        self.assertEqual(output, "false\n")

    def test_interpreter_shutdown_cancels_open_work_and_is_one_shot(self) -> None:
        program, _ = compile_source('''async fn pending() {
    await sleepFor(milliseconds(10))
    println("must not run")
}
actor Worker {
    on ping(reply: ReplySender<Int>) { }
}
fn main() {
    let task = pending()
    let worker = spawn(Worker())
    worker.request(ping)
}''', "shutdown.tyv")
        output = io.StringIO()
        interpreter = Interpreter(program, output)
        interpreter.run()

        self.assertEqual(output.getvalue(), "")
        self.assertEqual(interpreter._runtime_state, "closed")
        self.assertEqual(interpreter._scheduler_state, "closed")
        self.assertTrue(all(task.state in {"completed", "failed", "cancelled"} for task in interpreter._tasks))
        self.assertTrue(all(actor.status == "stopped" and not actor.mailbox for actor in interpreter._actors))
        self.assertFalse(interpreter._ready_tasks)
        self.assertFalse(interpreter._ready_actors)
        with self.assertRaisesRegex(Diagnostic, "can only run once"):
            interpreter.run()

    def test_task_group_cannot_be_used_by_a_different_task(self) -> None:
        output = self.run_source('''async fn useGroup(group: TaskGroup) {
    await group
}
async fn owner() {
    let group = taskGroup()
    let child = useGroup(group)
    await child
}
fn main() {
    let task = owner()
    match runTasks() {
        Ok(_) => println("ownership not enforced")
        Err(_) => println("foreign group rejected")
    }
    println(task.isDone())
}''')
        self.assertEqual(output, "foreign group rejected\ntrue\n")

    def test_task_timeout_returns_result_when_task_completes_first(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn answer() -> Int {
    await sleepFor(milliseconds(2))
    return 42
}
fn main() {
    let timed = answer().withTimeout(milliseconds(5))
    runTasks()
    match timed.tryResult() {
        Ok(value) => match value {
            Some(number) => println(number)
            None => println("pending")
        }
        Err(_) => println("failed")
    }
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "42\n")
        self.assertGreaterEqual(now[0], 0.002)

    def test_task_timeout_cancels_source_and_reports_timeout(self) -> None:
        now = [0.0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        output = self.run_source('''async fn slow() -> Int {
    await sleepFor(milliseconds(20))
    return 42
}
fn main() {
    let source = slow()
    let timed = source.withTimeout(milliseconds(2))
    match runTasks() {
        Ok(_) => println("missed timeout")
        Err(_) => println("timeout reported")
    }
    println(source.isCancelled())
    match timed.tryResult() {
        Err(reason) => println(reason)
        Ok(_) => println("unexpected result")
    }
}''', clock=lambda: now[0], sleeper=advance)
        self.assertEqual(output, "timeout reported\ntrue\ntask timed out\n")
        self.assertGreaterEqual(now[0], 0.002)

    def test_cancelling_timeout_task_cancels_source(self) -> None:
        output = self.run_source('''async fn slow() {
    await sleepFor(milliseconds(20))
}
fn main() {
    let source = slow()
    let timed = source.withTimeout(milliseconds(10))
    println(timed.cancel())
    println(source.isCancelled())
    runTasks()
}''')
        self.assertEqual(output, "true\ntrue\n")

    def test_timeout_wrapper_preserves_an_already_completed_result(self) -> None:
        output = self.run_source('''async fn answer() -> Int { return 42 }
async fn owner() {
    let source = answer()
    let first: Int = await source
    let timed = source.withTimeout(milliseconds(-1))
    let second: Int = await timed
    println(first + second)
}
fn main() {
    let task = owner()
    runTasks()
}''')
        self.assertEqual(output, "84\n")

    def test_negative_task_timeout_expires_before_source_starts(self) -> None:
        output = self.run_source('''async fn slow() {
    println("must not start")
}
fn main() {
    let source = slow()
    let timed = source.withTimeout(milliseconds(-1))
    match runTasks() {
        Ok(_) => println("missed timeout")
        Err(_) => println("expired")
    }
    println(source.isCancelled())
    println(timed.isDone())
}''')
        self.assertEqual(output, "expired\ntrue\ntrue\n")

    def test_task_group_and_detach_have_checked_types_and_actor_isolation(self) -> None:
        self.reject('''fn main() {
    let group = taskGroup()
    group.add(42)
}''', "expects Task<T>")
        self.reject('''async fn invalid() {
    let group = taskGroup()
    let value: Void = await group
}
fn main() { invalid() }''', "Void task cannot initialize")
        self.reject('''actor Worker {
    on receive(group: TaskGroup) { }
}
fn main() { let worker = spawn(Worker()); runActors() }''', "actor message cannot carry")
        self.reject('''actor Worker {
    on receive() { let group = taskGroup() }
}
fn main() { let worker = spawn(Worker()); runActors() }''', "inside actor handlers")
        self.reject('''actor Worker {
    on receive() { runTasks() }
}
fn main() { let worker = spawn(Worker()); runActors() }''', "scheduler pumps cannot be called inside actor handlers")
        self.reject('''async fn work() { }
fn main() {
    let task = work()
    task.withTimeout(10)
}''', "expects Duration, found Int")


if __name__ == "__main__":
    unittest.main()

