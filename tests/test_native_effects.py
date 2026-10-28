from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest

from tests import test_native_compiler as native
from tyvera.compiler import compile_checked_source
from tyvera.cpp_backend import emit_cpp
from tyvera.lowering import lower_checked_program
from tyvera.native_effects import independent_scalar_functions


class NativeScalarExecutionTests(unittest.TestCase):
    def module(self, source: str):
        return lower_checked_program(compile_checked_source(source, "native-io.tyv"))

    def names(self, module) -> set[str]:
        proven = independent_scalar_functions(module)
        return {function.name for function in module.functions if function.symbol in proven}

    def differential(self, source: str, expected: str | None = None) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-scalar-task-") as temporary:
            root = pathlib.Path(temporary)
            reference = native.NativeExecutionTests._reference_result(source, root)
            self.assertEqual(reference[0], 0, reference)
            if expected is not None:
                self.assertEqual(reference, (0, expected, ""))
            emit_cpp(self.module(source), project_root=root)
            if native.HAS_CPP_COMPILER:
                executable = root / "scalar-tasks"
                result = native.NativeExecutionTests._native_source_result(self, source, root, executable, root)
                self.assertEqual((result.returncode, result.stdout, result.stderr), reference)
                for _ in range(3):
                    result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                            text=True, timeout=10, check=False)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), reference)

    def test_scalar_proof_follows_sync_callees_and_preserves_local_control_flow(self) -> None:
        module = self.module('''async fn compute(value: Int) -> Int { return outer(value) }
fn outer(value: Int) -> Int { return inner(value) }
fn inner(value: Int) -> Int {
    var count: Int = 0
    var result: Int = 0
    while count < value {
        count = count + 1
        if count == 2 { continue }
        { let local: Int = count; result = result + local }
        if count > 10 { break }
    }
    return result
}
async fn text(value: String) -> String { return value + "!" }
async fn byteValue(value: Byte) -> Byte { return value }
async fn durationValue(value: Duration) -> Duration { return value }
fn main() {}''')
        self.assertEqual(self.names(module), {"compute", "outer", "inner", "text", "byteValue", "durationValue", "main"})
        generated = emit_cpp(module)
        self.assertEqual(sum(line.strip().startswith("}, false,") for line in generated.splitlines()), 4)

    def test_shared_values_resources_actor_fields_and_waits_are_never_marked_independent(self) -> None:
        module = self.module('''async fn listValue(value: List<Int>) { value.append(1) }
async fn mapValue(value: Map<String,Int>) { value.put("key", 1) }
async fn bufferValue(value: ByteBuffer, byte: Byte) { value.set(0, byte) }
async fn headersValue(value: HttpHeaders) { value.set("x-test", "value") }
async fn fileValue(value: TextWriter) { value.write("value") }
async fn waiting() { await sleepFor(milliseconds(1)) }
async fn output() { println("value") }
actor Counter {
    var count: Int
    init() { self.count = 0 }
    on tick() { self.count = self.count + 1 }
}
fn main() {}''')
        self.assertEqual(self.names(module), {"main"})
        self.assertFalse(any(line.strip().startswith("}, false,") for line in emit_cpp(module).splitlines()))

    def test_unknown_effects_recursion_and_effectful_sync_callees_remain_serialized(self) -> None:
        module = self.module('''fn recursive(value: Int) -> Int {
    if value == 0 { return 0 }; return recursive(value - 1)
}
fn printValue(value: Int) -> Int { println(value); return value }
async fn recursiveTask() -> Int { return recursive(3) }
async fn outputTask() -> Int { return printValue(3) }
async fn timeTask() -> Int { return monotonicMilliseconds() }
async fn caller() { let child = recursiveTask() }
fn main() {}''')
        self.assertEqual(self.names(module), {"main"})

    def test_locally_created_aliases_and_transitive_effects_retain_serialization(self) -> None:
        module = self.module('''fn hiddenMutation() -> Int {
    let values = List<Int>()
    let alias = values
    alias.append(1)
    return values.length
}
async fn localMutation() -> Int { return hiddenMutation() }
async fn createsTask() { let task = localMutation() }
async fn groupScope() { let group = taskGroup() }
async fn readsEnvironment() { let value = environment("TYVERA_EFFECT_TEST") }
fn main() {}''')
        self.assertEqual(self.names(module), {"main"})

    def test_all_allowed_scalar_types_keep_reference_values(self) -> None:
        self.differential('''async fn floatValue(value: Float) -> Float { return value * 2.0 }
async fn boolValue(value: Bool) -> Bool { return !value }
async fn byteValue(value: Byte) -> Byte { return value }
async fn durationValue(value: Duration) -> Duration { return value }
async fn noOp() {}
async fn controller() {
    let number: Float = await floatValue(1.25)
    let flag: Bool = await boolValue(false)
    let duration: Duration = await durationValue(milliseconds(7))
    await noOp()
    println(number)
    println(flag)
    println(duration.inMilliseconds())
    match byteFromInt(255) {
        Ok(value) => { let byte: Byte = await byteValue(value); println(byte) }
        Err(_) => println("bad byte")
    }
}
fn main() { let task = controller(); runTasks() }''', "2.5\ntrue\n7\n255\n")

    def test_scalar_workers_and_shared_mutable_controller_have_matching_values(self) -> None:
        self.differential('''fn sum(limit: Int) -> Int {
    var index: Int = 0
    var total: Int = 0
    while index < limit { index = index + 1; total = total + index }
    return total
}
async fn compute(limit: Int) -> Int { return sum(limit) }
async fn decorate(value: String) -> String { return value + "!" }
async fn controller() {
    let first = compute(100)
    let second = compute(200)
    let text = decorate("scalar")
    let a: Int = await first
    let b: Int = await second
    let result: String = await text
    let values = List<Int>()
    values.append(a)
    values.append(b)
    match values.get(0) { Some(value) => println(value)
        None => println("missing first") }
    match values.get(1) { Some(value) => println(value)
        None => println("missing second") }
    println(result)
}
fn main() { let task = controller(); runTasks() }''', "5050\n20100\nscalar!\n")

    def test_independent_failure_retains_source_and_owner_failure_propagation(self) -> None:
        source = '''async fn overflow() -> Int { return 9223372036854775807 + 1 }
async fn controller() { let child = overflow(); let value: Int = await child; println(value) }
fn main() { let task = controller(); match runTasks() { Ok(_) => println("unexpected")
    Err(error) => println(error) } }'''
        self.assertIn("overflow", self.names(self.module(source)))
        self.differential(source)

    @unittest.skipUnless(native.HAS_CPP_COMPILER, "native end-to-end checks require a C++17 compiler")
    def test_sync_scalar_callee_observes_task_cancellation(self) -> None:
        source = """fn spin() { while true {} }
async fn worker() { spin() }
async fn controller() {
    let child = worker()
    let timed = child.withTimeout(milliseconds(100))
    await timed
}
fn main() { let task = controller(); match runTasks() { Ok(_) => println("unexpected")
    Err(error) => println(error) } }"""
        self.assertIn("spin", self.names(self.module(source)))
        with tempfile.TemporaryDirectory(prefix="tyvera-sync-cancel-") as temporary:
            root = pathlib.Path(temporary)
            result = native.NativeExecutionTests._native_source_result(
                self, source, root, root / "sync-cancel", root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("unexpected", result.stdout)
            self.assertIn("timed out", result.stdout.lower())

    def test_independent_loop_still_observes_task_timeout_and_cancellation(self) -> None:
        source = '''async fn spin() { while true {} }
async fn controller() {
    let child = spin()
    let timed = child.withTimeout(milliseconds(100))
    await timed
}
fn main() { let task = controller(); match runTasks() { Ok(_) => println("unexpected")
    Err(error) => println(error) } }'''
        self.assertIn("spin", self.names(self.module(source)))
        self.differential(source)


if __name__ == "__main__":
    unittest.main()
