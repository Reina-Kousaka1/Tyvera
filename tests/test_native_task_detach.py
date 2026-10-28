from __future__ import annotations

import pathlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests import test_native_compiler as native
from tyvera.compiler import compile_checked_source
from tyvera.compiler import compile_checked_project
from tyvera.cpp_backend import CppNativeBackend
from tyvera.cpp_backend import emit_cpp
from tyvera.lowering import lower_checked_program


GATE = '''async fn waitFlag(flag: List<Bool>) {
    var attempts: Int = 0
    while flag.get(0) != Some(true) {
        if attempts >= 1000 { let stalled: Float = 1 / 0 }
        await sleepFor(milliseconds(1))
        attempts = attempts + 1
    }
}
'''


class NativeTaskDetachTests(unittest.TestCase):
    def differential(self, source: str, expected: str, *, repeats: int = 1) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-detach-") as temporary:
            root = pathlib.Path(temporary)
            reference = native.NativeExecutionTests._reference_result(source, root)
            self.assertEqual(reference, (0, expected, ""))
            cpp = emit_cpp(lower_checked_program(compile_checked_source(source, "native-io.tyv")), project_root=root)
            self.assertIn("detach_from", cpp)
            if native.HAS_CPP_COMPILER:
                executable = root / "detach"
                result = native.NativeExecutionTests._native_source_result(self, source, root, executable, root)
                self.assertEqual((result.returncode, result.stdout, result.stderr), reference)
                for _ in range(repeats - 1):
                    result = subprocess.run([str(executable)], cwd=root, capture_output=True,
                                            text=True, timeout=15, check=False)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), reference)

    def test_detached_child_outlives_parent_and_leaves_every_owner_group(self) -> None:
        self.differential(GATE + '''async fn child(flag: List<Bool>) {
    await waitFlag(flag)
    println("child finished")
}
async fn owner(flag: List<Bool>) -> Int {
    let first = taskGroup()
    let second = taskGroup()
    let task = child(flag)
    first.add(task)
    second.add(task)
    println(task.detach())
    println(first.isDone())
    println(second.cancel())
    await first
    await second
    println("owner ended")
    return 7
}
async fn controller() {
    let flag = List<Bool>()
    flag.append(false)
    let value: Int = await owner(flag)
    println(value)
    flag.set(0, true)
}
fn main() {
    let task = controller()
    match runTasks() {
        Ok(_) => println("drained")
        Err(error) => eprintln(error)
    }
}''', "true\ntrue\n0\nowner ended\n7\nchild finished\ndrained\n", repeats=4)

    def test_detach_is_owner_only_and_rejects_root_terminal_and_second_detach(self) -> None:
        self.differential('''actor Gate { on unused() {} }
async fn child(pending: ReplyReceiver<String>) { let reason: String = await pending }
async fn intruder(task: Task<Void>) -> Bool { return task.detach() }
async fn owner() {
    let gate = spawn(Gate())
    let task = child(gate.monitor())
    let stolen: Bool = await intruder(task)
    println(stolen)
    println(task.detach())
    println(task.detach())
    gate.stop()
}
fn main() {
    let task = owner()
    println(task.detach())
    runTasks()
    println(task.isDone())
    println(task.detach())
}''', "false\nfalse\ntrue\nfalse\ntrue\nfalse\n", repeats=4)

    def test_parent_failure_does_not_cancel_detached_reply_waiter(self) -> None:
        self.differential('''actor Gate { on unused() {} }
async fn survivor(pending: ReplyReceiver<String>) {
    let reason: String = await pending
    println("survived")
}
async fn owner() {
    let gate = spawn(Gate())
    let task = survivor(gate.monitor())
    println(task.detach())
    gate.stop()
    let broken: Float = 1 / 0
}
fn main() {
    let task = owner()
    match runTasks() {
        Ok(_) => println("missing failure")
        Err(_) => println("failure reported")
    }
    match task.tryResult() {
        Err(_) => println("owner failed")
        _ => println("unexpected owner state")
    }
}''', "true\nsurvived\nfailure reported\nowner failed\n", repeats=4)

    def test_explicit_parent_cancellation_does_not_reclaim_detached_child(self) -> None:
        self.differential('''actor Gate { on unused() {} }
async fn survivor(pending: ReplyReceiver<String>) {
    let reason: String = await pending
    println("survived")
}
async fn owner(gate: ActorRef<Gate>, ready: List<Bool>) {
    let child = survivor(gate.monitor())
    println(child.detach())
    ready.set(0, true)
    let reason: String = await gate.monitor()
    println("unexpected parent completion")
}
async fn controller() {
    let gate = spawn(Gate())
    let ready = List<Bool>()
    ready.append(false)
    let parent = owner(gate, ready)
    await waitFlag(ready)
    println(parent.cancel())
    gate.stop()
}
fn main() {
    let task = controller()
    match runTasks() {
        Ok(_) => println("drained")
        Err(error) => eprintln(error)
    }
}'''.replace("actor Gate", GATE + "actor Gate", 1), "true\ntrue\nsurvived\ndrained\n", repeats=4)

    @unittest.skipUnless(native.HAS_CPP_COMPILER, "native task capacity requires a C++17 compiler")
    def test_detachment_does_not_bypass_unfinished_task_limit(self) -> None:
        source = '''actor Gate { on unused() {} }
async fn child(pending: ReplyReceiver<String>) { let reason: String = await pending }
async fn owner() {
    let gate = spawn(Gate())
    var index: Int = 0
    while index < 65 {
        let task = child(gate.monitor())
        task.detach()
        index = index + 1
    }
}
fn main() {
    let task = owner()
    runTasks()
    match task.tryResult() {
        Err(reason) => eprintln(reason)
        _ => println("missing capacity failure")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-detach-capacity-") as temporary:
            root = pathlib.Path(temporary)
            result = native.NativeExecutionTests._native_source_result(self, source, root, root / "limit", root)
            self.assertEqual(result.returncode, 0)
            self.assertIn("native task capacity exceeded (maximum 64 unfinished tasks per run)", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_detached_failure_is_reported_without_failing_completed_owner(self) -> None:
        self.differential('''actor Gate { on unused() {} }
