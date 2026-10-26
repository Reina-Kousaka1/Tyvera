from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tyvera.backend import ReferenceInterpreterBackend
from tyvera.cli import main as cli_main
from tyvera.compiler import compile_checked_project, compile_checked_source
from tyvera.cpp_backend import CppNativeBackend, emit_cpp
from tyvera.ir import IRCall, IRFunction, IRIf, IRLiteral, IRModule, IRType, validate_ir
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

    def test_byte_binary_io_and_result_paths_are_explicitly_interpreter_only(self) -> None:
        for source, message in [
            ('fn main() { let byte = byteFromInt(1) }', "type 'Result<Byte,String>'"),
            ('fn main() { let bytes = bytesFromString("x") }', "type 'Bytes'"),
            ('fn main() { let result = readBytes("file.bin") }', "type 'Result<Bytes,String>'"),
        ]:
            with self.subTest(source=source):
                checked = compile_checked_source(source, "runtime_only.tyv")
                with self.assertRaises(Diagnostic) as caught:
                    lower_checked_program(checked)
                self.assertEqual(caught.exception.stage, "backend")
                self.assertIn(message, caught.exception.message)

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
        self.assertNotIn("python", source.lower())
        self.assertNotIn("PyObject", source)

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
    def _build_and_run(self, source_file: Path, output: Path) -> subprocess.CompletedProcess[str]:
        checked = compile_checked_project(source_file, REPOSITORY)
        module = lower_checked_program(checked)
        CppNativeBackend().build(module, output)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        if os.name != "nt":
            # The absolute executable must not depend on Python being on PATH.
            environment["PATH"] = ""
        return subprocess.run([str(output)], capture_output=True, text=True, env=environment, check=False)

    def test_native_examples_match_reference_interpreter_without_python_path(self) -> None:
        examples = ("native_hello.tyv", "native_control_flow.tyv", "native_values.tyv")
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


if __name__ == "__main__":
    unittest.main()
