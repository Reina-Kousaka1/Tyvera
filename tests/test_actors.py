from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from tyvera.compiler import compile_project, compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class ActorRuntimeTests(unittest.TestCase):
    def run_source(self, source: str) -> tuple[str, str]:
        program, _ = compile_source(source, "actors.tyv")
        output = io.StringIO()
        errors = io.StringIO()
        Interpreter(program, output, error_output=errors).run()
        return output.getvalue(), errors.getvalue()

    def reject(self, source: str, message: str) -> Diagnostic:
        with self.assertRaisesRegex(Diagnostic, message) as caught:
            compile_source(source, "invalid_actor.tyv")
        self.assertEqual(caught.exception.stage, "type")
        return caught.exception

    def test_messages_are_typed_ordered_and_state_is_isolated(self) -> None:
        output, _ = self.run_source('''actor Counter {
    var value: Int
    init(start: Int) { self.value = start }
    on add(amount: Int) { self.value = self.value + amount }
    on report() { println(self.value) }
}
fn main() {
    let counter: ActorRef<Counter> = spawn(Counter(10))
    counter.add(2)
    counter.add(3)
    counter.report()
    match runActors() {
        Ok(processed) => println(processed)
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "15\n3\n")

    def test_scheduler_round_robins_actors_and_preserves_each_mailbox_order(self) -> None:
        output, _ = self.run_source('''actor Printer {
    on write(value: String) { println(value) }
}
fn main() {
    let first = spawn(Printer())
    let second = spawn(Printer())
    first.write("A1")
    first.write("A2")
    second.write("B1")
    second.write("B2")
    match runActors() {
        Ok(count) => println(count)
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "A1\nB1\nA2\nB2\n4\n")

    def test_self_messages_are_enqueued_after_existing_mail(self) -> None:
        output, _ = self.run_source('''actor Worker {
    on start() { println("start"); self.finish() }
    on finish() { println("finish") }
}
fn main() {
    let worker = spawn(Worker())
    worker.start()
    worker.finish()
    match runActors() {
        Ok(count) => println(count)
        Err(reason) => println(reason)
    }
}''')
        self.assertEqual(output, "start\nfinish\nfinish\n3\n")

    def test_actor_crash_is_isolated_and_reported_as_result(self) -> None:
        output, _ = self.run_source('''actor Broken {
    on fail() { let value: Float = 1 / 0 }
}
actor Healthy {
    on work() { println("healthy") }
}
fn main() {
    let broken = spawn(Broken())
    let healthy = spawn(Healthy())
    broken.fail()
    broken.fail()
    healthy.work()
    match runActors() {
        Ok(count) => println(count)
        Err(reason) => println(reason)
    }
    println(broken.isAlive())
    println(healthy.isAlive())
}''')
        self.assertEqual(output, "healthy\nactor 'Broken' crashed at actors.tyv:2:38: division by zero\nfalse\ntrue\n")

    def test_stop_drops_pending_messages_and_prevents_new_sends(self) -> None:
        source = '''actor Worker {
    on work() { println("ran") }
}
fn main() {
    let worker = spawn(Worker())
    worker.work()
    worker.stop()
    println(worker.isAlive())
    worker.work()
}'''
        program, _ = compile_source(source, "stopped.tyv")
        output = io.StringIO()
        with self.assertRaisesRegex(Diagnostic, "cannot send a message to a stopped actor"):
            Interpreter(program, output).run()
        self.assertEqual(output.getvalue(), "false\n")

    def test_actor_types_and_message_parameters_are_checked(self) -> None:
        self.reject('''actor Counter { on add(value: Int) {} }
fn main() { let counter = spawn(Counter()); counter.add("wrong") }''', "expects Int, found String")
        self.reject('''actor Counter { on add(value: Int) {} }
fn main() { let counter = spawn(Counter()); counter.missing() }''', "no message handler 'missing'")
        self.reject('''actor Counter {}
fn main() { let counter: Counter = spawn(Counter()) }''', "only be used inside ActorRef")
        self.reject('''actor Counter {}
fn main() { let counter: ActorRef<String> = spawn(Counter()) }''', "requires one actor type")
        self.reject('''class Box { let value: Int
fn init(value: Int) { self.value = value } }
actor Holder { on keep(value: Box) {} }
fn main() {}''', "cannot carry mutable or process-local value type 'Box'")
        self.reject('''actor Holder { on keep(values: List<Int>) {} }
fn main() {}''', "cannot carry mutable or process-local value type 'List<Int>'")
        self.reject('''actor Counter { var value: Int
init(start: Int) { self.value = start } }
fn main() { let counter = spawn(Counter(1)); println(counter.value) }''', "has no field 'value'")

    def test_actor_constructor_fields_are_definitely_initialized(self) -> None:
        self.reject('''actor Counter {
    let value: Int
    init(start: Int) { if start > 0 { self.value = start } }
}
fn main() {}''', "does not initialize field 'value' on all paths")

    def test_pub_actor_can_be_imported_from_a_project_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tyvera.toml").write_text('name = "actors_app"\nentry = "main.tyv"\n', encoding="utf-8")
            (root / "counter.tyv").write_text('''pub actor Counter {
    var value: Int
    init(start: Int) { self.value = start }
    on report() { println(self.value) }
}
''', encoding="utf-8")
            entry = root / "main.tyv"
            entry.write_text('''import "counter.tyv" as counters
fn main() {
    let counter: ActorRef<counters.Counter> = spawn(counters.Counter(7))
    counter.report()
    match runActors() {
        Ok(count) => println(count)
        Err(reason) => println(reason)
    }
}
''', encoding="utf-8")
            program, _ = compile_project(entry, root)
            output = io.StringIO()
            Interpreter(program, output, project_root=root).run()
            self.assertEqual(output.getvalue(), "7\n1\n")


if __name__ == "__main__":
    unittest.main()