async fn broken(pending: ReplyReceiver<String>) {
    let reason: String = await pending
    let failure: Float = 1 / 0
}
async fn owner(gate: ActorRef<Gate>) -> Int {
    let task = broken(gate.monitor())
    println(task.detach())
    return 7
}
async fn controller() {
    let gate = spawn(Gate())
    let value: Int = await owner(gate)
    println(value)
    gate.stop()
}
fn main() {
    let task = controller()
    match runTasks() {
        Ok(_) => println("missing failure")
        Err(_) => println("detached failure reported")
    }
    match task.tryResult() {
        Ok(true) => println("controller completed")
        _ => println("unexpected controller state")
    }
}''', "true\n7\ndetached failure reported\ncontroller completed\n", repeats=4)

    def test_detached_descendant_outlives_actor_stop_but_actor_owned_parent_is_cancelled(self) -> None:
        self.differential('''actor Gate { on unused() {} }
async fn survivor(pending: ReplyReceiver<String>) {
    let reason: String = await pending
    println("survived")
}
async fn actorWork(gate: ActorRef<Gate>, reply: ReplySender<Int>) {
    let child = survivor(gate.monitor())
    println(child.detach())
    reply.send(1)
    await sleepFor(seconds(60))
    println("actor task leaked")
}
actor Owner {
    on start(gate: ActorRef<Gate>, reply: ReplySender<Int>) {
        let task = actorWork(gate, reply)
    }
}
async fn controller() {
    let gate = spawn(Gate())
    let owner = spawn(Owner())
    let started: Int = await owner.request(start, gate)
    owner.stop()
    gate.stop()
}
fn main() {
    let task = controller()
    match runTasks() {
        Ok(_) => println("drained")
        Err(error) => eprintln(error)
    }
}''', "true\nsurvived\ndrained\n", repeats=4)

    def test_resource_moved_into_detached_task_closes_after_explicit_cancellation(self) -> None:
        self.differential(GATE + '''async fn writePending(file: TextWriter, written: List<Bool>, release: List<Bool>) {
    file.write("payload")
    written.set(0, true)
    await waitFlag(release)
}
async fn owner(file: TextWriter, written: List<Bool>, release: List<Bool>) -> Task<Void> {
    let task = writePending(move file, written, release)
    println(task.detach())
    return task
}
async fn controller() {
    let written = List<Bool>()
    written.append(false)
    let release = List<Bool>()
    release.append(false)
    match openTextWriter("detached.txt", "write") {
        Err(error) => eprintln(error)
        Ok(file) => {
            let task: Task<Void> = await owner(move file, written, release)
            println("owner returned")
            await waitFlag(written)
            println(task.cancel())
        }
    }
}
fn main() {
    let task = controller()
    match runTasks() {
        Ok(_) => println("drained")
        Err(error) => eprintln(error)
    }
    match readText("detached.txt") {
        Ok(text) => println(text)
        Err(error) => eprintln(error)
    }
}''', "true\nowner returned\ntrue\ndrained\npayload\n", repeats=3)

    def test_many_detached_roots_remain_registered_until_their_actor_reply_arrives(self) -> None:
        self.differential('''actor Gate { on unused() {} }
