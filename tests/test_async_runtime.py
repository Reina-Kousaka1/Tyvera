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
    await sleepFor(milliseconds(10))
    println("must not run")
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

    def test_await_is_restricted_to_top_level_async_body_statements(self) -> None:
        self.reject('''fn main() {
    await sleepFor(milliseconds(1))
}''', "only valid as a top-level statement")
        self.reject('''async fn work() {
    if true {
        await sleepFor(milliseconds(1))
    }
}
fn main() { work() }''', "only valid as a top-level statement")

    def test_async_entry_point_is_rejected(self) -> None:
        self.reject('''async fn main() { }
''', "entry point 'main' must be synchronous")

    def test_await_requires_a_task_or_typed_reply_receiver(self) -> None:
        self.reject('''async fn invalid() {
    await milliseconds(1)
}
fn main() { invalid() }''', "expects Task or ReplyReceiver")


if __name__ == "__main__":
    unittest.main()
