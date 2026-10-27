from __future__ import annotations

import io
import os
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tyvera.backend import ReferenceInterpreterBackend
from tyvera.cli import main as cli_main
from tyvera.compiler import compile_checked_project, compile_checked_source
from tyvera.cpp_backend import CppNativeBackend, emit_cpp
from tyvera.ir import (
    IRAppliedType,
    IRCall,
    IRCollectionLength,
    IRBuiltinCall,
    IRForEach,
    IRFunction,
    IRIf,
    IRLet,
    IRLiteral,
    IRMatch,
    IRMethodCall,
    IRMove,
    IRModule,
    IROutput,
    IRType,
    IRUsing,
    IRVariant,
    validate_ir,
)
from tyvera.lowering import lower_checked_program
from tyvera.source import Diagnostic, Location


REPOSITORY = Path(__file__).resolve().parents[1]


def _compiler_available() -> bool:
    configured = os.environ.get("CXX", "").strip()
    if configured:
        try:
            command = shlex.split(configured, posix=os.name != "nt")
        except ValueError:
            return False
        return bool(command) and (Path(command[0]).is_file() or shutil.which(command[0]) is not None)
    return any(shutil.which(command) for command in ("clang++", "g++", "c++", "cl"))


HAS_CPP_COMPILER = _compiler_available()