async fn child(pending: ReplyReceiver<String>) {
    let reason: String = await pending
    println("child")
}
async fn owner(gate: ActorRef<Gate>) {
    var index: Int = 0
    while index < 40 {
        let task = child(gate.monitor())
        if !task.detach() { println("missed detach") }
        index = index + 1
    }
}
async fn controller() {
    let gate = spawn(Gate())
    await owner(gate)
    println("owner returned")
    gate.stop()
}
fn main() {
    let task = controller()
    match runTasks() {
        Ok(_) => println("drained")
        Err(error) => eprintln(error)
    }
}''', "owner returned\n" + "child\n" * 40 + "drained\n", repeats=3)

    @unittest.skipUnless(sys.platform.startswith("linux") and native.HAS_CPP_COMPILER,
                         "native detached shutdown integration requires Linux and C++17")
    def test_sigint_sigterm_wake_detached_sleep_and_close_owned_resource_like_reference(self) -> None:
        source = '''async fn child() {
    using writer = openTextWriter("owned.txt", "write") {
        writer.write("closed during shutdown")
        writeText("ready.txt", "ready")
        await sleepFor(seconds(60))
        println("unexpected wake")
    }
}
async fn owner() {
    let task = child()
    println(task.detach())
}
fn main() {
    let task = owner()
    runTasks()
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-detached-shutdown-") as temporary:
            results = {}
            for number in (signal.SIGINT, signal.SIGTERM):
                for backend in ("reference", "native"):
                    with self.subTest(signal=number, backend=backend):
                        root = pathlib.Path(temporary) / f"{backend}-{number}"
                        root.mkdir()
                        entry = root / "signal.tyv"
                        entry.write_text(source, encoding="utf-8")
                        environment = os.environ.copy()
                        if backend == "native":
                            executable = root / "signal-app"
                            checked = compile_checked_project(entry, root)
                            CppNativeBackend().build(lower_checked_program(checked), executable, project_root=root)
                            command = [str(executable)]
                            environment.pop("PYTHONPATH", None)
                            environment.pop("PYTHONHOME", None)
                            environment["PATH"] = ""
                        else:
                            command = [sys.executable, "-m", "tyvera", "run", str(entry)]
                            environment["PYTHONPATH"] = str(pathlib.Path(__file__).resolve().parents[1])
                        process = subprocess.Popen(command, cwd=root, env=environment,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   text=True, encoding="utf-8")
                        try:
                            deadline = time.monotonic() + 5
                            while not (root / "ready.txt").exists() and process.poll() is None and time.monotonic() < deadline:
                                time.sleep(0.01)
                            self.assertTrue((root / "ready.txt").exists(), "detached task did not acquire its resource")
                            process.send_signal(number)
                            output, errors = process.communicate(timeout=5)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.wait(timeout=5)
                        self.assertEqual(process.returncode, 128 + number, errors)
                        self.assertEqual(output, "true\n")
                        self.assertIn(f"runtime shutdown requested by {signal.Signals(number).name}", errors)
                        self.assertNotIn("Traceback", errors)
                        self.assertEqual((root / "owned.txt").read_text(), "closed during shutdown")
                        results[backend, number] = (process.returncode, output, errors)
                self.assertEqual(results["native", number], results["reference", number])


if __name__ == "__main__":
    unittest.main()
