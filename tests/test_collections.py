from __future__ import annotations

import io
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class CollectionAndForTests(unittest.TestCase):
    def _run(self, source: str) -> str:
        program, _ = compile_source(source, "collections.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        return output.getvalue()

    def test_list_creation_mutation_access_length_and_iteration(self) -> None:
        source = '''
fn main() {
    let values: List<Int> = List<Int>()
    values.append(1)
    values.insert(0, 0)
    values.set(1, 2)
    values.append(3)
    println(values.length)
    println(values.get(1))
    println(values.contains(3))
    var total: Int = 0
    for value in values {
        if value == 0 {
            continue
        }
        total = total + value
        if total > 2 {
            break
        }
    }
    println(total)
    println(values.removeAt(0))
    println(values.length)
}
'''
        self.assertEqual(self._run(source), "3\n2\ntrue\n5\n0\n2\n")

    def test_map_creation_access_mutation_and_key_iteration(self) -> None:
        source = '''
fn main() {
    let scores: Map<String, Int> = Map<String, Int>()
    scores.put("Ada", 9)
    scores.put("Mira", 12)
    scores.put("Ada", 10)
    println(scores.length)
    println(scores.get("Ada"))
    println(scores.containsKey("Mira"))
    var names: Int = 0
    for key in scores {
        println(key)
        names = names + 1
    }
    println(names)
    println(scores.keys().length)
    println(scores.values().get(1))
    println(scores.remove("Mira"))
    println(scores.length)
}
'''
        self.assertEqual(self._run(source), "2\n10\ntrue\nAda\nMira\n2\n2\n12\ntrue\n1\n")

    def test_for_variable_is_lexical_and_typed(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "unknown variable 'value'"):
            compile_source('''fn main() {
    let values: List<Int> = List<Int>()
    for value in values { println(value) }
    println(value)
}
''')
        with self.assertRaisesRegex(Diagnostic, "expected String, found Int"):
            compile_source('''fn main() {
    let values: List<Int> = List<Int>()
    for value in values { let wrong: String = value }
}
''')

    def test_for_accepts_map_keys_and_rejects_non_collections(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "must be a List or Map"):
            compile_source("fn main() { for value in 42 {} }")
        with self.assertRaisesRegex(Diagnostic, "expects 1 type argument"):
            compile_source("fn main() { let values: List = List<Int>() }")

    def test_collection_element_and_map_value_types_are_checked(self) -> None:
        bad_list = '''fn main() {
    let values: List<Int> = List<Int>()
    values.append("wrong")
}
'''
        with self.assertRaisesRegex(Diagnostic, "expects Int, found String"):
            compile_source(bad_list)
        bad_map = '''fn main() {
    let values: Map<String, Int> = Map<String, Int>()
    values.put("key", "wrong")
}
'''
        with self.assertRaisesRegex(Diagnostic, "expects Int, found String"):
            compile_source(bad_map)

    def test_map_keys_must_have_a_stable_supported_type(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "Map key type 'List<Int>' is not a supported"):
            compile_source("fn main() { let values: Map<List<Int>, String> = Map<List<Int>, String>() }")
        with self.assertRaisesRegex(Diagnostic, "Map key type 'List<Int>' is not a supported"):
            compile_source("fn main() { let values = Map<List<Int>, String>() }")

    def test_for_loop_does_not_prove_a_return_path(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "must return Int on all reachable paths"):
            compile_source('''fn first(values: List<Int>) -> Int {
    for value in values { return value }
}
fn main() {}
''')

    def test_collection_bounds_and_missing_map_keys_are_runtime_diagnostics(self) -> None:
        for body, message in [
            ('''let values: List<Int> = List<Int>()
println(values.get(0))''', "List index 0 is out of range"),
            ('''let values: Map<String, Int> = Map<String, Int>()
println(values.get("missing"))''', "Map.get could not find the requested key"),
        ]:
            program, _ = compile_source(f"fn main() {{\n{body}\n}}", "bounds.tyv")
            with self.subTest(message=message), self.assertRaisesRegex(Diagnostic, message):
                Interpreter(program, io.StringIO()).run()

    def test_iteration_over_values_uses_a_stable_snapshot(self) -> None:
        source = '''
fn main() {
    let values: List<Int> = List<Int>()
    values.append(1)
    var total: Int = 0
    for value in values {
        total = total + value
        values.append(2)
    }
    println(total)
    println(values.length)
}
'''
        self.assertEqual(self._run(source), "1\n2\n")

    def test_collection_values_can_flow_through_generic_functions(self) -> None:
        source = '''
fn identity<T>(value: T) -> T { return value }
fn main() {
    let values: List<Int> = identity(List<Int>())
    values.append(8)
    println(values.get(0))
}
'''
        self.assertEqual(self._run(source), "8\n")


if __name__ == "__main__":
    unittest.main()
