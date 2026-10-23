from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


ROOT = Path(__file__).resolve().parents[1]


class InterpreterTests(unittest.TestCase):
    def test_calls_arithmetic_booleans_and_println(self) -> None:
        source = '''fn add(left: Int, right: Int) -> Int {
    return left + right
}
fn main() {
    let answer = add(2, 3)
    println(answer)
    println(1 / 2)
    println(true and !false)
    println("Ty" + "vera")
}
'''
        program, _ = compile_source(source, "runtime.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        self.assertEqual(output.getvalue(), "5\n0.5\ntrue\nTyvera\n")

    def test_division_by_zero_has_runtime_location(self) -> None:
        program, _ = compile_source("fn main() { println(4 / 0) }", "zero.tyv")
        with self.assertRaises(Diagnostic) as caught:
            Interpreter(program, io.StringIO()).run()
        self.assertEqual(caught.exception.stage, "runtime")
        self.assertEqual(caught.exception.location.filename, "zero.tyv")
        self.assertEqual(caught.exception.location.line, 1)

    def test_signed_integer_boundary_and_checked_overflow(self) -> None:
        minimum, _ = compile_source("fn main() { println(-9223372036854775808) }")
        output = io.StringIO()
        Interpreter(minimum, output).run()
        self.assertEqual(output.getvalue(), "-9223372036854775808\n")

        overflow, _ = compile_source("fn main() { println(9223372036854775807 + 1) }", "overflow.tyv")
        with self.assertRaisesRegex(Diagnostic, "Int overflow"):
            Interpreter(overflow, io.StringIO()).run()

    def test_short_circuit_avoids_right_operand_call(self) -> None:
        source = '''fn explode() -> Bool {
    return 1 / 0 == 0
}
fn main() {
    println(false and explode())
}
'''
        program, _ = compile_source(source)
        output = io.StringIO()
        Interpreter(program, output).run()
        self.assertEqual(output.getvalue(), "false\n")


class CliTests(unittest.TestCase):
    def test_python_module_runs_real_tyvera_example(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "tyvera", "run", "hello.tyv"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Hello from Tyvera!\n")
        self.assertEqual(result.stderr, "")

    def test_cli_reports_filename_line_and_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_file = Path(directory) / "bad.tyv"
            source_file.write_text('fn main() {\n    let value: Int = "wrong"\n}\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "tyvera", "check", str(source_file)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("bad.tyv:2:", result.stderr)
        self.assertIn("type error: expected Int, found String", result.stderr)

    def test_cli_handles_missing_file_without_traceback(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "tyvera", "run", "no-such-file.tyv"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("io error: cannot read source file", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
