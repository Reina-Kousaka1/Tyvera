from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tyvera.compiler import compile_project
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


ROOT = Path(__file__).resolve().parents[1]


class ModuleProjectTests(unittest.TestCase):
    def _project(self, files: dict[str, str]) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, source in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        return root

    def _run(self, root: Path, entry: str = "src/main.tyv") -> str:
        program, _ = compile_project(root / entry, root)
        output = io.StringIO()
        Interpreter(program, output).run()
        return output.getvalue()

    def test_imported_function_runs_across_two_files(self) -> None:
        root = self._project({
            "src/main.tyv": 'import "text.tyv" as text\nfn main() { println(text.message()) }\n',
            "src/text.tyv": 'pub fn message() -> String { return "from module" }\n',
        })
        self.assertEqual(self._run(root), "from module\n")

    def test_imported_class_and_interface_types_are_checked_and_dispatched(self) -> None:
        root = self._project({
            "src/main.tyv": (
                'import "model.tyv" as model\n'
                'fn main() {\n'
                '    let printable: model.Printable = model.User("Mira")\n'
                '    println(printable.display())\n'
                '}\n'
            ),
            "src/model.tyv": (
                'pub interface Printable { fn display() -> String }\n'
                'pub class User implements Printable {\n'
                '    let name: String\n'
                '    fn init(name: String) { self.name = name }\n'
                '    fn display() -> String { return self.name }\n'
                '}\n'
            ),
        })
        self.assertEqual(self._run(root), "Mira\n")

    def test_private_symbol_cannot_be_imported(self) -> None:
        root = self._project({
            "src/main.tyv": 'import "library.tyv" as lib\nfn main() { println(lib.hidden()) }\n',
            "src/library.tyv": 'fn hidden() -> String { return "secret" }\n',
        })
        with self.assertRaisesRegex(Diagnostic, "is private") as caught:
            compile_project(root / "src/main.tyv", root)
        self.assertEqual(caught.exception.location.filename, "src/main.tyv")

    def test_missing_imported_symbol_is_reported_at_use(self) -> None:
        root = self._project({
            "src/main.tyv": 'import "library.tyv" as lib\nfn main() { lib.missing() }\n',
            "src/library.tyv": 'pub fn available() {}\n',
        })
        with self.assertRaisesRegex(Diagnostic, "has no exported symbol 'missing'"):
            compile_project(root / "src/main.tyv", root)

    def test_missing_module_is_reported_at_import(self) -> None:
        root = self._project({"src/main.tyv": 'import "nope.tyv" as nope\nfn main() {}\n'})
        with self.assertRaisesRegex(Diagnostic, "cannot read module 'src/nope.tyv'") as caught:
            compile_project(root / "src/main.tyv", root)
        self.assertEqual(caught.exception.location.filename, "src/main.tyv")
        self.assertEqual(caught.exception.location.line, 1)

    def test_circular_import_reports_the_module_chain(self) -> None:
        root = self._project({
            "src/main.tyv": 'import "a.tyv" as a\nfn main() {}\n',
            "src/a.tyv": 'import "main.tyv" as mainmod\npub fn value() {}\n',
        })
        with self.assertRaisesRegex(Diagnostic, "circular import detected: src/main.tyv -> src/a.tyv -> src/main.tyv"):
            compile_project(root / "src/main.tyv", root)

    def test_import_cannot_escape_project_root(self) -> None:
        root = self._project({"src/main.tyv": 'import "../../outside.tyv" as outside\nfn main() {}\n'})
        with self.assertRaisesRegex(Diagnostic, "must stay inside the project root"):
            compile_project(root / "src/main.tyv", root)

    def test_duplicate_alias_and_duplicate_module_symbols_are_rejected(self) -> None:
        duplicate_alias = self._project({
            "src/main.tyv": 'import "a.tyv" as shared\nimport "b.tyv" as shared\nfn main() {}\n',
            "src/a.tyv": 'pub fn a() {}\n',
            "src/b.tyv": 'pub fn b() {}\n',
        })
        with self.assertRaisesRegex(Diagnostic, "import alias 'shared' is already declared"):
            compile_project(duplicate_alias / "src/main.tyv", duplicate_alias)

        duplicate_symbol = self._project({
            "src/main.tyv": 'import "lib.tyv" as lib\nfn main() { lib.value() }\n',
            "src/lib.tyv": 'pub fn value() {}\npub class value {}\n',
        })
        with self.assertRaisesRegex(Diagnostic, "symbol 'value' is declared more than once"):
            compile_project(duplicate_symbol / "src/main.tyv", duplicate_symbol)

    def test_project_modules_keep_same_named_private_functions_separate(self) -> None:
        root = self._project({
            "src/main.tyv": 'import "first.tyv" as first\nimport "second.tyv" as second\nfn main() { println(first.value() + second.value()) }\n',
            "src/first.tyv": 'fn helper() -> String { return "A" }\npub fn value() -> String { return helper() }\n',
            "src/second.tyv": 'fn helper() -> String { return "B" }\npub fn value() -> String { return helper() }\n',
        })
        self.assertEqual(self._run(root), "AB\n")


class ProjectCliTests(unittest.TestCase):
    def test_new_check_and_run_create_a_multi_file_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            created = subprocess.run(
                [sys.executable, "-m", "tyvera", "new", "demo"], cwd=root, text=True, capture_output=True
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertTrue((root / "demo" / "tyvera.toml").is_file())
            self.assertIn(".tyvera.secrets", (root / "demo" / ".gitignore").read_text(encoding="utf-8"))
            self.assertTrue((root / "demo" / ".tyvera.secrets.example").is_file())

            checked = subprocess.run(
                [sys.executable, "-m", "tyvera", "check", str(root / "demo")],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("checked src/main.tyv", checked.stdout)
            run = subprocess.run(
                [sys.executable, "-m", "tyvera", "run", str(root / "demo")],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "Tyvera project ready!\n")

    def test_new_refuses_to_overwrite_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "demo"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "tyvera", "new", "demo"], cwd=root, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertIn("refusing to overwrite", result.stderr)


if __name__ == "__main__":
    unittest.main()
