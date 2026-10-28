from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import tempfile
import unittest

from tests import test_native_compiler as native
from tyvera.compiler import compile_checked_source
from tyvera.cpp_backend import emit_cpp
from tyvera.ir import IRActorSend, IRLiteral, IRType, validate_ir
from tyvera.lowering import lower_checked_program
from tyvera.source import Diagnostic


class NativeActorTimerTests(unittest.TestCase):
    def differential(self, source: str, expected: str, *, repeats: int = 1) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-timer-") as temporary:
            root = pathlib.Path(temporary)
            reference = native.NativeExecutionTests._reference_result(source, root)
            self.assertEqual(reference, (0, expected, ""))
            checked = compile_checked_source(source, "native-io.tyv")
            self.assertIn("struct Timer", emit_cpp(lower_checked_program(checked), project_root=root))
            if native.HAS_CPP_COMPILER:
                executable = root / "timer"
                result = native.NativeExecutionTests._native_source_result(self, source, root, executable, root)
                self.assertEqual((result.returncode, result.stdout, result.stderr), reference)
                for _ in range(repeats - 1):
                    result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                            text=True, timeout=15, check=False)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), reference)

    def test_timer_delay_is_typed_and_ir_validator_rejects_invalid_delay_or_result(self) -> None:
        source = '''actor Worker { on tick(value: Int) {} }
fn main() {
    let worker = spawn(Worker())
    let timer = worker.sendAfter(milliseconds(1), tick, 7)
}'''
        module = lower_checked_program(compile_checked_source(source, "timer.tyv"))
        main = next(f for f in module.functions if f.name == "main")
        binding = main.body[1]
        self.assertIsInstance(binding.value, IRActorSend)
        self.assertEqual(binding.value.type, IRType.TIMER)
        self.assertEqual(binding.value.delay.type, IRType.DURATION)
        for changed in (
            dataclasses.replace(binding.value, delay=IRLiteral(1, IRType.INT, binding.location)),
            dataclasses.replace(binding.value, type=IRType.VOID),
            dataclasses.replace(binding.value, arguments=()),
        ):
            body = (main.body[0], dataclasses.replace(binding, value=changed))
            bad_main = dataclasses.replace(main, body=body)
            bad_module = dataclasses.replace(module, functions=tuple(
                bad_main if f is main else f for f in module.functions))
            with self.subTest(changed=changed), self.assertRaises(Diagnostic):
                validate_ir(bad_module)

    def test_timer_primitives_cover_immediate_negative_huge_and_alias_cancellation(self) -> None:
        self.differential('''fn main() {
    let immediate = timerAfter(milliseconds(0))
    println(immediate.isReady())
    println(immediate.cancel())
    println(immediate.remaining().inMilliseconds())
    let negative = timerAfter(milliseconds(-1))
    println(negative.isReady())
    let huge = timerAfter(milliseconds(9223372036854775807))
    let alias = huge
    println(huge == alias)
    println(huge.isReady())
    println(huge.remaining().inMilliseconds() > 0)
    println(alias.cancel())
    println(huge.isCancelled())
    println(huge.isReady())
    println(huge.cancel())
    println(huge.remaining().inMilliseconds())
}''', "true\nfalse\n0\ntrue\ntrue\nfalse\ntrue\ntrue\ntrue\nfalse\nfalse\n0\n")

    def test_due_messages_capture_arguments_and_join_existing_fifo_without_inline_delivery(self) -> None:
        self.differential('''actor Worker { on tick(value: Int) { println(value) } }
fn main() {
    let worker = spawn(Worker())
    worker.tick(1)
    var value: Int = 2
    let timer = worker.sendAfter(milliseconds(0), tick, value)
    value = 4
    worker.tick(3)
    println("before drain")
    match runActors() { Ok(count) => println(count)
        Err(error) => eprintln(error) }
    println(timer.isReady())
    println(timer.cancel())
    runActors()
}''', "before drain\n1\n3\n2\n3\ntrue\nfalse\n", repeats=4)

    def test_future_actor_timer_wakes_reply_wait_without_an_unrelated_task_or_sleep(self) -> None:
        self.differential('''actor Worker { on expire(target: ActorRef<Worker>) { target.stop() } }
async fn observe() {
    let worker = spawn(Worker())
    let stopped = worker.monitor()
    let timer = worker.sendAfter(milliseconds(20), expire, worker)
    let reason: String = await stopped
    println(reason)
    println(timer.isReady())
    println(timer.isCancelled())
}
fn main() {
    let task = observe()
    match runTasks() { Ok(_) => println("drained")
        Err(error) => eprintln(error) }
}''', "normal stop\ntrue\nfalse\ndrained\n", repeats=8)

    def test_cancel_stop_restart_and_stale_reference_discard_old_timers(self) -> None:
        self.differential('''actor Worker {
    init() { println("started") }
    on tick() { println("tick") }
    on crash() { let broken: Float = 1 / 0 }
}
fn main() {
    let supervisor = oneForOne(2, 1000)
    let worker = supervisor.start(Worker(), transientChildPolicy())
    let cancelled = worker.sendAfter(milliseconds(60000), tick)
    println(cancelled.cancel())
    println(cancelled.cancel())
    let old = worker.sendAfter(milliseconds(60000), tick)
    worker.crash()
    runActors()
    println(old.isCancelled())
    let fresh = worker.sendAfter(milliseconds(0), tick)
    runActors()
    println(fresh.isReady())
    let stopped = worker.sendAfter(milliseconds(60000), tick)
    worker.stop()
    println(stopped.isCancelled())
    println(stopped.isReady())
    println(stopped.remaining().inMilliseconds())
    supervisor.stop()
}''', "started\ntrue\nfalse\nstarted\ntrue\ntick\ntrue\ntrue\nfalse\n0\n", repeats=4)

    def test_nonblocking_actor_drain_does_not_wait_for_future_timer_and_shutdown_discards_it(self) -> None:
        self.differential('''actor Worker { on tick() { println("unexpected") } }
fn main() {
    let worker = spawn(Worker())
    let timer = worker.sendAfter(milliseconds(60000), tick)
    match runActors() { Ok(count) => println(count)
        Err(error) => eprintln(error) }
    println(timer.isReady())
    println(timer.remaining().inMilliseconds() > 0)
    println("main finished")
}''', "0\nfalse\ntrue\nmain finished\n")

    def test_timer_stop_cycles_do_not_deliver_discarded_messages(self) -> None:
        self.differential('''actor Worker { on tick() { println("unexpected") } }
fn main() {
    var index: Int = 0
    while index < 128 {
        let worker = spawn(Worker())
        let timer = worker.sendAfter(milliseconds(0), tick)
        worker.stop()
        if !timer.isCancelled() { println("missed cancellation") }
        runActors()
        index = index + 1
    }
    println("finished")
}''', "finished\n", repeats=4)

    def test_timer_is_bound_to_target_actor_not_the_completed_scheduling_task(self) -> None:
        self.differential('''actor Worker { on tick() { println("tick") } }
async fn schedule(worker: ActorRef<Worker>) -> Timer {
    return worker.sendAfter(milliseconds(0), tick)
}
fn main() {
    let worker = spawn(Worker())
    let task = schedule(worker)
    runTasks()
    println("task finished")
    runActors()
    match task.tryResult() {
        Ok(Some(timer)) => println(timer.isReady())
        _ => println("missing timer")
    }
}''', "task finished\ntick\ntrue\n")

    @unittest.skipUnless(native.HAS_CPP_COMPILER, "native timer capacity requires a C++17 compiler")
    def test_pending_timer_capacity_fails_with_controlled_native_diagnostic(self) -> None:
        source = '''actor Worker { on tick() {} }
fn main() {
    let worker = spawn(Worker())
    var index: Int = 0
    while index < 4097 {
        worker.sendAfter(milliseconds(60000), tick)
        index = index + 1
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-timer-limit-") as temporary:
            root = pathlib.Path(temporary)
            result = native.NativeExecutionTests._native_source_result(self, source, root, root / "limit", root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("native actor timer capacity exceeded (maximum 4096 pending timers)", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_scheduling_a_stopped_actor_has_source_based_diagnostic(self) -> None:
        source = '''actor Worker { on tick() {} }
fn main() {
    let worker = spawn(Worker())
    worker.stop()
    worker.sendAfter(milliseconds(0), tick)
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-stale-timer-") as temporary:
            root = pathlib.Path(temporary)
            reference = native.NativeExecutionTests._reference_result(source, root)
            self.assertEqual(reference[0], 1)
            self.assertIn("cannot schedule a message for a stopped actor", reference[2])
            if native.HAS_CPP_COMPILER:
                result = native.NativeExecutionTests._native_source_result(self, source, root, root / "stale", root)
                self.assertEqual((result.returncode, result.stdout, result.stderr), reference)


if __name__ == "__main__":
    unittest.main()
