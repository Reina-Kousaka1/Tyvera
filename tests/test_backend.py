from __future__ import annotations

import io
import unittest
from pathlib import Path

from tyvera.backend import ExecutionBackend, ReferenceInterpreterBackend
from tyvera.compiler import compile_checked_source


class BackendBoundaryTests(unittest.TestCase):
    def test_reference_backend_executes_only_the_compiler_result(self) -> None:
        checked = compile_checked_source(
            'fn main() { println("reference") }', "backend.tyv",
        )
        backend: ExecutionBackend = ReferenceInterpreterBackend()
        output = io.StringIO()
        errors = io.StringIO()

        backend.execute(
            checked,
            output=output,
            error_output=errors,
            project_root=Path.cwd(),
        )

        self.assertEqual(backend.name, "reference-interpreter")
        self.assertEqual(output.getvalue(), "reference\n")
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
