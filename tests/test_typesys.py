from __future__ import annotations

import unittest

from tyvera.compiler import compile_source
from tyvera.source import Diagnostic


class TypeCheckerTests(unittest.TestCase):
    def test_infers_local_and_checks_user_function_calls(self) -> None:
        source = '''fn add(left: Int, right: Int) -> Int {
    return left + right
}
fn main() {
    let result: Int = add(2, 3)
    println(result)
}
'''
        program, signatures = compile_source(source, "valid.tyv")
        self.assertEqual(len(program.functions), 2)
        self.assertEqual(signatures["add"].return_type, "Int")

    def test_annotation_mismatch_is_a_type_error(self) -> None:
        source = 'fn main() {\n    let count: Int = "three"\n}\n'
        with self.assertRaises(Diagnostic) as caught:
            compile_source(source, "types.tyv")
        self.assertEqual(caught.exception.stage, "type")
        self.assertEqual(caught.exception.location.filename, "types.tyv")
        self.assertIn("expected Int, found String", caught.exception.message)

    def test_wrong_argument_type_is_rejected(self) -> None:
        source = '''fn greet(name: String) {
    println(name)
}
fn main() {
    greet(4)
}
'''
        with self.assertRaisesRegex(Diagnostic, "expects String, found Int"):
            compile_source(source)

    def test_unknown_variables_and_functions_are_rejected(self) -> None:
        for expression, message in [("missing", "unknown variable"), ("absent()", "unknown function")]:
            source = f"fn main() {{ let value = {expression} }}"
            with self.subTest(expression=expression), self.assertRaisesRegex(Diagnostic, message):
                compile_source(source)

    def test_missing_or_invalid_entry_point_is_rejected(self) -> None:
        with self.assertRaisesRegex(Diagnostic, r"must define 'fn main\(\)'"):
            compile_source("fn helper() {}", "entry.tyv")
        with self.assertRaisesRegex(Diagnostic, "must not have parameters"):
            compile_source("fn main(value: Int) {}", "entry.tyv")

    def test_value_returning_function_requires_return(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "must return Int"):
            compile_source("fn value() -> Int {}\nfn main() {}")

    def test_out_of_range_integer_literal_is_rejected(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "outside the signed 64-bit range"):
            compile_source("fn main() { let value = 9223372036854775808 }")

    def test_builtin_name_cannot_be_shadowed(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "'println' is reserved"):
            compile_source('fn main() { let println = "shadow" }')

    def test_errors_are_found_before_execution(self) -> None:
        source = '''fn main() {
    println("this must not be printed")
    println(1 + true)
}
'''
        with self.assertRaisesRegex(Diagnostic, "expects numeric operands"):
            compile_source(source, "late-error.tyv")
