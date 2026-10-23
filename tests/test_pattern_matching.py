from __future__ import annotations

import io
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class PatternMatchingTests(unittest.TestCase):
    def _run(self, source: str) -> str:
        program, _ = compile_source(source, "patterns.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        return output.getvalue()

    def test_option_result_and_literal_patterns(self) -> None:
        source = '''
fn main() {
    let present: Option<Int> = Some(7)
    let absent: Option<Int> = None<Int>()
    match present {
        Some(value) => println(value)
        None => println("empty")
    }
    match absent {
        Some(_) => println("unexpected")
        None => println("none")
    }
    let successful: Result<Int, String> = Ok<Int, String>(13)
    let failed: Result<Int, String> = Err<Int, String>("offline")
    match successful {
        Ok(value) => println(value)
        Err(message) => println(message)
    }
    match failed {
        Ok(value) => println(value)
        Err(message) => println(message)
    }
    match 404 {
        200 => println("OK")
        404 => println("Not found")
        _ => println("unknown")
    }
}
'''
        self.assertEqual(self._run(source), "7\nnone\n13\noffline\nNot found\n")

    def test_binding_pattern_is_typed_scoped_and_immutable(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "unknown variable 'copy'"):
            compile_source('''fn main() {
    match "value" { copy => println(copy) }
    println(copy)
}
''')
        with self.assertRaisesRegex(Diagnostic, "cannot assign to immutable binding 'copy'"):
            compile_source('''fn main() {
    match 1 { copy => copy = 2 }
}
''')

    def test_variant_payload_binding_has_the_payload_type(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "expected String, found Int"):
            compile_source('''fn main() {
    let value: Option<Int> = Some(1)
    match value {
        Some(item) => { let text: String = item }
        None => {}
    }
}
''')

    def test_match_pattern_types_and_variants_are_checked(self) -> None:
        for source, message in [
            ('''fn main() {
    match 1 {
        "one" => {}
        _ => {}
    }
}
''', "pattern has type String, but match value has type Int"),
            ('''fn main() {
    match 1 {
        Some(item) => {}
        _ => {}
    }
}
''', "variant patterns require Option or Result"),
            ('''fn main() {
    match None<Int>() {
        Ok(value) => {}
        _ => {}
    }
}
''', "Option<Int> has no pattern 'Ok'"),
            ('''fn main() {
    match true {
        true => {}
        true => {}
        _ => {}
    }
}
''', "duplicate match pattern"),
            ('''fn main() {
    match true {
        _ => {}
        false => {}
    }
}
''', "unreachable after a wildcard or binding"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(Diagnostic, message):
                compile_source(source, "pattern-error.tyv")

    def test_option_and_result_constructors_are_typed(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "cannot infer type argument 'T'"):
            compile_source("fn main() { let value = None() }")
        with self.assertRaisesRegex(Diagnostic, "expects String, found Int"):
            compile_source('''fn main() { let value: Option<String> = Some<String>(1) }
''')
        with self.assertRaisesRegex(Diagnostic, "expects 2 type argument"):
            compile_source('''fn main() { let value: Result<Int, String> = Ok<Int>(1) }
''')

    def test_exhaustive_matches_contribute_to_return_path_analysis(self) -> None:
        source = '''
fn value(flag: Bool) -> Int {
    match flag {
        true => return 1
        false => return 0
    }
}
fn unwrap(option: Option<Int>) -> Int {
    match option {
        Some(value) => return value
        None => return 0
    }
}
fn main() {
    println(value(true))
    println(unwrap(Some(5)))
}
'''
        self.assertEqual(self._run(source), "1\n5\n")
        with self.assertRaisesRegex(Diagnostic, "must return Int on all reachable paths"):
            compile_source('''fn value(option: Option<Int>) -> Int {
    match option { Some(value) => return value }
}
fn main() {}
''')

    def test_match_on_a_missing_optional_value_falls_through_without_a_default(self) -> None:
        source = '''
fn main() {
    let missing: Option<Int> = None<Int>()
    var seen: Int = 0
    match missing { Some(value) => seen = value }
    println(seen)
}
'''
        self.assertEqual(self._run(source), "0\n")


if __name__ == "__main__":
    unittest.main()
