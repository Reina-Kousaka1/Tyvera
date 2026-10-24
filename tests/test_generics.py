from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from tyvera.compiler import compile_project, compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class GenericTypeTests(unittest.TestCase):
    def _run(self, source: str) -> str:
        program, _ = compile_source(source, "generics.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        return output.getvalue()

    def test_generic_function_infers_types_and_returns_them_statically(self) -> None:
        self.assertEqual(self._run('''
fn identity<T>(value: T) -> T {
    return value
}
fn main() {
    let number: Int = identity(42)
    let text: String = identity("typed")
    println(number)
    println(text)
}
'''), "42\ntyped\n")

    def test_generic_function_interface_constraints_enable_checked_dispatch(self) -> None:
        source = '''
interface Named { fn name() -> String }
interface Identified { fn id() -> Int }
class Person implements Named, Identified {
    let displayName: String
    let identifier: Int
    fn init(displayName: String, identifier: Int) {
        self.displayName = displayName
        self.identifier = identifier
    }
    fn name() -> String { return self.displayName }
    fn id() -> Int { return self.identifier }
}
fn describe<T: Named + Identified>(person: T) -> String {
    println(person.id())
    return person.name()
}
fn relay<T: Named + Identified>(person: T) -> String { return describe(person) }
fn main() { println(relay(Person("Mira", 17))) }
'''
        self.assertEqual(self._run(source), "17\nMira\n")

    def test_interface_implementations_must_match_return_types(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "method 'name' does not match 'Named': expected return type String, found Int"):
            compile_source('''interface Named { fn name() -> String }
class Broken implements Named {
    fn name() -> Int { return 1 }
}
fn main() {}
''')

    def test_generic_function_constraint_rejects_unimplemented_capability(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "type 'Int' does not satisfy constraint 'Named'"):
            compile_source('''interface Named { fn name() -> String }
fn describe<T: Named>(value: T) -> String { return value.name() }
fn main() { println(describe(4)) }
''')

    def test_generic_function_without_a_bound_cannot_call_interface_methods(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "no interface constraint providing method 'name'"):
            compile_source('''interface Named { fn name() -> String }
fn describe<T>(value: T) -> String { return value.name() }
fn main() {}
''')

    def test_generic_function_constraints_work_across_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "main.tyv").write_text(
                'import "named.tyv" as named\n'
                'fn main() { println(named.label(named.Person("Ada"))) }\n', encoding="utf-8"
            )
            (root / "src" / "named.tyv").write_text(
                'pub interface Named { fn name() -> String }\n'
                'pub class Person implements Named {\n'
                '    let value: String\n'
                '    fn init(value: String) { self.value = value }\n'
                '    fn name() -> String { return self.value }\n'
                '}\n'
                'pub fn label<T: Named>(value: T) -> String { return value.name() }\n', encoding="utf-8"
            )
            program, _ = compile_project(root / "src/main.tyv", root)
            output = io.StringIO()
            Interpreter(program, output).run()
            self.assertEqual(output.getvalue(), "Ada\n")

    def test_explicit_type_arguments_work_when_inference_has_no_input(self) -> None:
        self.assertEqual(self._run('''
fn marker<T>() -> String {
    return "ready"
}
fn main() {
    println(marker<Int>())
}
'''), "ready\n")

    def test_generic_function_rejects_conflicting_or_missing_inference(self) -> None:
        for source, message in [
            ('''fn same<T>(left: T, right: T) -> T { return left }
fn main() { same(1, "wrong") }
''', "cannot infer one type"),
            ('''fn marker<T>() {}
fn main() { marker() }
''', "cannot infer type argument"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(Diagnostic, message):
                compile_source(source, "generic-error.tyv")

    def test_explicit_type_argument_arity_and_value_types_are_checked(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "expects 1 type argument"):
            compile_source('''fn identity<T>(value: T) -> T { return value }
fn main() { identity<Int, String>(1) }
''')
        with self.assertRaisesRegex(Diagnostic, "expected String, found Int"):
            compile_source('''fn identity<T>(value: T) -> T { return value }
fn main() { let text: String = identity<Int>(1) }
''')

    def test_generic_class_infers_constructor_type_and_checks_fields_and_methods(self) -> None:
        source = '''
class Box<T> {
    let value: T
    fn init(value: T) { self.value = value }
    fn get() -> T { return self.value }
}
fn main() {
    let box: Box<Int> = Box(42)
    println(box.get())
}
'''
        self.assertEqual(self._run(source), "42\n")
        bad = source.replace("let box: Box<Int> = Box(42)", "let box: Box<String> = Box(42)")
        with self.assertRaisesRegex(Diagnostic, "expected Box<String>, found Box<Int>"):
            compile_source(bad)

    def test_generic_class_rejects_wrong_constructor_and_type_argument_arity(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "expects Int, found String"):
            compile_source('''class Box<T> {
    let value: T
    fn init(value: T) { self.value = value }
}
fn main() { let box: Box<Int> = Box<Int>("wrong") }
''')
        with self.assertRaisesRegex(Diagnostic, "expects 1 type argument"):
            compile_source('''class Box<T> { let value: T
    fn init(value: T) { self.value = value }
}
fn main() { let box: Box = Box(1) }
''')

    def test_nested_generic_types_and_explicit_constructor_arguments(self) -> None:
        source = '''
class Box<T> {
    let value: T
    fn init(value: T) { self.value = value }
    fn get() -> T { return self.value }
}
fn main() {
    let inner: Box<Int> = Box(9)
    let outer: Box<Box<Int>> = Box(inner)
    let empty: Box<String> = Box<String>("ok")
    println(outer.get().get())
    println(empty.get())
}
'''
        self.assertEqual(self._run(source), "9\nok\n")

    def test_generic_function_can_preserve_a_generic_class_type(self) -> None:
        source = '''
class Box<T> {
    let value: T
    fn init(value: T) { self.value = value }
}
fn identity<T>(value: T) -> T { return value }
fn main() {
    let box: Box<Int> = identity(Box(11))
    println(box.value)
}
'''
        self.assertEqual(self._run(source), "11\n")

    def test_generic_class_and_function_work_through_an_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "main.tyv").write_text(
                'import "box.tyv" as boxes\n'
                'fn main() {\n'
                '    let box: boxes.Box<Int> = boxes.Box(17)\n'
                '    println(boxes.identity(box).get())\n'
                '}\n', encoding="utf-8"
            )
            (root / "src" / "box.tyv").write_text(
                'pub class Box<T> {\n'
                '    let value: T\n'
                '    fn init(value: T) { self.value = value }\n'
                '    fn get() -> T { return self.value }\n'
                '}\n'
                'pub fn identity<T>(value: T) -> T { return value }\n', encoding="utf-8"
            )
            program, _ = compile_project(root / "src/main.tyv", root)
            output = io.StringIO()
            Interpreter(program, output).run()
            self.assertEqual(output.getvalue(), "17\n")

    def test_explicit_generic_arguments_work_on_qualified_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "main.tyv").write_text(
                'import "library.tyv" as lib\n'
                'fn main() {\n'
                '    let empty: lib.Empty<Int> = lib.Empty<Int>()\n'
                '    println(lib.marker<String>())\n'
                '}\n', encoding="utf-8"
            )
            (root / "src" / "library.tyv").write_text(
                'pub class Empty<T> {}\n'
                'pub fn marker<T>() -> String { return "qualified" }\n', encoding="utf-8"
            )
            program, _ = compile_project(root / "src/main.tyv", root)
            output = io.StringIO()
            Interpreter(program, output).run()
            self.assertEqual(output.getvalue(), "qualified\n")

    def test_void_cannot_be_used_as_a_generic_argument(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "type arguments cannot be Void"):
            compile_source('''class Box<T> {}
fn main() { let box: Box<Void> = Box<Void>() }
''')


if __name__ == "__main__":
    unittest.main()
