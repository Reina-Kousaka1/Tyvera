from __future__ import annotations

import io
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic
from tyvera.supervision import CrashKind, ExponentialBackoff, RestartStrategy, RuntimeEventKind


_WORKER = '''actor Worker {
    var value: Int
    init(start: Int) { self.value = start }
    on bump() { self.value = self.value + 1 }
    on fail() { let failure: Float = 1 / 0 }
    on report() { println(self.value) }
}
'''


class SupervisorV2Tests(unittest.TestCase):
    def run_source(self, source: str, **runtime_options: object) -> tuple[str, list[object], Interpreter]:
        program, _ = compile_source(source, "supervisor_v2.tyv")
        output = io.StringIO()
        events: list[object] = []
        runtime_options.setdefault("runtime_event_sink", events.append)
        interpreter = Interpreter(program, output, **runtime_options)
        interpreter.run()
        return output.getvalue(), events, interpreter

    def test_one_for_all_restarts_every_child_and_rebuilds_state(self) -> None:
        output, events, _ = self.run_source(_WORKER + '''fn main() {
    let supervisor = oneForAll(3, 1000)
    let first = supervisor.start(Worker(10))
    let second = supervisor.start(Worker(20))
    first.bump()
    second.bump()
    first.fail()
    runActors()
    first.report()
    second.report()
    runActors()
}''')
        self.assertEqual(output, "10\n20\n")
        restarted = [event.entity for event in events if event.kind is RuntimeEventKind.ACTOR_RESTARTED]
        self.assertEqual(len(restarted), 2)

    def test_rest_for_one_restarts_failed_and_later_children_only(self) -> None:
        output, events, _ = self.run_source(_WORKER + '''fn main() {
    let supervisor = restForOne(3, 1000)
    let first = supervisor.start(Worker(10))
    let second = supervisor.start(Worker(20))
    let third = supervisor.start(Worker(30))
    first.bump()
    second.fail()
    third.bump()
    runActors()
    first.report()
    second.report()
    third.report()
    runActors()
}''')
        self.assertEqual(output, "11\n20\n30\n")
        restarted = [event.entity for event in events if event.kind is RuntimeEventKind.ACTOR_RESTARTED]
        self.assertEqual(len(restarted), 2)

    def test_nested_supervisor_escalates_and_parent_restarts_its_tree(self) -> None:
        output, events, _ = self.run_source(_WORKER + '''fn main() {
    let root = oneForOne(2, 1000)
    let nested = oneForOne(0, 1000)
    let child = nested.start(Worker(7))
    root.start(nested)
    child.fail()
    match runActors() {
        Ok(_) => println("recovered")
        Err(reason) => println(reason)
    }
    println(nested.isAlive())
    println(child.isAlive())
    child.report()
    runActors()
}''')
        self.assertEqual(output, "recovered\ntrue\ntrue\n7\n")
        self.assertTrue(any(event.kind is RuntimeEventKind.SUPERVISOR_ESCALATED for event in events))

    def test_permanent_restart_policy_restarts_normal_stop(self) -> None:
        output, events, _ = self.run_source(_WORKER + '''fn main() {
    let supervisor = oneForOne(2, 1000)
    let permanentChild = supervisor.start(Worker(8), permanentChildPolicy())
    let transientChild = supervisor.start(Worker(9), transientChildPolicy())
    let temporaryChild = supervisor.start(Worker(10), temporaryChildPolicy())
    permanentChild.stop()
    transientChild.stop()
    temporaryChild.stop()
    println(permanentChild.isAlive())
    println(transientChild.isAlive())
    println(temporaryChild.isAlive())
}''')
        self.assertEqual(output, "true\nfalse\nfalse\n")
        normal_restart = next(event for event in events if event.kind is RuntimeEventKind.ACTOR_RESTARTED)
        self.assertIsNotNone(normal_restart.reason)
        self.assertIs(normal_restart.reason.kind, CrashKind.NORMAL_STOP)

    def test_transient_restarts_failure_but_temporary_does_not(self) -> None:
        transient, _, _ = self.run_source(_WORKER + '''fn main() {
    let supervisor = oneForOne(2, 1000)
    let child = supervisor.start(Worker(11), transientChildPolicy())
    child.fail()
    runActors()
    println(child.isAlive())
    child.report()
    runActors()
}''')
        self.assertEqual(transient, "true\n11\n")

        program, _ = compile_source(_WORKER + '''fn main() {
    let supervisor = oneForOne(2, 1000)
    let child = supervisor.start(Worker(12), temporaryChildPolicy())
    child.fail()
    match runActors() {
        Ok(_) => println("unexpected")
                Err(_) => {
                    println(child.isAlive())
                    println(supervisor.isAlive())
                }
    }
}''', "temporary.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        self.assertEqual(output.getvalue(), "false\ntrue\n")

    def test_backoff_uses_injected_monotonic_clock_without_real_sleep(self) -> None:
        now = [12.0]
        delays: list[float] = []

        def sleep(seconds: float) -> None:
            delays.append(seconds)
            now[0] += seconds

        output, _, _ = self.run_source(_WORKER + '''fn main() {
    let supervisor = oneForOne(2, 1000, exponentialBackoff(10, 40))
    let child = supervisor.start(Worker(13))
    child.fail()
    runActors()
    println(child.isAlive())
}''', clock=lambda: now[0], sleeper=sleep)
        self.assertEqual(output, "true\n")
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 0.01)

    def test_task_scheduler_services_supervisor_deadline_while_other_task_waits(self) -> None:
        source = _WORKER + '''actor Responder {
    on read(reply: ReplySender<Int>) { reply.send(1) }
}
async fn waitAndContinue(target: ActorRef<Responder>) -> Int {
    let response = target.request(read)
    await response
    await sleepFor(milliseconds(20))
    println("task finished")
    return 1
}
fn main() {
    let supervisor = oneForOne(2, 1000, exponentialBackoff(10, 40))
    let child = supervisor.start(Worker(14))
    let responder = spawn(Responder())
    child.fail()
    let task = waitAndContinue(responder)
    match runTasks() {
        Ok(_) => {}
        Err(_) => println("task failed")
    }
    match task.tryResult() {
        Ok(Some(value)) => println(value)
        _ => println("missing task result")
    }
}'''
        now = [1.0]
        delays: list[float] = []

        def sleep(seconds: float) -> None:
            delays.append(seconds)
            now[0] += seconds

        output, events, _ = self.run_source(source, clock=lambda: now[0], sleeper=sleep)
        self.assertEqual(output, "task finished\n1\n")
        self.assertEqual(len(delays), 2)
        self.assertAlmostEqual(sum(delays), 0.02)
        restart_position = next(
            index for index, event in enumerate(events)
            if event.kind is RuntimeEventKind.ACTOR_RESTARTED
        )
        self.assertLess(restart_position, next(
            index for index, event in enumerate(events)
            if event.kind is RuntimeEventKind.RUNTIME_SHUTDOWN
        ))

    def test_backoff_is_capped_and_overflow_safe(self) -> None:
        policy = ExponentialBackoff(10, 25)
        self.assertEqual([policy.delay_for(index) for index in range(1, 6)], [10, 20, 25, 25, 25])
        self.assertEqual(ExponentialBackoff(1, 100).delay_for(100_000), 100)

    def test_restart_strategy_and_policy_are_statically_typed(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "SupervisorBackoff"):
            compile_source("fn main() { let s = oneForAll(2, 1000, permanentChildPolicy()) }", "bad_strategy.tyv")
        with self.assertRaisesRegex(Diagnostic, "RestartPolicy"):
            compile_source(_WORKER + '''fn main() {
    let s = oneForOne(2, 1000)
    s.start(Worker(1), exponentialBackoff(1, 2))
}''', "bad_policy.tyv")

    def test_dead_letter_is_bounded_diagnostic_and_event_sink_cannot_change_behavior(self) -> None:
        program, _ = compile_source('''actor Worker { on work() {} }
fn main() {
    let worker = spawn(Worker())
    worker.stop()
    worker.work()
}''', "dead_letter.tyv")
        events: list[object] = []

        def diagnostics(event: object) -> None:
            events.append(event)
            raise RuntimeError("observer failed")

        interpreter = Interpreter(program, io.StringIO(), runtime_event_sink=diagnostics)
        with self.assertRaisesRegex(Diagnostic, "cannot send a message to a stopped actor"):
            interpreter.run()
        self.assertEqual(len(interpreter._dead_letters), 1)
        self.assertTrue(any(event.kind is RuntimeEventKind.DEAD_LETTER for event in events))


if __name__ == "__main__":
    unittest.main()
