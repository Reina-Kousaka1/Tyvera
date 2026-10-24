from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class ResourceScopeTests(unittest.TestCase):
    def run_source(self, source: str, root: Path) -> str:
        program, _ = compile_source(source, "resource-test.tyv")
        output = io.StringIO()
        Interpreter(program, output, project_root=root).run()
        return output.getvalue()

    def test_text_file_scope_writes_then_closes_on_normal_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''fn main() {
    using file = openText("message.txt", "write") {
        match file.write("saved") {
            Ok(_) => println(file.isOpen())
            Err(_) => println("write failed")
        }
    }
    match readText("message.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "true\nsaved\n")
            self.assertEqual((root / "message.txt").read_text(encoding="utf-8"), "saved")

    def test_read_mode_reads_utf8_and_rejects_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.txt").write_text("Märchen", encoding="utf-8")
            output = self.run_source('''fn main() {
    using file = openText("input.txt", "read") {
        match file.readAll() {
            Ok(contents) => println(contents)
            Err(_) => println("read failed")
        }
        match file.write("ignored") {
            Ok(_) => println("unexpected write")
            Err(_) => println("read only")
        }
    }
}''', root)
            self.assertEqual(output, "Märchen\nread only\n")
            self.assertEqual((root / "input.txt").read_text(encoding="utf-8"), "Märchen")

    def test_scope_cleanup_runs_on_sync_return_and_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''fn writeAndReturn() -> Int {
    using file = openText("return.txt", "write") {
        file.write("returned")
        return 7
    }
}
fn main() {
    println(writeAndReturn())
    match readText("return.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "7\nreturned\n")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, _ = compile_source('''fn main() {
    using file = openText("failure.txt", "write") {
        file.write("closed after error")
        let invalid = 1 / 0
    }
}''', "resource-failure.tyv")
            with self.assertRaisesRegex(Diagnostic, "division by zero"):
                Interpreter(program, io.StringIO(), project_root=root).run()
            self.assertEqual((root / "failure.txt").read_text(encoding="utf-8"), "closed after error")

    def test_async_task_failure_closes_active_resource(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''async fn fail() {
    using file = openText("async-failure.txt", "write") {
        file.write("closed on task failure")
        let invalid = 1 / 0
    }
}
fn main() {
    let task = fail()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("failed")
    }
    println(task.isDone())
    match readText("async-failure.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "failed\ntrue\nclosed on task failure\n")

    def test_async_return_closes_active_resource(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''async fn finish() -> Int {
    using file = openText("async-return.txt", "write") {
        file.write("closed on return")
        return 9
    }
}
fn main() {
    let task = finish()
    runTasks()
    match task.tryResult() {
        Ok(Some(value)) => println(value)
        _ => println("task failed")
    }
    match readText("async-return.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "9\nclosed on return\n")

    def test_loop_break_closes_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''fn main() {
    while true {
        using file = openText("break.txt", "write") {
            file.write("closed on break")
            break
        }
    }
    match readText("break.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "closed on break\n")

    def test_task_cancellation_closes_suspended_resource(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''async fn holder() {
    using file = openText("cancelled.txt", "write") {
        file.write("closed on cancellation")
        await sleepFor(seconds(1))
    }
}
async fn cancel(target: Task) {
    await sleepFor(milliseconds(1))
    target.cancel()
}
fn main() {
    let target = holder()
    let cancellation = cancel(target)
    runTasks()
    println(target.isCancelled())
    match readText("cancelled.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "true\nclosed on cancellation\n")

    def test_resource_handles_cannot_escape_or_be_acquired_outside_using(self) -> None:
        invalid_programs = (
            ('''fn main() {
    let file = openText("x.txt", "write")
}''', "must be acquired by a 'using' scope"),
            ('''fn main() {
    using file = openText("x.txt", "write") {
        let alias = file
    }
}''', "can only be used as the receiver"),
            ('''fn main() {
    using file = openText("x.txt", "write") {
        file
    }
}''', "only a function call can be used"),
        )
        for source, message in invalid_programs:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Diagnostic, message):
                    compile_source(source, "invalid-resource.tyv")

    def test_scoped_resources_have_no_public_equality(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "can only be used as the receiver"):
            compile_source('''fn main() {
    using file = openText("x.txt", "write") {
        println(file == file)
    }
}''', "resource-equality.tyv")

    def test_explicit_close_is_idempotent_and_use_after_close_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''fn main() {
    using file = openText("closed.txt", "write") {
        println(file.close())
        println(file.close())
        println(file.isOpen())
    }
}''', root)
            self.assertEqual(output, "true\nfalse\nfalse\n")

            program, _ = compile_source('''fn main() {
    using file = openText("closed.txt", "write") {
        file.close()
        file.write("invalid")
    }
}''', "closed-resource.tyv")
            with self.assertRaisesRegex(Diagnostic, "TextFile is closed"):
                Interpreter(program, io.StringIO(), project_root=root).run()

    def test_open_text_errors_can_be_handled_without_leaking_secret_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".tyvera.secrets").write_text('TOKEN = "private-value"\n', encoding="utf-8")
            output = self.run_source('''fn main() {
    using file = openText(".tyvera.secrets", "read") {
        file.readAll()
    } else error {
        println(error)
    }
}''', root)
            self.assertEqual(output, "secret file can only be accessed through the secrets provider\n")
            self.assertNotIn("private-value", output)

            program, _ = compile_source('''fn main() {
    using file = openText("missing.txt", "read") {
        file.readAll()
    }
}''', "missing-resource.tyv")
            with self.assertRaises(Diagnostic) as caught:
                Interpreter(program, io.StringIO(), project_root=root).run()
            self.assertIn("cannot open text file", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
