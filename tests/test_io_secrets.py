from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


ROOT = Path(__file__).resolve().parents[1]


class IOAndSecretsTests(unittest.TestCase):
    def _run(self, source: str, root: Path) -> tuple[str, str]:
        program, _ = compile_source(source, "io.tyv")
        output, errors = io.StringIO(), io.StringIO()
        Interpreter(program, output, project_root=root, error_output=errors).run()
        return output.getvalue(), errors.getvalue()

    def test_text_io_returns_result_values_and_path_operations_are_defined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = '''
fn main() {
    match writeText("note.txt", "hello") {
        Ok(wrote) => println(wrote)
        Err(_) => println("write failed")
    }
    match readText("note.txt") {
        Ok(contents) => println(contents)
        Err(_) => println("read failed")
    }
    match readText("missing.txt") {
        Ok(_) => println("unexpected")
        Err(_) => println("missing handled")
    }
    println(pathExists("note.txt"))
    println(pathExists("missing.txt"))
    println(pathJoin("folder", "note.txt"))
    eprintln("stderr works")
}
'''
            self.assertEqual(self._run(source, root), ("true\nhello\nmissing handled\ntrue\nfalse\nfolder/note.txt\n", "stderr works\n"))
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "hello")

    def test_io_type_errors_and_failure_results_are_checked(self) -> None:
        for source, message in [
            ('fn main() { readText(1) }', "expects String, found Int"),
            ('fn main() { writeText("x", false) }', "expects String, found Bool"),
            ('fn main() { pathJoin("x", 1) }', "expects String, found Int"),
            ('fn main() { println(readText("x")) }', "accepts Int, Float, Bool, String, or Byte, found Result<String,String>"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(Diagnostic, message):
                compile_source(source, "bad-io.tyv")

    def test_secret_values_are_wrapped_redacted_and_explicitly_revealed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = "test-token-74a0"
            (root / ".tyvera.secrets").write_text(f'DISCORD_TOKEN = "{fake}"\n', encoding="utf-8")
            output, _ = self._run('''
fn main() {
    match secrets.DISCORD_TOKEN {
        Some(token) => {
            println("configured")
            let recovered: String = token.reveal()
            if recovered == "test-token-74a0" {
                println("verified")
            }
        }
        None => println("missing")
    }
}
''', root)
            self.assertEqual(output, "configured\nverified\n")
            self.assertNotIn(fake, output)

    def test_secret_display_is_rejected_without_explicit_unwrapping(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "found Secret<String>"):
            compile_source('''fn main() {
    match secrets.DISCORD_TOKEN {
        Some(token) => println(token)
        None => {}
    }
}
''', "secret-display.tyv")

    def test_secret_provider_falls_back_to_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"TEST_SECRET": "environment-test-value"}):
            root = Path(temporary)
            output, _ = self._run('''fn main() {
    match secrets.TEST_SECRET {
        Some(_) => println("available")
        None => println("missing")
    }
}
''', root)
            self.assertEqual(output, "available\n")
            self.assertNotIn("environment-test-value", output)

    def test_local_secret_provider_takes_precedence_and_never_echoes_malformed_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"TEST_SECRET": "environment-value"}):
            root = Path(temporary)
            (root / ".tyvera.secrets").write_text('TEST_SECRET = "local-value"\n', encoding="utf-8")
            output, _ = self._run('''fn main() {
    match secrets.TEST_SECRET {
        Some(token) => {
            if token.reveal() == "local-value" { println("local first") }
        }
        None => println("missing")
    }
}
''', root)
            self.assertEqual(output, "local first\n")
            self.assertNotIn("local-value", output)

            raw_value = "never-print-this-secret"
            (root / ".tyvera.secrets").write_text(f"TEST_SECRET = {raw_value}\n", encoding="utf-8")
            program, _ = compile_source('''fn main() {
    let value: Option<Secret<String>> = secrets.TEST_SECRET
}
''', "bad-secret-file.tyv")
            with self.assertRaises(Diagnostic) as caught:
                Interpreter(program, io.StringIO(), project_root=root).run()
            self.assertNotIn(raw_value, caught.exception.render())
            self.assertIn("invalid quoted secret value", caught.exception.message)

    def test_secret_file_cannot_be_read_through_generic_text_io(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".tyvera.secrets").write_text('API_KEY = "private-test-value"\n', encoding="utf-8")
            output, _ = self._run('''fn main() {
    match readText(".tyvera.secrets") {
        Ok(contents) => println(contents)
        Err(_) => println("protected")
    }
}
''', root)
            self.assertEqual(output, "protected\n")
            self.assertNotIn("private-test-value", output)

    def test_secret_storage_path_is_ignored_by_git(self) -> None:
        self.assertIn(".tyvera.secrets", (ROOT / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