class NativeLoweringTests(unittest.TestCase):
    def lower(self, source: str):
        return lower_checked_program(compile_checked_source(source, "native_ir.tyv"))

    def test_ast_lowers_to_typed_functions_calls_and_locals(self) -> None:
        module = self.lower('''fn add(left: Int, right: Int) -> Int { return left + right }
fn main() {
    let answer: Int = add(20, 22)
    println(answer)
}
''')
        self.assertEqual(module.entry_symbol, "tyv_fn_1")
        function = module.functions[0]
        self.assertEqual(function.return_type, IRType.INT)
        self.assertEqual([item.type for item in function.parameters], [IRType.INT, IRType.INT])
        self.assertEqual(function.body[0].value.type, IRType.INT)
        self.assertEqual(module.functions[1].body[0].variable.type, IRType.INT)

    def test_if_else_while_and_loop_control_lower(self) -> None:
        module = self.lower('''fn main() {
    var index = 0
    while index < 4 {
        index = index + 1
        if index == 2 { continue }
        if index == 4 { break }
    }
}
''')
        loop = module.functions[0].body[1]
        self.assertEqual(type(loop).__name__, "IRWhile")
        self.assertEqual(type(loop.body[1]).__name__, "IRIf")
        self.assertEqual(loop.body[1].then_body[0].kind, "continue")
        self.assertEqual(loop.body[2].then_body[0].kind, "break")

    def test_shadowed_locals_get_distinct_ir_ids(self) -> None:
        module = self.lower('''fn main() {
    let value = 1
    if true { let value = 2; println(value) }
    println(value)
}
''')
        outer = module.functions[0].body[0].variable
        inner = module.functions[0].body[1].then_body[0].variable
        self.assertNotEqual(outer.identifier, inner.identifier)
        self.assertEqual(module.functions[0].body[2].value.variable_id, outer.identifier)

    def test_signed_64_bit_minimum_has_a_single_typed_ir_literal(self) -> None:
        module = self.lower("fn main() { println(-9223372036854775808) }")
        value = module.functions[0].body[0].value
        self.assertEqual(value.type, IRType.INT)
        self.assertEqual(value.value, -(1 << 63))

    def test_transparent_primitive_aliases_lower_to_their_target_type(self) -> None:
        module = self.lower('''type Count = Int
fn increment(value: Count) -> Count { return value + 1 }
fn main() { println(increment(4)) }
''')
        self.assertEqual(module.functions[0].parameters[0].type, IRType.INT)
        self.assertEqual(module.functions[0].return_type, IRType.INT)

    def test_linked_modules_with_scalar_functions_lower(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-modules-") as temporary:
            root = Path(temporary)
            (root / "math.tyv").write_text(
                "pub fn add(left: Int, right: Int) -> Int { return left + right }\n",
                encoding="utf-8",
            )
            entry = root / "main.tyv"
            entry.write_text(
                'import "math.tyv" as math\nfn main() { println(math.add(2, 3)) }\n',
                encoding="utf-8",
            )
            module = lower_checked_program(compile_checked_project(entry, root))
            self.assertEqual(len(module.functions), 2)
            entry_function = next(function for function in module.functions if function.name == "main")
            call = entry_function.body[0].value
            self.assertIsInstance(call, IRCall)
            self.assertEqual(call.type, IRType.INT)
            self.assertIn(call.function, {function.symbol for function in module.functions})

    def test_unsupported_actor_path_is_a_source_diagnostic(self) -> None:
        checked = compile_checked_source("actor Worker { on ping() {} }\nfn main() {}", "actor.tyv")
        with self.assertRaises(Diagnostic) as caught:
            lower_checked_program(checked)
        self.assertEqual(caught.exception.stage, "backend")
        self.assertIn("does not yet support actors", caught.exception.message)
        self.assertEqual(caught.exception.location.filename, "actor.tyv")

    def test_native_task_cancellation_methods_lower_to_typed_ir(self) -> None:
        module = self.lower('''async fn background() {}
fn main() {
    let task = background()
    println(task.isDone())
    println(task.cancel())
    println(task.isCancelled())
}
''')
        calls = [
            statement.value for statement in module.functions[1].body
            if isinstance(statement, IROutput)
        ]
        self.assertTrue(all(isinstance(call, IRMethodCall) for call in calls))
        self.assertEqual([call.method for call in calls], ["isDone", "cancel", "isCancelled"])
        self.assertTrue(all(call.receiver.type == IRAppliedType("Task", (IRType.VOID,)) for call in calls))
        generated = emit_cpp(module)
        self.assertIn(".is_done()", generated)
        self.assertIn(".cancel()", generated)
        self.assertIn(".is_cancelled()", generated)

    def test_native_duration_and_task_timeout_lower_to_typed_ir(self) -> None:
        module = self.lower('''async fn value() -> Int { return 7 }
fn main() {
    let duration = seconds(2) - milliseconds(500)
    println(duration.inMilliseconds())
    let task = value()
    let timed = task.withTimeout(milliseconds(20))
}
''')
        calls = [
            statement.value for statement in module.functions[1].body
            if isinstance(statement, IROutput)
        ]
        self.assertIsInstance(calls[0], IRMethodCall)
        self.assertEqual(calls[0].receiver.type, IRType.DURATION)
        self.assertEqual(calls[0].method, "inMilliseconds")
        generated = emit_cpp(module)
        self.assertIn("duration_seconds", generated)
        self.assertIn("task_with_timeout", generated)

    def test_native_byte_buffer_and_binary_io_lower_to_typed_ir(self) -> None:
        module = self.lower('''fn main() {
    let buffer = byteBuffer(4)
    let read_result = readBytes("input.bin")
    let write_result = writeBytes("output.bin", bytesFromString("data"))
    let text_result = readText("input.txt")
    let joined = pathJoin("folder", "file.txt")
    let exists = pathExists(joined)
}
''')
        builtins = [statement.value.builtin for statement in module.functions[0].body if isinstance(statement, IRLet)]
        self.assertEqual(builtins, ["byteBuffer", "readBytes", "writeBytes", "readText", "pathJoin", "pathExists"])
        self.assertEqual(module.functions[0].body[0].value.type, IRAppliedType("Result", (IRType.BYTE_BUFFER, IRType.STRING)))

    def test_native_using_lowers_checked_resource_transfer_and_move(self) -> None:
        module = self.lower('''fn relay(opened: Result<TextFile,String>) -> Result<TextFile,String> {
    return move opened
}
fn main() {
    using file = openText("out.txt", "write") {
        file.write("text")
    } else error {
        println(error)
    }
}
''')
        relay_move = module.functions[0].body[0].value
        self.assertIsInstance(relay_move, IRMove)
        using = module.functions[1].body[0]
        self.assertIsInstance(using, IRUsing)
        self.assertEqual(using.resource.type, IRType.TEXT_FILE)
        self.assertEqual(using.acquisition.type, IRAppliedType("Result", (IRType.TEXT_FILE, IRType.STRING)))
        self.assertEqual(using.error_variable.type, IRType.STRING)

    def test_native_await_moves_resource_results_and_tcp_using_has_cleanup_metadata(self) -> None:
        module = self.lower('''async fn reserve() {
    let pending = tcpListen("127.0.0.1", 0, 8)
    let opened = await move pending
    using listener = move opened {
        println(listener.isOpen())
    } else error {
        println(error)
    }
}
fn main() {
    let task = reserve()
    runTasks()
}
''')
        source = emit_cpp(module)
        self.assertIn(".await_move(", source)
        self.assertIn("set_resource_location(tyv_local_", source)
        self.assertIn("cannot close TCP listener", source)

    def test_native_byte_buffer_and_io_signatures_reject_wrong_types(self) -> None:
        invalid_sources = (
            '''fn main() {
    match byteBuffer(1) {
        Err(_) => {}
        Ok(buffer) => buffer.set(0, "not a Byte")
    }
}''',
            'fn main() { writeBytes("out.bin", "not Bytes") }',
            'fn main() { pathJoin(1, "file") }',
            'fn main() { byteBuffer("not a length") }',
        )
        for source in invalid_sources:
            with self.subTest(source=source):
                with self.assertRaises(Diagnostic) as caught:
                    compile_checked_source(source, "native-io-types.tyv")
                self.assertEqual(caught.exception.stage, "type")

    def test_ir_validator_rejects_mistyped_native_byte_buffer_builtin(self) -> None:
        from tyvera.ir import IRExpressionStatement

        location = Location("bad_buffer_ir.tyv", 1, 1)
        call = IRBuiltinCall(
            "byteBuffer", (IRLiteral(1, IRType.INT, location),), IRType.BYTE_BUFFER, location,
        )
        main = IRFunction("main", "tyv_fn_main", (), IRType.VOID,
                          (IRExpressionStatement(call, location),), location)
        with self.assertRaisesRegex(Diagnostic, "invalid typed IR builtin 'byteBuffer'"):
            validate_ir(IRModule((main,), "tyv_fn_main"))

    def test_sum_types_and_recursive_patterns_lower_with_checked_types(self) -> None:
        module = self.lower('''fn main() {
    let option: Option<Int> = Some(3)
    match option {
        Some(value) => println(value)
        None => println(0)
    }
    let result: Result<Option<Int>, String> = Ok<Option<Int>, String>(Some(5))
    match result {
        Ok(Some(value)) => println(value)
        Ok(None) => println(0)
        Err(reason) => println(reason)
    }
}
''')
        function = module.functions[0]
        self.assertIsInstance(function.body[0].value, IRVariant)
        self.assertEqual(function.body[0].variable.type, IRAppliedType("Option", (IRType.INT,)))
        option_match = function.body[1]
        result_match = function.body[3]
        self.assertIsInstance(option_match, IRMatch)
        self.assertIsInstance(result_match, IRMatch)
        self.assertTrue(option_match.exhaustive)
        self.assertTrue(result_match.exhaustive)
        self.assertEqual(option_match.covered_cases, frozenset({"Some", "None"}))

    def test_list_map_and_immutable_bytes_lower_to_typed_ir(self) -> None:
        module = self.lower('''fn main() {
    let values: List<Int> = List<Int>()
    values.append(2)
    println(values.length)
    for value in values { println(value) }
    let entries: Map<String, Int> = Map<String, Int>()
    entries.put("key", 3)
    for key in entries { println(key) }
    let bytes: Bytes = bytesFromString("a")
    println(bytes.length())
}
''')
        body = module.functions[0].body
        self.assertEqual(body[0].variable.type, IRAppliedType("List", (IRType.INT,)))
        self.assertIsInstance(body[1].expression, IRMethodCall)
        self.assertIsInstance(body[2].value, IRCollectionLength)
        self.assertIsInstance(body[3], IRForEach)
        self.assertEqual(body[3].iterable.type.name, "List")
        self.assertIsInstance(body[6], IRForEach)
        self.assertEqual(body[6].iterable.type.name, "Map")
        self.assertEqual(body[7].variable.type, IRType.BYTES)

    def test_ir_validator_rejects_out_of_range_byte_literal(self) -> None:
        location = Location("bad_byte_ir.tyv", 1, 1)
        main = IRFunction(
            "main", "tyv_fn_main", (), IRType.VOID,
            (IROutput("stdout", IRLiteral(256, IRType.BYTE, location), location),), location,
        )
        with self.assertRaisesRegex(Diagnostic, "invalid Byte literal"):
            validate_ir(IRModule((main,), "tyv_fn_main"))

    def test_ir_validator_rejects_untyped_control_flow(self) -> None:
        location = Location("bad_ir.tyv", 1, 1)
        main = IRFunction(
            "main", "tyv_fn_main", (), IRType.VOID,
            (IRIf(IRLiteral(1, IRType.INT, location), (), (), location),), location,
        )
        with self.assertRaisesRegex(Diagnostic, "if condition must be Bool"):
            validate_ir(IRModule((main,), "tyv_fn_main"))

    def test_ir_validator_rejects_out_of_scope_local_reference(self) -> None:
        location = Location("bad_ir.tyv", 1, 1)
        from tyvera.ir import IRExpressionStatement, IRLocalRef

        main = IRFunction(
            "main", "tyv_fn_main", (), IRType.VOID,
            (IRExpressionStatement(IRLocalRef(42, IRType.INT, location), location),), location,
        )
        with self.assertRaisesRegex(Diagnostic, "outside its scope"):
            validate_ir(IRModule((main,), "tyv_fn_main"))

    def test_ir_validator_rejects_nonvoid_fallthrough(self) -> None:
        location = Location("bad_ir.tyv", 1, 1)
        main = IRFunction("main", "tyv_fn_main", (), IRType.VOID, (), location)
        value = IRFunction("value", "tyv_fn_value", (), IRType.INT, (), location)
        with self.assertRaisesRegex(Diagnostic, "fall through without returning Int"):
            validate_ir(IRModule((main, value), "tyv_fn_main"))

    def test_generated_cpp_contains_typed_runtime_and_no_python_bridge(self) -> None:
        module = self.lower('''fn add(a: Int, b: Int) -> Int { return a + b }
fn main() { println(add(1, 2)); println("Tyvera") }
''')
        source = emit_cpp(module)
        self.assertIn("std::int64_t tyv_fn_0", source)
        self.assertIn("checked_add", source)
        self.assertIn("tyvera_runtime::Failure", source)
        self.assertIn("if (!tyvera_runtime::install_shutdown_handlers())", source)
        self.assertIn("ExitCode::initialization_failure", source)
        self.assertNotIn("python", source.lower())
        self.assertNotIn("PyObject", source)

    def test_new_native_examples_lower_and_emit_cpp_without_python_bridge(self) -> None:
        for name in (
            "native_sum_types.tyv", "native_collections.tyv", "native_bytes.tyv", "byte_buffers.tyv",
            "tcp_echo.tyv",
        ):
            with self.subTest(example=name):
                checked = compile_checked_project(REPOSITORY / "examples" / name, REPOSITORY)
                module = lower_checked_program(checked)
                source = emit_cpp(module)
                self.assertIn("std::optional", source)
                self.assertIn("exhaustive match had no matching pattern", source)
                self.assertIn("tyvera_generated", source)
                self.assertNotIn("PyObject", source)
                self.assertNotIn("python", source.lower())
                if name == "native_collections.tyv":
                    self.assertIn(
                        "tyvera_runtime::map_put<std::optional<std::int64_t>, std::string>", source,
                    )
                    self.assertIn(
                        "tyvera_runtime::map_get<std::optional<std::int64_t>, std::string>", source,
                    )
                    self.assertIn(
                        "tyvera_runtime::list_append<std::optional<std::int64_t>>", source,
                    )
                    self.assertIn(
                        "tyvera_runtime::map_put<std::string, std::optional<std::int64_t>>", source,
                    )
                if name == "tcp_echo.tyv":
                    self.assertIn("struct TaskState", source)
                    self.assertIn("::getaddrinfo", source)
                    self.assertIn("tyvera_runtime::tcp_connect", source)
                    self.assertIn("tyvera_runtime::tcp_listen", source)
                    self.assertIn("tyvera_runtime::tcp_accept", source)
                    self.assertIn("tyvera_runtime::tcp_read", source)
                    self.assertIn("tyvera_runtime::tcp_write_all", source)

    def test_build_cli_reports_unsupported_native_construct_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-cli-build-") as temporary:
            root = Path(temporary)
            source = root / "unsupported.tyv"
            source.write_text("actor Worker { on ping() {} }\nfn main() {}", encoding="utf-8")
            output = root / "unsupported"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = cli_main(["build", str(source), "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertIn("native backend does not yet support actors", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_build_cli_typechecks_before_creating_any_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-cli-static-error-") as temporary:
            root = Path(temporary)
            source = root / "invalid.tyv"
            source.write_text('fn main() { println(1 + true) }', encoding="utf-8")
            output = root / "invalid"
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                result = cli_main(["build", str(source), "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertIn("type error", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_build_cli_reports_missing_compiler_without_creating_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-cli-no-cxx-") as temporary:
            root = Path(temporary)
            source = root / "hello.tyv"
            source.write_text("fn main() { println(1) }", encoding="utf-8")
            output = root / "hello"
            stderr = io.StringIO()
            with patch.dict(os.environ, {"CXX": "tyvera-cxx-that-does-not-exist"}):
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    result = cli_main(["build", str(source), "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertIn("C++ compiler from CXX was not found", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_missing_cxx_compiler_has_a_controlled_diagnostic(self) -> None:
        module = self.lower("fn main() { println(1) }")
        with tempfile.TemporaryDirectory(prefix="tyvera-no-cxx-") as temporary:
            with patch.dict(os.environ, {"CXX": "tyvera-cxx-that-does-not-exist"}):
                with self.assertRaises(Diagnostic) as caught:
                    CppNativeBackend().build(module, Path(temporary) / "app")
            self.assertEqual(caught.exception.stage, "backend")
            self.assertIn("was not found", caught.exception.message)


@unittest.skipUnless(HAS_CPP_COMPILER, "native end-to-end checks require a C++17 compiler")
class NativeExecutionTests(unittest.TestCase):
    def _build_and_run(
        self, source_file: Path, output: Path, *, project_root: Path = REPOSITORY,
        cwd: Path = REPOSITORY,
    ) -> subprocess.CompletedProcess[str]:
        checked = compile_checked_project(source_file, project_root)
        module = lower_checked_program(checked)
        CppNativeBackend().build(module, output, project_root=project_root)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        if os.name != "nt":
            # The absolute executable must not depend on Python being on PATH.
            environment["PATH"] = ""
        return subprocess.run(
            [str(output)], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=environment, check=False,
        )

    @staticmethod
    def _reference_result(source: str, project_root: Path) -> tuple[int, str, str]:
        checked = compile_checked_source(source, "native-io.tyv")
        output = io.StringIO()
        errors = io.StringIO()
        try:
            ReferenceInterpreterBackend().execute(
                checked, output=output, error_output=errors, project_root=project_root,
            )
        except Diagnostic as error:
            return 1, output.getvalue(), error.render() + "\n"
        return 0, output.getvalue(), errors.getvalue()

    def _native_source_result(
        self, source: str, project_root: Path, output: Path, cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        checked = compile_checked_source(source, "native-io.tyv")
        CppNativeBackend().build(lower_checked_program(checked), output, project_root=project_root)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        if os.name != "nt":
            environment["PATH"] = ""
        return subprocess.run(
            [str(output)], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", env=environment, check=False,
        )

    @staticmethod
    def _tyvera_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def test_native_duration_conversions_match_reference(self) -> None:
        source = '''fn main() {
    println(milliseconds(-2).inMilliseconds())
    println((seconds(2) - milliseconds(500)).inMilliseconds())
    match durationFromSeconds(0.0001) {
        Ok(value) => println(value.inMilliseconds())
        Err(error) => println(error)
    }
    match durationFromSeconds(-1.0) {
        Ok(_) => println("unexpected duration")
        Err(error) => println(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-duration-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "duration-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(
                native.stdout,
                "-2\\n1500\\n1\\nDuration seconds must be finite and non-negative\\n",
            )

    def test_native_task_timeout_cancels_source_and_matches_reference(self) -> None:
        source = '''async fn background() {
    while true {}
}
fn main() {
    let source = background()
    let timed = source.withTimeout(milliseconds(20))
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("timed out")
    }
    println(source.isCancelled())
    println(timed.isDone())
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-timeout-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "timeout-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "timed out\\ntrue\\ntrue\\n")

    def test_native_negative_timeout_prevents_unstarted_source_body(self) -> None:
        source = '''async fn sideEffect() {
    println("source started")
}
fn main() {
    let source = sideEffect()
    let timed = source.withTimeout(milliseconds(-1))
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("task timed out")
    }
    println(source.isCancelled())
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-negative-timeout-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "negative-timeout-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "task timed out\\ntrue\\n")

    def test_native_completed_task_wins_over_an_expired_timeout(self) -> None:
        source = '''async fn immediate() -> Int { return 7 }
async fn run() {
    let original = immediate()
    let first = await original
    let timed = original.withTimeout(milliseconds(-1))
    let second = await timed
    println(first + second)
}
fn main() {
    let task = run()
    match runTasks() {
        Ok(_) => println("ok")
        Err(_) => println("failed")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-timeout-complete-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "timeout-complete", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "14\\nok\\n")

    def test_native_cancelling_timeout_wrapper_cancels_source(self) -> None:
        source = '''async fn background() {
    while true {}
}
fn main() {
    let source = background()
    let timed = source.withTimeout(seconds(10))
    println(timed.cancel())
    println(source.isCancelled())
    match runTasks() {
        Ok(_) => println("cancelled")
        Err(_) => println("failed")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-timeout-cancel-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "timeout-cancel", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\\ntrue\\ncancelled\\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking timeout tests require Linux")
    def test_native_timeout_cancels_pending_accept_and_matches_reference(self) -> None:
        source = '''async fn waitForPeer() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    using listener = move opened {
        let pending = listener.accept()
        let timed = pending.withTimeout(milliseconds(30))
        let accepted = await move timed
        match move accepted {
            Ok(connection) => connection.close()
            Err(error) => println(error)
        }
    } else error {
        println(error)
    }
}
fn main() {
    let task = waitForPeer()
    match runTasks() {
        Ok(_) => println("unexpected success")
        Err(_) => println("timed out")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-accept-timeout-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "accept-timeout", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "timed out\\n")

    def test_native_owned_child_task_cancellation_matches_reference(self) -> None:
        source = '''async fn background() {
    while true {}
}
async fn parent() {
    let child = background()
    println(child.cancel())
    println(child.isCancelled())
    println(child.isDone())
}
fn main() {
    let task = parent()
    match runTasks() {
        Ok(_) => println("ok")
        Err(error) => println(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-child-cancel-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "child-cancel", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\ntrue\ntrue\nok\n")

    def test_native_byte_buffer_binary_and_text_io_match_reference(self) -> None:
        source = '''
fn mutate(buffer: ByteBuffer, value: Byte) {
    let alias = buffer
    match alias.set(1, value) {
        Ok(_) => println("mutated")
        Err(error) => eprintln(error)
    }
}
fn relay(opened: Result<TextFile,String>) -> Result<TextFile,String> {
    return move opened
}
fn main() {
    match byteFromInt(255) {
        Err(error) => eprintln(error)
        Ok(value) => match byteBuffer(3) {
            Err(error) => eprintln(error)
            Ok(buffer) => {
                let alias = buffer
                mutate(alias, value)
                match buffer.get(1) {
                    Err(error) => eprintln(error)
                    Ok(changed) => println(changed.toInt())
                }
                match buffer.toBytes() {
                    Err(error) => eprintln(error)
                    Ok(snapshot) => {
                        match writeBytes("payload.bin", snapshot) {
                            Err(error) => eprintln(error)
                            Ok(_) => match readBytes("payload.bin") {
                                Err(error) => eprintln(error)
                                Ok(contents) => println(contents.length())
                            }
                        }
                    }
                }
            }
        }
    }
    match readBytes("input.bin") {
        Err(error) => eprintln(error)
        Ok(contents) => {
            println(contents.length())
            match writeBytes(pathJoin("nested", "copy.bin"), contents) {
                Err(error) => eprintln(error)
                Ok(_) => println(pathExists("nested/copy.bin"))
            }
        }
    }
    match readText("seed.txt") {
        Err(error) => eprintln(error)
        Ok(contents) => println(contents)
    }
    match writeText("written.txt", "written") {
        Err(error) => eprintln(error)
        Ok(_) => match readText("written.txt") {
            Err(error) => eprintln(error)
            Ok(contents) => println(contents)
        }
    }
    let transferred = relay(openText("resource.txt", "write"))
    match move transferred {
        Err(error) => eprintln(error)
        Ok(file) => {
            match file.write("resource") {
                Err(error) => eprintln(error)
                Ok(_) => match file.readAll() {
                    Err(error) => println(error)
                    Ok(_) => println("unexpected")
                }
            }
            println(file.close())
            println(file.isOpen())
        }
    }
    using writer = openTextWriter("scoped.txt", "write") {
        match writer.write("scoped") {
            Err(error) => eprintln(error)
            Ok(_) => match writer.flush() {
                Err(error) => eprintln(error)
                Ok(_) => println("flushed")
            }
        }
    } else error {
        eprintln(error)
    }
    using reader = openTextReader("scoped.txt") {
        match reader.readAll() {
            Err(error) => eprintln(error)
            Ok(contents) => println(contents)
        }
    } else error {
        eprintln(error)
    }
    using appender = openTextWriter("scoped.txt", "append") {
        match appender.append("-more") {
            Err(error) => eprintln(error)
            Ok(_) => println("appended")
        }
    } else error {
        eprintln(error)
    }
    match readText("scoped.txt") {
        Err(error) => eprintln(error)
        Ok(contents) => println(contents)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-io-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                (root / "nested").mkdir(parents=True)
                (root / "input.bin").write_bytes(b"\x00\xff\x2a")
                (root / "seed.txt").write_bytes(b"first\r\nsecond\r\n")
            expected = self._reference_result(source, roots[0])
            native = self._native_source_result(source, roots[1], workspace / "native-app", workspace)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual((roots[0] / "payload.bin").read_bytes(), (roots[1] / "payload.bin").read_bytes())
            self.assertEqual((roots[1] / "payload.bin").read_bytes(), b"\x00\xff\x00")
            self.assertEqual((roots[0] / "nested" / "copy.bin").read_bytes(), (roots[1] / "nested" / "copy.bin").read_bytes())
            for name in ("written.txt", "resource.txt", "scoped.txt"):
                self.assertEqual((roots[0] / name).read_bytes(), (roots[1] / name).read_bytes())
            self.assertEqual((roots[1] / "scoped.txt").read_text(encoding="utf-8"), "scoped-more")

    def test_native_io_errors_buffer_bounds_and_secret_paths_match_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-io-errors-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                root.mkdir()
                (root / "bad-utf8.txt").write_bytes(b"\xff")
                (root / ".tyvera.secrets").write_text("TOKEN = \"must-not-appear\"\n", encoding="utf-8")
                (root / "directory").mkdir()
                (root / "nested").mkdir()
            error_source = '''fn main() {
    match byteBuffer(-1) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match byteBuffer(67108865) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match byteFromInt(256) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match byteBuffer(1) {
        Err(error) => println(error)
        Ok(buffer) => {
            match byteFromInt(0) {
                Err(error) => println(error)
                Ok(zero) => {
                    match buffer.get(-1) {
                        Err(error) => println(error)
                        Ok(_) => println("unexpected")
                    }
                    match buffer.get(1) {
                        Err(error) => println(error)
                        Ok(_) => println("unexpected")
                    }
                    match buffer.set(1, zero) {
                        Err(error) => println(error)
                        Ok(_) => println("unexpected")
                    }
                }
            }
        }
    }
    match readBytes("missing.bin") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    using reader = openTextReader("missing.txt") {
        println("unexpected")
    } else error {
        println(error)
    }
    using writer = openTextWriter("missing-parent/out.txt", "write") {
        println("unexpected")
    } else error {
        println(error)
    }
    using invalidMode = openTextWriter("ignored.txt", "read") {
        println("unexpected")
    } else error {
        println(error)
    }
    match readText("bad-utf8.txt") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match readText("directory") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match writeBytes("directory", bytesFromString("x")) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match readText(".tyvera.secrets") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match readText("./nested/../.tyvera.secrets") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match readText(@ABS_SECRET@) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match writeText(".tyvera.secrets", "overwritten") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
    match writeBytes(".tyvera.secrets", bytesFromString("overwritten")) {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
}'''
            source_for = lambda root: error_source.replace(
                "@ABS_SECRET@", self._tyvera_string(str(root / ".tyvera.secrets")),
            )
            sources = (source_for(roots[0]), source_for(roots[1]))
            expected = self._reference_result(sources[0], roots[0])
            native = self._native_source_result(sources[1], roots[1], workspace / "native-errors", workspace)
            self.maxDiff = None
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertNotIn("must-not-appear", native.stdout + native.stderr)
            for root in roots:
                self.assertEqual((root / ".tyvera.secrets").read_text(encoding="utf-8"), 'TOKEN = "must-not-appear"\n')

            if os.name != "nt":
                for root in roots:
                    try:
                        (root / "secret-link").symlink_to(root / ".tyvera.secrets")
                    except OSError:
                        self.skipTest("test environment does not permit creating symlinks")
                symlink_source = '''fn main() {
    match readText("secret-link") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
}'''
                expected_link = self._reference_result(symlink_source, roots[0])
                native_link = self._native_source_result(
                    symlink_source, roots[1], workspace / "native-link", workspace,
                )
                self.assertEqual((native_link.returncode, native_link.stdout, native_link.stderr), expected_link)

    def test_native_byte_buffer_accepts_64_mib_and_rejects_the_next_byte(self) -> None:
        source = '''fn main() {
    match byteBuffer(67108864) {
        Err(error) => println(error)
        Ok(buffer) => println(buffer.length())
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-buffer-limit-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                root.mkdir()
            expected = self._reference_result(source, roots[0])
            native = self._native_source_result(source, roots[1], workspace / "native-limit", workspace)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)

    def test_native_binary_read_rejects_file_over_64_mib_like_reference(self) -> None:
        source = '''fn main() {
    match readBytes("oversized.bin") {
        Err(error) => println(error)
        Ok(_) => println("unexpected")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-binary-limit-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                root.mkdir()
                with (root / "oversized.bin").open("wb") as stream:
                    stream.truncate(67_108_865)
            expected = self._reference_result(source, roots[0])
            native = self._native_source_result(source, roots[1], workspace / "native-binary-limit", workspace)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)

    def test_native_permission_errors_match_reference_when_enforced(self) -> None:
        if os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0:
            self.skipTest("permission-denied fixture requires a non-root POSIX user")
        source = '''fn main() {
    match readBytes("restricted/private.bin") {
        Err(error) => println(error)
        Ok(_) => println("unexpected read")
    }
    match writeBytes("restricted/out.bin", bytesFromString("x")) {
        Err(error) => println(error)
        Ok(_) => println("unexpected write")
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-permissions-") as temporary:
            workspace = Path(temporary)
            roots = (workspace / "reference", workspace / "native")
            for root in roots:
                restricted = root / "restricted"
                restricted.mkdir(parents=True)
                (restricted / "private.bin").write_bytes(b"private")
                restricted.chmod(0)
            try:
                expected = self._reference_result(source, roots[0])
                native = self._native_source_result(source, roots[1], workspace / "native-permissions", workspace)
                self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            finally:
                for root in roots:
                    (root / "restricted").chmod(0o700)

    def test_native_cleanup_failure_does_not_replace_primary_failure(self) -> None:
        if not Path("/dev/full").exists():
            self.skipTest("cleanup failure fixture requires /dev/full")
        source = '''fn main() {
    using writer = openTextWriter("/dev/full", "write") {
        writer.write("buffered")
        let invalid = 1 / 0
    } else error {
        eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-cleanup-") as temporary:
            workspace = Path(temporary)
            root = workspace / "project"
            root.mkdir()
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, workspace / "native-cleanup", workspace)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)

    def test_native_examples_match_reference_interpreter_without_python_path(self) -> None:
        examples = (
            "native_hello.tyv", "native_control_flow.tyv", "native_values.tyv",
            "native_sum_types.tyv", "native_collections.tyv", "native_bytes.tyv",
            "byte_buffers.tyv",
        )
        if sys.platform.startswith("linux"):
            examples += ("tcp_echo.tyv",)
        with tempfile.TemporaryDirectory(prefix="tyvera-native-e2e-") as temporary:
            for name in examples:
                with self.subTest(example=name):
                    source = REPOSITORY / "examples" / name
                    checked = compile_checked_project(source, REPOSITORY)
                    expected = io.StringIO()
                    ReferenceInterpreterBackend().execute(
                        checked, output=expected, error_output=io.StringIO(), project_root=REPOSITORY,
                    )
                    result = self._build_and_run(source, Path(temporary) / Path(name).stem)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected.getvalue())
                    self.assertEqual(result.stderr, "")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_dns_success_and_failure_match_reference(self) -> None:
        source = '''async fn lookups() {
    let local = await resolveHost("localhost", 80)
    match local {
        Ok(addresses) => println(addresses.length > 0)
        Err(error) => println(error)
    }
    let invalid = await resolveHost("not a valid host name", 80)
    match invalid {
        Ok(_) => println("unexpected DNS success")
        Err(error) => println(error)
    }
}
fn main() {
    let task = lookups()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-dns-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "dns-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\nDNS resolution failed\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_task_cancellation_stops_pending_accept_and_matches_reference(self) -> None:
        source = '''async fn cancelPendingAccept() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(error) => println(error)
        Ok(listener) => {
            let pending = listener.accept()
            println(pending.cancel())
            println(pending.isCancelled())
            println(pending.isDone())
            listener.close()
        }
    }
}
fn main() {
    let task = cancelPendingAccept()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-task-cancel-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "task-cancel", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "true\ntrue\ntrue\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_dns_and_tcp_argument_errors_match_reference(self) -> None:
        source = '''async fn invalidCalls() {
    let emptyHost = await resolveHost("", 80)
    match emptyHost {
        Ok(_) => println("unexpected")
        Err(error) => println(error)
    }
    let invalidPort = await resolveHost("localhost", -1)
    match invalidPort {
        Ok(_) => println("unexpected")
        Err(error) => println(error)
    }
    let invalidConnect = await tcpConnect("127.0.0.1", 0)
    match move invalidConnect {
        Ok(connection) => connection.close()
        Err(error) => println(error)
    }
    let invalidBacklog = await tcpListen("127.0.0.1", 0, 0)
    match move invalidBacklog {
        Ok(listener) => listener.close()
        Err(error) => println(error)
    }
}
fn main() {
    let task = invalidCalls()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-network-errors-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "network-errors", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(
                native.stdout,
                "invalid address\nport must be between 0 and 65535\n"
                "port must be between 1 and 65535\nbacklog must be between 1 and 4096\n",
            )

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_connection_refusal_matches_reference(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''async fn connect() {{
    let result = await tcpConnect("127.0.0.1", {port})
    match move result {{
        Ok(connection) => {{
            connection.close()
            println("unexpected connection")
        }}
        Err(error) => println(error)
    }}
}}
fn main() {{
    let task = connect()
    match runTasks() {{
        Ok(_) => {{}}
        Err(error) => eprintln(error)
    }}
}}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-tcp-error-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "tcp-error-app", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "connection refused\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_concurrent_tcp_connections_match_reference(self) -> None:
        source = '''async fn client(port: Int, payload: String) -> String {
    let opened = await tcpConnect("127.0.0.1", port)
    match move opened {
        Err(error) => return error
        Ok(connection) => {
            let sent = await connection.writeAll(bytesFromString(payload))
            match sent {
                Err(error) => { connection.close(); return error }
                Ok(_) => {}
            }
            let received = await connection.read(16)
            match received {
                Err(error) => { connection.close(); return error }
                Ok(value) => match value {
                    None => { connection.close(); return "unexpected EOF" }
                    Some(bytes) => match bytes.toString() {
                        Err(error) => { connection.close(); return error }
                        Ok(text) => { connection.close(); return text }
                    }
                }
            }
        }
    }
}
async fn echo(opened: Result<TcpConnection,String>) {
    match move opened {
        Err(error) => println(error)
        Ok(connection) => {
            let received = await connection.read(16)
            match received {
                Err(error) => println(error)
                Ok(value) => match value {
                    None => println("unexpected EOF")
                    Some(bytes) => {
                        let sent = await connection.writeAll(bytes)
                        match sent {
                            Ok(_) => {}
                            Err(error) => println(error)
                        }
                    }
                }
            }
            connection.close()
        }
    }
}
async fn serve() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(error) => println(error)
        Ok(listener) => match listener.localAddress() {
            Err(error) => { println(error); listener.close() }
            Ok(address) => {
                let first = client(address.port(), "first")
                let second = client(address.port(), "second")
                let acceptedFirst = await listener.accept()
                let serviceFirst = echo(move acceptedFirst)
                let acceptedSecond = await listener.accept()
                let serviceSecond = echo(move acceptedSecond)
                await serviceFirst
                await serviceSecond
                let firstResponse = await first
                let secondResponse = await second
                println(firstResponse)
                println(secondResponse)
                listener.close()
            }
        }
    }
}
fn main() {
    let task = serve()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-multi-tcp-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "multi-tcp", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "first\nsecond\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native IPv6 networking tests require Linux")
    def test_native_ipv6_tcp_echo_and_eof_match_reference(self) -> None:
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(("::1", 0))
        except OSError as error:
            self.skipTest(f"IPv6 loopback is unavailable: {error}")
        source = '''async fn client(port: Int) -> String {
    let opened = await tcpConnect("::1", port)
    using connection = move opened {
        let sent = await connection.writeAll(bytesFromString("x"))
        match sent {
            Err(error) => return error
            Ok(_) => {}
        }
        let echoed = await connection.read(1)
        match echoed {
            Err(error) => return error
            Ok(value) => match value {
                None => return "unexpected EOF"
                Some(bytes) => match bytes.toString() {
                    Err(error) => return error
                    Ok(text) => {
                        let ended = await connection.read(1)
                        match ended {
                            Err(error) => return error
                            Ok(result) => match result {
                                None => return text + "|EOF"
                                Some(_) => return "unexpected extra data"
                            }
                        }
                    }
                }
            }
        }
        return "incomplete client exchange"
    } else error {
        return "connect: " + error
    }
}
async fn serve() {
    let opened = await tcpListen("::1", 0, 8)
    using listener = move opened {
        match listener.localAddress() {
            Err(error) => println(error)
            Ok(address) => {
                let clientTask = client(address.port())
                let accepted = await listener.accept()
                using connection = move accepted {
                    let received = await connection.read(1)
                    match received {
                        Err(error) => println(error)
                        Ok(value) => match value {
                            None => println("unexpected server EOF")
                            Some(bytes) => {
                                let echoed = await connection.writeAll(bytes)
                                match echoed {
                                    Ok(_) => {}
                                    Err(error) => println(error)
                                }
                            }
                        }
                    }
                } else error {
                    println(error)
                }
                let response = await clientTask
                println(address.family())
                println(response)
            }
        }
    } else error {
        println(error)
    }
}
fn main() {
    let server = serve()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-ipv6-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "ipv6-echo", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(native.stdout, "IPv6\nx|EOF\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking tests require Linux")
    def test_native_tcp_resource_cleanup_on_scope_and_error_exit_matches_reference(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''async fn reserve(port: Int, fail: Bool) {{
    let pending = tcpListen("127.0.0.1", port, 8)
    let opened = await move pending
    using listener = move opened {{
        println(listener.isOpen())
        if fail {{
            let invalid = 1 / 0
        }}
    }} else error {{
        println(error)
    }}
}}
fn main() {{
    let normal = reserve({port}, false)
    match runTasks() {{
        Ok(_) => println("normal")
        Err(_) => println("normal failed")
    }}
    let failing = reserve({port}, true)
    match runTasks() {{
        Ok(_) => println("unexpected success")
        Err(_) => println("failure unwound")
    }}
    let reopened = reserve({port}, false)
    match runTasks() {{
        Ok(_) => println("reopened")
        Err(_) => println("reopen failed")
    }}
}}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-tcp-cleanup-") as temporary:
            root = Path(temporary)
            expected = self._reference_result(source, root)
            native = self._native_source_result(source, root, root / "tcp-cleanup", root)
            self.assertEqual((native.returncode, native.stdout, native.stderr), expected)
            self.assertEqual(
                native.stdout,
                "true\nnormal\ntrue\nfailure unwound\ntrue\nreopened\n",
            )

    @unittest.skipUnless(sys.platform.startswith("linux"), "native networking signal tests require Linux")
    def test_native_sigterm_interrupts_pending_accept_and_unwinds_tcp_listener(self) -> None:
        source = '''async fn waitForPeer() {
    let pending = tcpListen("127.0.0.1", 0, 8)
    let opened = await move pending
    using listener = move opened {
        writeText("accept-ready", "ready")
        let accepted = await listener.accept()
        match move accepted {
            Ok(connection) => connection.close()
            Err(_) => println("accept ended")
        }
    } else error {
        println(error)
    }
}
fn main() {
    let task = waitForPeer()
    runTasks()
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-pending-accept-") as temporary:
            root = Path(temporary)
            executable = root / "pending-accept"
            checked = compile_checked_source(source, "native-pending-accept.tyv")
            CppNativeBackend().build(
                lower_checked_program(checked), executable, project_root=root,
            )
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            environment["PATH"] = ""
            process = subprocess.Popen(
                [str(executable)], cwd=root, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            try:
                deadline = time.monotonic() + 5
                marker = root / "accept-ready"
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "native listener did not reach its pending accept")
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual(process.returncode, 143, error)
            self.assertIn("runtime shutdown requested by SIGTERM", error)

    @unittest.skipUnless(sys.platform.startswith("linux"), "native signal integration requires Linux")
    def test_native_sigint_and_sigterm_unwind_resources_with_stable_exit_codes(self) -> None:
        source = '''fn main() {
    using writer = openTextWriter("signal.txt", "write") {
        writer.write("closed on signal")
        while true {}
    }
}'''
        with tempfile.TemporaryDirectory(prefix="tyvera-native-signals-") as temporary:
            root = Path(temporary)
            executable = root / "signal-app"
            checked = compile_checked_source(source, "native-signal.tyv")
            CppNativeBackend().build(
                lower_checked_program(checked), executable, project_root=root,
            )
            for selected_signal, expected_code, expected_name in (
                (signal.SIGINT, 130, "SIGINT"),
                (signal.SIGTERM, 143, "SIGTERM"),
            ):
                with self.subTest(signal=expected_name):
                    output_file = root / "signal.txt"
                    output_file.unlink(missing_ok=True)
                    process = subprocess.Popen(
                        [str(executable)], cwd=root, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    )
                    try:
                        time.sleep(0.05)
                        self.assertIsNone(process.poll(), "the test program should still be in its loop")
                        process.send_signal(selected_signal)
                        _, error = process.communicate(timeout=5)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                    self.assertEqual(process.returncode, expected_code, error)
                    self.assertIn(f"runtime shutdown requested by {expected_name}", error)
                    self.assertEqual(output_file.read_text(encoding="utf-8"), "closed on signal")

    def test_native_sync_resource_examples_match_reference_interpreter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-native-resources-") as temporary:
            root = Path(temporary)
            for name in ("resource_scopes.tyv", "resource_ownership.tyv"):
                with self.subTest(example=name):
                    source = REPOSITORY / "examples" / name
                    project_root = source.parent
                    checked = compile_checked_project(source, project_root)
                    expected = io.StringIO()
                    ReferenceInterpreterBackend().execute(
                        checked, output=expected, error_output=io.StringIO(), project_root=project_root,
                    )
                    result = self._build_and_run(
                        source, root / Path(name).stem, project_root=project_root, cwd=root,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected.getvalue())
                    self.assertEqual(result.stderr, "")

    def test_cli_build_produces_executable_and_keeps_python_out_of_runtime(self) -> None:
        source = REPOSITORY / "examples" / "native_hello.tyv"
        with tempfile.TemporaryDirectory(prefix="tyvera-cli-native-") as temporary:
            output = Path(temporary) / ("hello.exe" if os.name == "nt" else "hello")
            result = subprocess.run(
                [sys.executable, "-m", "tyvera", "build", str(source), "--output", str(output)],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())
            run_environment = os.environ.copy()
            run_environment.pop("PYTHONPATH", None)
            run_environment.pop("PYTHONHOME", None)
            if os.name != "nt":
                run_environment["PATH"] = ""
            program = subprocess.run([str(output)], capture_output=True, text=True, env=run_environment, check=False)
            self.assertEqual(program.returncode, 0, program.stderr)
            self.assertEqual(program.stdout, "Hello from native Tyvera!\n")

    def test_runtime_overflow_is_a_located_tyvera_failure(self) -> None:
        source = compile_checked_source(
            "fn main() { println(9223372036854775807 + 1) }", "overflow.tyv",
        )
        module = lower_checked_program(source)
        with tempfile.TemporaryDirectory(prefix="tyvera-native-overflow-") as temporary:
            executable = Path(temporary) / "overflow"
            CppNativeBackend().build(module, executable)
            result = subprocess.run([str(executable)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("overflow.tyv:1:", result.stderr)
        self.assertIn("Int overflow: result is outside the signed 64-bit range", result.stderr)

    def test_division_by_zero_is_a_controlled_native_runtime_failure(self) -> None:
        checked = compile_checked_source("fn main() { println(4 / 0) }", "divide.tyv")
        module = lower_checked_program(checked)
        with tempfile.TemporaryDirectory(prefix="tyvera-native-divide-") as temporary:
            executable = Path(temporary) / "divide"
            CppNativeBackend().build(module, executable)
            result = subprocess.run([str(executable)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("divide.tyv:1:", result.stderr)
        self.assertIn("division by zero", result.stderr)

    def test_list_bounds_failure_matches_reference_diagnostic(self) -> None:
        for operation in ("values.insert(-1, 8)", "values.set(1, 8)", "values.set(4, 8)"):
            with self.subTest(operation=operation):
                source_text = f'''fn main() {{
    let values: List<Int> = List<Int>()
    values.append(1)
    {operation}
}}
'''
                checked = compile_checked_source(source_text, "list_bounds.tyv")
                with self.assertRaises(Diagnostic) as caught:
                    ReferenceInterpreterBackend().execute(
                        checked, output=io.StringIO(), error_output=io.StringIO(), project_root=REPOSITORY,
                    )
                diagnostic = caught.exception.render()
                with tempfile.TemporaryDirectory(prefix="tyvera-native-list-bounds-") as temporary:
                    executable = Path(temporary) / "list_bounds"
                    CppNativeBackend().build(lower_checked_program(checked), executable)
                    result = subprocess.run(
                        [str(executable)], capture_output=True, text=True, encoding="utf-8", check=False,
                    )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr.strip(), diagnostic)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()

