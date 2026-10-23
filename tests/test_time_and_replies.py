from __future__ import annotations

import io
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class TimeAndReplyTests(unittest.TestCase):
    def run_source(self, source: str, *, clock=None) -> str:
        program, _ = compile_source(source, "runtime-foundations.tyv")
        output = io.StringIO()
        Interpreter(program, output, clock=clock).run()
        return output.getvalue()

    def reject(self, source: str, message: str) -> Diagnostic:
        with self.assertRaisesRegex(Diagnostic, message) as caught:
            compile_source(source, "invalid-runtime-foundations.tyv")
        self.assertEqual(caught.exception.stage, "type")
        return caught.exception

    def test_actor_request_returns_typed_one_shot_reply(self) -> None:
        output = self.run_source('''actor Counter {
    var value: Int
    init(start: Int) { self.value = start }
    on add(amount: Int) { self.value = self.value + amount }
    on read(reply: ReplySender<Int>) { reply.send(self.value) }
}
fn main() {
    let counter = spawn(Counter(12))
    counter.add(3)
    let answer = counter.request(read)
    println(answer.isReady())
    match runActors() {
        Ok(_) => println("drained")
        Err(reason) => println(reason)
    }
    println(answer.isReady())
    match answer.tryReceive() {
        Ok(result) => {
            match result {
                Some(value) => println(value)
                None => println("pending")
            }
        }
        Err(reason) => println(reason)
    }
    println(answer.isReady())
}''')
        self.assertEqual(output, "false\ndrained\ntrue\n15\ntrue\n")

    def test_request_reply_supports_arguments_and_failed_or_duplicate_sends(self) -> None:
        output = self.run_source('''actor Echo {
    on echo(value: String, reply: ReplySender<String>) {
        println(reply.send(value))
        println(reply.send("duplicate"))
    }
}
fn main() {
    let echoer = spawn(Echo())
    let answer = echoer.request(echo, "hello")
    runActors()
    match answer.tryReceive() {
        Ok(result) => {
            match result {
                Some(value) => println(value)
                None => println("pending")
            }
        }
        Err(reason) => println(reason)
    }
    match answer.tryReceive() {
        Ok(result) => {
            match result {
                Some(_) => println("duplicate receive")
                None => println("consumed")
            }
        }
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "true\nfalse\nhello\nconsumed\n")

    def test_cancellation_is_one_shot_and_does_not_run_actor_inline(self) -> None:
        output = self.run_source('''actor Worker {
    on work(reply: ReplySender<Int>) {
        println(reply.send(7))
    }
}
fn main() {
    let worker = spawn(Worker())
    let answer = worker.request(work)
    println(answer.cancel())
    println(answer.cancel())
    println(answer.isCancelled())
    runActors()
    match answer.tryReceive() {
        Ok(result) => {
            match result {
                Some(value) => println(value)
                None => println("empty")
            }
        }
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "true\nfalse\ntrue\nfalse\nempty\n")

    def test_actor_failure_completes_pending_request_with_error(self) -> None:
        output = self.run_source('''actor Broken {
    on read(reply: ReplySender<Int>) {
        let failure: Float = 1 / 0
    }
}
fn main() {
    let supervisor = oneForOne(1, 1000)
    let worker = supervisor.start(Broken())
    let answer = worker.request(read)
    match runActors() {
        Ok(_) => println("recovered")
        Err(reason) => println(reason)
    }
    match answer.tryReceive() {
        Ok(result) => {
            match result {
                Some(value) => println(value)
                None => println("pending")
            }
        }
        Err(reason) => println(reason)
    }
    println(worker.isAlive())
}''')
        self.assertIn("recovered\nactor 'Broken' crashed at runtime-foundations.tyv:3:", output)
        self.assertTrue(output.endswith("division by zero\ntrue\n"))

    def test_actor_stop_completes_pending_request_with_error(self) -> None:
        output = self.run_source('''actor Worker {
    on read(reply: ReplySender<Int>) { reply.send(1) }
}
fn main() {
    let worker = spawn(Worker())
    let answer = worker.request(read)
    worker.stop()
    match answer.tryReceive() {
        Ok(_) => println("unexpected")
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "actor stopped before replying\n")

    def test_request_with_timeout_uses_injected_clock(self) -> None:
        output = self.run_source('''actor Worker {
    on work(reply: ReplySender<Int>) { println(reply.send(7)) }
}
fn main() {
    let worker = spawn(Worker())
    let answer = worker.requestWithin(milliseconds(0), work)
    runActors()
    println(answer.isTimedOut())
    match answer.tryReceive() {
        Ok(result) => {
            match result {
                Some(value) => println(value)
                None => println("pending")
            }
        }
        Err(reason) => println(reason)
    }
}''', clock=lambda: 3.0)
        self.assertEqual(output, "false\ntrue\nrequest timed out\n")

    def test_request_timeout_prevents_a_late_reply(self) -> None:
        ticks = iter([0.0, 0.01])
        output = self.run_source('''actor Worker {
    on read(reply: ReplySender<Int>) { println(reply.send(1)) }
}
fn main() {
    let worker = spawn(Worker())
    let answer = worker.requestWithin(milliseconds(10), read)
    runActors()
    println(answer.isTimedOut())
    match answer.tryReceive() {
        Ok(_) => println("unexpected")
        Err(reason) => println(reason)
    }
}''', clock=lambda: next(ticks, 0.01))
        self.assertEqual(output, "false\ntrue\nrequest timed out\n")

    def test_request_requires_a_typed_reply_parameter_and_exact_arguments(self) -> None:
        self.reject('''actor Worker { on work() {} }
fn main() { let worker = spawn(Worker()); worker.request(work) }''', "must end with ReplySender<T>")
        self.reject('''actor Worker { on work(reply: ReplySender<Int>) {} }
fn main() { let worker = spawn(Worker()); worker.request(work, 1) }''', r"expects 0 argument\(s\), got 1")
        self.reject('''actor Worker { on work(reply: ReplySender<Int>) {} }
fn main() { let worker = spawn(Worker()); worker.request(missing) }''', "no message handler 'missing'")
        self.reject('''actor Worker { on work(reply: ReplySender<Int>) {} }
fn main() { let worker = spawn(Worker()); worker.requestWithin(5, work) }''', "expects Duration, found Int")
        self.reject('''actor Worker { on read(reply: ReplySender<Int>) {} }
fn main() { let worker = spawn(Worker()); let answer: ReplyReceiver<String> = worker.request(read) }''', "expected ReplyReceiver<String>, found ReplyReceiver<Int>")

    def test_reply_senders_cannot_be_forwarded_as_ordinary_actor_messages(self) -> None:
        self.reject('''actor Sink { on take(reply: ReplySender<Int>) {} }
actor Worker {
    on forward(sink: ActorRef<Sink>, reply: ReplySender<Int>) { sink.take(reply) }
}
fn main() {}''', "cannot be forwarded as ordinary messages")

    def test_reply_sender_is_only_valid_as_final_actor_handler_parameter(self) -> None:
        self.reject('''actor Worker { on work(reply: ReplySender<Int>, name: String) {} }
fn main() {}''', "only allowed as the final parameter")
        self.reject('''actor Worker { init(reply: ReplySender<Int>) {} }
fn main() {}''', "only allowed as the final parameter")
        self.reject('''actor Worker { on work(reply: ReplySender<List<Int>>) {} }
fn main() {}''', "cannot carry mutable or process-local")

    def test_duration_construction_arithmetic_and_comparison(self) -> None:
        self.assertEqual(self.run_source('''fn main() {
    let one = seconds(1)
    let quarter = milliseconds(250)
    println((one - quarter).inMilliseconds())
    println(quarter < one)
    println((quarter + quarter) == milliseconds(500))
}'''), "750\ntrue\ntrue\n")

    def test_timer_uses_injected_monotonic_clock_and_reports_remaining_time(self) -> None:
        ticks = iter([0.0, 0.0, 0.0, 0.25])
        self.assertEqual(
            self.run_source('''fn main() {
    let timer = timerAfter(milliseconds(250))
    println(timer.isReady())
    println(timer.remaining().inMilliseconds())
    println(timer.isReady())
}''', clock=lambda: next(ticks, 0.25)),
            "false\n250\ntrue\n",
        )

    def test_timer_cancel_is_idempotent_and_ready_timer_cannot_be_cancelled(self) -> None:
        ticks = iter([0.0, 0.0, 0.0, 0.01])
        self.assertEqual(
            self.run_source('''fn main() {
    let timer = timerAfter(milliseconds(10))
    println(timer.cancel())
    println(timer.cancel())
    println(timer.isCancelled())
    println(timer.isReady())
}''', clock=lambda: next(ticks, 0.01)),
            "true\nfalse\ntrue\nfalse\n",
        )

    def test_elapsed_timer_cannot_be_cancelled(self) -> None:
        ticks = iter([1.0, 1.01, 1.02])
        output = self.run_source('''fn main() {
    let timer = timerAfter(milliseconds(10))
    println(timer.isReady())
    println(timer.cancel())
    println(timer.remaining().inMilliseconds())
}''', clock=lambda: next(ticks, 1.02))
        self.assertEqual(output, "true\nfalse\n0\n")

    def test_send_after_delivers_actor_message_when_controlled_clock_reaches_deadline(self) -> None:
        ticks = iter([0.0, 0.0, 0.0, 0.5])
        output = self.run_source('''actor Worker {
    on tick(value: Int) { println(value) }
}
fn main() {
    let worker = spawn(Worker())
    let timer = worker.sendAfter(milliseconds(500), tick, 7)
    match runActors() {
        Ok(count) => println(count)
        Err(reason) => println(reason)
    }
    println(timer.isReady())
    match runActors() {
        Ok(count) => println(count)
        Err(reason) => println(reason)
    }
}''', clock=lambda: next(ticks, 0.5))
        self.assertEqual(output, "0\nfalse\n7\n1\n")

    def test_send_after_can_be_cancelled_and_actor_stop_cancels_targeted_timers(self) -> None:
        output = self.run_source('''actor Worker { on tick() { println("unexpected") } }
fn main() {
    let worker = spawn(Worker())
    let cancelled = worker.sendAfter(milliseconds(10), tick)
    println(cancelled.cancel())
    runActors()
    println(cancelled.isCancelled())
    let stopped = worker.sendAfter(milliseconds(10), tick)
    worker.stop()
    println(stopped.isCancelled())
    runActors()
}''', clock=lambda: 0.0)
        self.assertEqual(output, "true\ntrue\ntrue\n")

    def test_duration_and_timer_inputs_are_statically_typed_and_overflow_checked(self) -> None:
        self.reject("fn main() { let wait = milliseconds(1.5) }", "expects Int, found Float")
        self.reject("fn main() { let timer = timerAfter(5) }", "expects Duration, found Int")
        self.reject('''actor Worker { on tick(value: Int) {} }
fn main() { let worker = spawn(Worker()); worker.sendAfter(milliseconds(1), tick, "wrong") }''', "expects Int, found String")
        self.reject('''actor Worker { on read(reply: ReplySender<Int>) {} }
fn main() { let worker = spawn(Worker()); worker.sendAfter(milliseconds(1), read) }''', "cannot schedule a request handler")
        program, _ = compile_source("fn main() { let wait = seconds(9223372036854776) }", "duration_overflow.tyv")
        with self.assertRaisesRegex(Diagnostic, "Duration overflow"):
            Interpreter(program, io.StringIO()).run()


if __name__ == "__main__":
    unittest.main()
