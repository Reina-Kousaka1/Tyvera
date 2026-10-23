from __future__ import annotations

import unittest

from tyvera.compiler import compile_checked_source


class CheckedProgramMetadataTests(unittest.TestCase):
    def test_checked_program_exposes_types_substitutions_aliases_and_match_coverage(self) -> None:
        checked = compile_checked_source('''type UserId = Int
record Pair<A, B> { first: A, second: B }
enum Outcome<T> { Value(T) Empty }
fn identity<T>(value: T) -> T { return value }
fn main() {
    let pair = Pair(identity(5), true)
    let tuple = (pair.first, pair.second)
    let outcome: Outcome<UserId> = Outcome.Value(pair.first)
    println(tuple.0)
    match outcome {
        Outcome.Value(value) => println(value)
        Outcome.Empty => println(0)
    }
}
''', "metadata.tyv")

        self.assertEqual(checked.type_alias_targets["UserId"], "Int")
        self.assertIn("Tuple<Int,Bool>", checked.expression_types.values())
        self.assertTrue(any(arguments.get("T") == "Int"
                            for arguments in checked.generic_substitutions.values()))
        self.assertEqual(list(checked.match_exhaustiveness.values()), [True])
        with self.assertRaises(TypeError):
            checked.expression_types[next(iter(checked.expression_types))] = "String"


if __name__ == "__main__":
    unittest.main()
