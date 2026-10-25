from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tyvera.compiler import compile_checked_source, compile_source
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

    def test_owned_resource_must_be_explicitly_moved_and_scoped_resource_cannot_escape(self) -> None:
        compile_source('''fn main() {
    let file = openText("x.txt", "write")
}''', "owned-resource.tyv")

        invalid_programs = (
            ('''fn main() {
    using file = openText("x.txt", "write") {
        let alias = move file
    }
}''', "scoped resource 'file' cannot be moved"),
            ('''fn main() {
    let opened = openText("x.txt", "write")
    let alias = opened
}''', "Resource 'opened' is move-only"),
        )
        for source, message in invalid_programs:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Diagnostic, message):
                    compile_source(source, "invalid-resource.tyv")

    def test_scoped_resources_have_no_public_equality(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "not value-comparable"):
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

            with self.assertRaisesRegex(Diagnostic, "already closed"):
                compile_source('''fn main() {
    using file = openText("closed.txt", "write") {
        file.close()
        file.write("invalid")
    }
}''', "closed-resource.tyv")

    def test_move_transfer_across_functions_and_safe_owned_return(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''fn relay(resource: Result<TextFile,String>) -> Result<TextFile,String> {
    return move resource
}
fn consume(file: TextFile) {
    file.write("transferred")
}
fn main() {
    let opened = openText("transferred.txt", "write")
    let forwarded = relay(move opened)
    match move forwarded {
        Ok(file) => consume(move file)
        Err(error) => println(error)
    }
    match readText("transferred.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "transferred\n")

    def test_use_after_move_double_move_and_branch_sensitive_move_are_rejected(self) -> None:
        invalid_programs = (
            ('''fn take(resource: Result<TextFile,String>) {}
fn main() {
    let opened = openText("x.txt", "write")
    take(move opened)
    take(move opened)
}''', "already moved"),
            ('''fn take(resource: Result<TextFile,String>) {}
fn main() {
    let opened = openText("x.txt", "write")
    take(move opened)
    let again = move opened
}''', "already moved"),
            ('''fn take(resource: Result<TextFile,String>) {}
fn maybe(flag: Bool, opened: Result<TextFile,String>) {
    if flag { take(move opened) }
    take(move opened)
}
fn main() {}''', "may have been moved on another control-flow path"),
        )
        for source, message in invalid_programs:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Diagnostic, message):
                    compile_source(source, "invalid-move.tyv")

    def test_branch_and_loop_ownership_flow_is_conservative(self) -> None:
        compile_source('''fn take(resource: Result<TextFile,String>) {}
fn choose(flag: Bool, opened: Result<TextFile,String>) {
    if flag { take(move opened) } else { take(move opened) }
}
fn main() {}''', "both-branches-move.tyv")
        with self.assertRaisesRegex(Diagnostic, "may have been moved"):
            compile_source('''fn take(resource: Result<TextFile,String>) {}
fn repeat(flag: Bool, opened: Result<TextFile,String>) {
    while flag { take(move opened) }
    take(move opened)
}
fn main() {}''', "loop-move.tyv")

    def test_resource_carriers_are_supported_but_collections_and_tuples_are_rejected(self) -> None:
        invalid_programs = (
            ('''fn main() { let files: List<TextFile> = List<TextFile>() }''', "cannot contain move-only Resource"),
            ('''fn main() { let files: Map<String,TextFile> = Map<String,TextFile>() }''', "cannot contain move-only Resource"),
            ('''fn main() { let files: Map<TextFile,String> = Map<TextFile,String>() }''', "not hashable and cannot be used as a Map key"),
            ('''fn main() {
    let opened = openText("x.txt", "write")
    let pair = (move opened, 1)
}''', "tuples cannot contain move-only Resource"),
        )
        for source, message in invalid_programs:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Diagnostic, message):
                    compile_source(source, "invalid-container.tyv")

    def test_option_and_result_carriers_preserve_single_resource_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''fn main() {
    let acquired = openText("option-resource.txt", "write")
    let maybe = Some(move acquired)
    match move maybe {
        Some(result) => match move result {
            Ok(file) => file.write("owned through Option and Result")
            Err(error) => println(error)
        }
        None => println("no resource")
    }
    match readText("option-resource.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "owned through Option and Result\n")
            self.assertEqual((root / "option-resource.txt").read_text(encoding="utf-8"),
                             "owned through Option and Result")

    def test_user_types_and_generic_functions_cannot_hide_or_copy_resources(self) -> None:
        invalid_programs = (
            ('''record Holder { file: TextFile }
fn main() {}''', "records cannot contain move-only Resource"),
            ('''enum Holder { Has(TextFile), Empty }
fn main() {}''', "enum payloads cannot contain move-only Resource"),
            ('''newtype Holder = TextFile
fn main() {}''', "newtypes cannot wrap move-only Resource"),
            ('''fn identity<T>(value: T) -> T { return value }
fn main() {
    let opened = openText("x.txt", "write")
    let returned = identity(move opened)
}''', "generic parameter 'T' in 'identity' cannot contain a move-only Resource"),
            ('''class Box<T> {
    let value: T
    fn init(value: T) { self.value = value }
}
fn main() {
    let opened = openText("x.txt", "write")
    let box = Box(move opened)
}''', "generic parameter 'T' in 'Box' cannot contain a move-only Resource"),
        )
        for source, message in invalid_programs:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Diagnostic, message):
                    compile_source(source, "hidden-resource.tyv")

    def test_resource_equality_is_rejected_for_values_and_result_carriers(self) -> None:
        invalid_programs = (
            '''fn main() {
    let opened = openText("x.txt", "write")
    let other = openText("y.txt", "write")
    let equal = opened == other
}''',
            '''fn main() {
    let opened = openText("x.txt", "write")
    let other = openText("y.txt", "write")
    let equal = move opened == move other
}''',
        )
        for source in invalid_programs:
            with self.subTest(source=source):
                with self.assertRaisesRegex(Diagnostic, "not value-comparable"):
                    compile_source(source, "resource-equality.tyv")

    def test_cleanup_closes_owned_resources_in_lifo_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            closed: list[str] = []
            close = Interpreter._close_text_file

            def track_close(resource):
                closed.append(Path(resource.stream.name).name)
                return close(resource)

            program, _ = compile_source('''fn main() {
    let first = openText("first.txt", "write")
    let second = openText("second.txt", "write")
    var older = openText("older.txt", "write")
    var newer = openText("newer.txt", "write")
    let temporary = move older
    older = move newer
    newer = move temporary
}''', "lifo-cleanup.tyv")
            with patch.object(Interpreter, "_close_text_file", staticmethod(track_close)):
                Interpreter(program, io.StringIO(), project_root=root).run()
            self.assertEqual(closed, ["newer.txt", "older.txt", "second.txt", "first.txt"])

    def test_owned_resource_cleanup_runs_on_return_runtime_error_and_loop_control(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''fn returnEarly() -> Int {
    let file = openText("return-owned.txt", "write")
    return 7
}
fn main() {
    println(returnEarly())
    while true {
        let file = openText("break-owned.txt", "write")
        break
    }
    var index = 0
    while index < 1 {
        let file = openText("continue-owned.txt", "write")
        index = index + 1
        continue
    }
    match readText("return-owned.txt") {
        Ok(_) => println("return closed")
        Err(_) => println("return open")
    }
    match readText("break-owned.txt") {
        Ok(_) => println("break closed")
        Err(_) => println("break open")
    }
    match readText("continue-owned.txt") {
        Ok(_) => println("continue closed")
        Err(_) => println("continue open")
    }
}''', root)
            self.assertEqual(output, "7\nreturn closed\nbreak closed\ncontinue closed\n")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, _ = compile_source('''fn main() {
    let opened = openText("failure-owned.txt", "write")
    let invalid = 1 / 0
}''', "failure-owned.tyv")
            with self.assertRaisesRegex(Diagnostic, "division by zero"):
                Interpreter(program, io.StringIO(), project_root=root).run()
            self.assertTrue((root / "failure-owned.txt").exists())
            self.assertEqual((root / "failure-owned.txt").read_text(encoding="utf-8"), "")

    def test_failed_argument_evaluation_and_call_depth_limit_release_transferred_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, _ = compile_source('''fn accept(opened: Result<TextFile,String>, value: Int) {}
fn main() {
    let resource = openText("argument-failure.txt", "write")
    accept(move resource, 1 % 0)
}''', "argument-failure.tyv")
            with self.assertRaisesRegex(Diagnostic, "remainder by zero"):
                Interpreter(program, io.StringIO(), project_root=root).run()
            self.assertEqual((root / "argument-failure.txt").read_text(encoding="utf-8"), "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, _ = compile_source('''fn accept(opened: Result<TextFile,String>) {}
fn main() {
    let resource = openText("depth-failure.txt", "write")
    accept(move resource)
}''', "depth-failure.tyv")
            with self.assertRaisesRegex(Diagnostic, r"maximum call depth \(1\) exceeded"):
                Interpreter(program, io.StringIO(), project_root=root, max_call_depth=1).run()
            self.assertEqual((root / "depth-failure.txt").read_text(encoding="utf-8"), "")

    def test_cleanup_failures_are_reported_without_replacing_the_primary_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, _ = compile_source('''fn main() {
    let opened = openText("cleanup-error.txt", "write")
    let invalid = 1 % 0
}''', "cleanup-error.tyv")
            close = Interpreter._close_text_file

            def close_with_error(resource):
                close(resource)
                return "simulated close failure"

            with patch.object(Interpreter, "_close_text_file", staticmethod(close_with_error)):
                with self.assertRaisesRegex(
                    Diagnostic,
                    "remainder by zero; resource cleanup also failed: cleanup-error.tyv:",
                ):
                    Interpreter(program, io.StringIO(), project_root=root).run()
            self.assertEqual((root / "cleanup-error.txt").read_text(encoding="utf-8"), "")

    def test_async_owned_resource_cleanup_on_failure_cancellation_and_detached_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''async fn fail(opened: Result<TextFile,String>) {
    match move opened {
        Ok(file) => {
            file.write("failed task")
            let invalid = 1 / 0
        }
        Err(error) => println(error)
    }
}
async fn hold(opened: Result<TextFile,String>) {
    match move opened {
        Ok(file) => {
            file.write("cancelled task")
            await sleepFor(seconds(1))
        }
        Err(error) => println(error)
    }
}
async fn cancel(target: Task) {
    await sleepFor(milliseconds(1))
    target.cancel()
}
fn main() {
    let failed = fail(openText("failed-task.txt", "write"))
    match runTasks() {
        Ok(_) => println("unexpected")
        Err(_) => println("failed")
    }
    let target = hold(openText("cancelled-task.txt", "write"))
    let cancellation = cancel(target)
    runTasks()
    println(target.isCancelled())
    match readText("failed-task.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
    match readText("cancelled-task.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "failed\ntrue\nfailed task\ncancelled task\n")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program, _ = compile_source('''async fn hold(opened: Result<TextFile,String>) {
    match move opened {
        Ok(file) => {
            file.write("detached shutdown")
            await sleepFor(milliseconds(1))
        }
        Err(_) => {}
    }
}
async fn parent() {
    let resource = openText("detached.txt", "write")
    let task = hold(move resource)
    task.detach()
}
fn main() {
    let parentTask = parent()
    runTasks()
    println(parentTask.isDone())
}''', "detached-cleanup.tyv")
            output = io.StringIO()
            Interpreter(program, output, project_root=root).run()
            self.assertEqual(output.getvalue(), "true\n")
            self.assertEqual((root / "detached.txt").read_text(encoding="utf-8"), "detached shutdown")

    def test_actor_owned_resource_is_cleaned_before_supervisor_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.run_source('''actor Worker {
    on crash() {
        let opened = openText("actor-crash.txt", "write")
        match move opened {
            Ok(file) => {
                file.write("closed before restart")
                let invalid = 1 / 0
            }
            Err(_) => {}
        }
    }
}
fn main() {
    let supervisor = oneForOne(1, 1000)
    let worker = supervisor.start(Worker())
    worker.crash()
    runActors()
    println(worker.isAlive())
    match readText("actor-crash.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
}''', root)
            self.assertEqual(output, "true\nclosed before restart\n")

    def test_resources_are_not_allowed_in_actor_state_or_message_transfer(self) -> None:
        invalid_programs = (
            ('''actor Holder {
    var file: TextFile
    init(file: TextFile) { self.file = file }
}
fn main() {}''', "actor state cannot contain move-only Resource"),
            ('''actor Holder { on keep(file: TextFile) {} }
fn main() {}''', "actor message cannot carry mutable or process-local value type 'TextFile'"),
        )
        for source, message in invalid_programs:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Diagnostic, message):
                    compile_source(source, "actor-resource.tyv")

    def test_text_reader_and_writer_are_distinct_move_only_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.txt").write_text("start", encoding="utf-8")
            output = self.run_source('''fn main() {
    let writer = openTextWriter("input.txt", "append")
    match move writer {
        Ok(file) => {
            file.append(" + end")
            file.flush()
        }
        Err(error) => println(error)
    }
    let reader = openTextReader("input.txt")
    match move reader {
        Ok(file) => match file.readAll() {
            Ok(contents) => println(contents)
            Err(error) => println(error)
        }
        Err(error) => println(error)
    }
}''', root)
            self.assertEqual(output, "start + end\n")
            self.assertEqual((root / "input.txt").read_text(encoding="utf-8"), "start + end")

    def test_text_reader_and_writer_reject_wrong_operations_and_moves_are_tracked(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "TextReader has no method 'write'"):
            compile_source('''fn main() {
    let reader = openTextReader("input.txt")
    match move reader {
        Ok(file) => file.write("wrong")
        Err(_) => {}
    }
}''', "reader-write.tyv")
        with self.assertRaisesRegex(Diagnostic, "TextWriter has no method 'readAll'"):
            compile_source('''fn main() {
    let writer = openTextWriter("input.txt", "write")
    match move writer {
        Ok(file) => file.readAll()
        Err(_) => {}
    }
}''', "writer-read.tyv")

    def test_checked_program_exposes_immutable_ownership_metadata(self) -> None:
        checked = compile_checked_source('''fn relay(resource: Result<TextFile,String>) -> Result<TextFile,String> {
    return move resource
}
fn main() {
    let opened = openText("metadata.txt", "write")
    let forwarded = relay(move opened)
    match move forwarded {
        Ok(file) => file.close()
        Err(_) => {}
    }
}''', "ownership-metadata.tyv")
        self.assertEqual(checked.resource_types, frozenset({
            "TextFile", "TextReader", "TextWriter", "TcpConnection", "TcpListener", "HttpServer",
        }))
        self.assertTrue(checked.move_sites)
        self.assertTrue(checked.owner_scopes)
        self.assertTrue(checked.cleanup_points)
        self.assertTrue(checked.ownership_transfers)
        with self.assertRaises(TypeError):
            checked.move_sites[next(iter(checked.move_sites))] = "overwritten"

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
    using reader = openTextReader(".tyvera.secrets") {
        reader.readAll()
    } else error {
        println(error)
    }
    using writer = openTextWriter(".tyvera.secrets", "append") {
        writer.append("not allowed")
    } else error {
        println(error)
    }
}''', root)
            self.assertEqual(
                output,
                "secret file can only be accessed through the secrets provider\n" * 3,
            )
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
