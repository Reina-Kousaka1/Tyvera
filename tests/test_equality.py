from __future__ import annotations

import io
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class PortableEqualityTests(unittest.TestCase):
    def _run(self, source: str) -> str:
        program, _ = compile_source(source, "equality.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        return output.getvalue()

    def test_numeric_and_duration_equality_is_value_based(self) -> None:
        self.assertEqual(self._run('''fn main() {
    println(1 == 1.0)
    println(1 != 1.5)
    println(milliseconds(500) == milliseconds(500))
}
'''), "true\ntrue\ntrue\n")

    def test_lists_options_and_maps_use_tyvera_value_equality(self) -> None:
        self.assertEqual(self._run('''fn main() {
    let first: List<Int> = List<Int>()
    let second: List<Int> = List<Int>()
    first.append(4)
    second.append(4)
    println(first == second)
    let nested: List<List<Int>> = List<List<Int>>()
    nested.append(first)
    println(nested.contains(second))
    println(Some(first) == Some(second))
    let left: Map<String, Int> = Map<String, Int>()
    let right: Map<String, Int> = Map<String, Int>()
    left.put("a", 1)
    left.put("b", 2)
    right.put("b", 2)
    right.put("a", 1)
    println(left == right)
}
'''), "true\ntrue\ntrue\ntrue\n")

    def test_classes_and_map_keys_use_stable_reference_identity(self) -> None:
        self.assertEqual(self._run('''class Box {
    let value: Int
    fn init(value: Int) { self.value = value }
}
fn main() {
    let first = Box(7)
    let same = first
    let other = Box(7)
    println(first == same)
    println(first == other)
    let values: Map<Box, String> = Map<Box, String>()
    values.put(first, "found")
    println(values.containsKey(same))
    println(values.containsKey(other))
}
'''), "true\nfalse\ntrue\nfalse\n")

    def test_unstable_map_key_types_are_still_rejected(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "Map key type 'List<Int>' is not a supported"):
            compile_source("fn main() { let values: Map<List<Int>, String> = Map<List<Int>, String>() }")

    def test_tuples_have_fixed_types_access_and_destructuring_patterns(self) -> None:
        self.assertEqual(self._run('''fn main() {
    let pair: (String, Int) = ("age", 24)
    println(pair.0)
    println(pair.1)
    println(pair == ("age", 24))
    match pair {
        (label, value) => { println(label)\n println(value) }
    }
}
'''), "age\n24\ntrue\nage\n24\n")

    def test_tuple_types_and_patterns_are_checked(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "tuple index 2 is out of range"):
            compile_source("fn main() { let pair = (1, true)\n println(pair.2) }")
        with self.assertRaisesRegex(Diagnostic, "tuple pattern with 3 elements does not match Tuple<Int,Bool>"):
            compile_source("fn main() { let pair = (1, true)\n match pair { (one, two, three) => {} } }")

    def test_records_are_immutable_value_types_with_patterns_and_hashable_keys(self) -> None:
        self.assertEqual(self._run('''record User<T> {
    id: T
    name: String
}
fn main() {
    let user = User<Int>(17, "Mira")
    let copy = User(17, "Mira")
    println(user == copy)
    println(user.name)
    let users: Map<User<Int>, String> = Map<User<Int>, String>()
    users.put(user, "member")
    println(users.containsKey(copy))
    match user {
        User(id, name) => { println(id)\n println(name) }
    }
}
'''), "true\nMira\ntrue\n17\nMira\n")

    def test_record_fields_are_immutable_and_unhashable_fields_prevent_keys(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "cannot assign to immutable field 'name'"):
            compile_source('''record User { name: String }
fn main() {
    var user = User("Mira")
    user.name = "Ada"
}
''')
        with self.assertRaisesRegex(Diagnostic, "Map key type 'MutableRecord' is not a supported"):
            compile_source('''record MutableRecord { values: List<Int> }
fn main() { let values: Map<MutableRecord, Int> = Map<MutableRecord, Int>() }
''')

    def test_type_aliases_are_transparent_and_cycles_are_rejected(self) -> None:
        self.assertEqual(self._run('''type UserId = Int
fn identity(value: UserId) -> UserId { return value }
fn main() {
    let id: UserId = identity(41)
    println(id == 41)
}
'''), "true\n")
        with self.assertRaisesRegex(Diagnostic, "type alias cycle includes 'UserId'"):
            compile_source("type UserId = UserId\nfn main() {}")
        self.assertEqual(self._run('''type Maybe<T> = Option<T>
fn main() {
    let value: Maybe<Int> = Some(9)
    match value {
        Some(item) => println(item)
        None => println(-1)
    }
}
'''), "9\n")

    def test_newtypes_are_nominal_values_with_access_patterns_and_hashing(self) -> None:
        self.assertEqual(self._run('''newtype UserId = Int
fn main() {
    let id = UserId(41)
    let copy = UserId(41)
    println(id == copy)
    println(id.value)
    let names: Map<UserId, String> = Map<UserId, String>()
    names.put(id, "Ada")
    println(names.containsKey(copy))
    match id { UserId(raw) => println(raw) }
}
'''), "true\n41\ntrue\n41\n")
        with self.assertRaisesRegex(Diagnostic, "cannot compare UserId with OrderId"):
            compile_source('''newtype UserId = Int
newtype OrderId = Int
fn main() { println(UserId(1) == OrderId(1)) }
''')

    def test_generic_enums_construct_match_and_check_exhaustiveness(self) -> None:
        self.assertEqual(self._run('''enum LoginResult<T> {
    Success(T)
    InvalidPassword
    Locked(Duration)
}
fn main() {
    let result = LoginResult.Success<Int>(7)
    match result {
        LoginResult.Success(value) => println(value)
        LoginResult.InvalidPassword => println("invalid")
        LoginResult.Locked(duration) => println(duration.inMilliseconds())
    }
    let empty: LoginResult<String> = LoginResult.InvalidPassword<String>()
    match empty {
        LoginResult.Success(_) => println("success")
        LoginResult.InvalidPassword => println("invalid")
        LoginResult.Locked(_) => println("locked")
    }
}
'''), "7\ninvalid\n")
        with self.assertRaisesRegex(Diagnostic, "variant 'Success' expects 1 payload pattern"):
            compile_source('''enum Outcome { Success(Int) Failure }
fn main() {
    let value = Outcome.Success(1)
    match value { Outcome.Success => {} }
}
''')

    def test_enum_cases_are_hashable_only_when_all_payloads_are_hashable(self) -> None:
        self.assertEqual(self._run('''enum Key { Number(Int) Empty }
fn main() {
    let values: Map<Key, String> = Map<Key, String>()
    values.put(Key.Number(3), "three")
    println(values.containsKey(Key.Number(3)))
    println(Key.Empty == Key.Empty())
}
'''), "true\ntrue\n")
        with self.assertRaisesRegex(Diagnostic, "Map key type 'ContainsList' is not a supported"):
            compile_source('''enum ContainsList { Value(List<Int>) }
fn main() { let values: Map<ContainsList, Int> = Map<ContainsList, Int>() }
''')

    def test_recursive_enum_keys_are_checked_without_unbounded_expansion(self) -> None:
        self.assertEqual(self._run('''enum Tree<T> { Leaf(T) Branch(Tree<T>, Tree<T>) }
fn main() {
    let trees: Map<Tree<Int>, String> = Map<Tree<Int>, String>()
    let tree = Tree.Branch(Tree.Leaf(3), Tree.Leaf(4))
    trees.put(tree, "branch")
    println(trees.containsKey(Tree.Branch(Tree.Leaf(3), Tree.Leaf(4))))
}
'''), "true\n")
        with self.assertRaisesRegex(Diagnostic, "Map key type 'Growing<Int>' is not a supported"):
            compile_source('''enum Growing<T> { Step(Growing<List<T>>) End(T) }
fn main() { let values: Map<Growing<Int>, String> = Map<Growing<Int>, String>() }
''')

    def test_nested_enum_patterns_and_unreachable_cases_are_checked(self) -> None:
        self.assertEqual(self._run('''enum Lookup<T> { Found(T) Missing }
fn main() {
    let value = Lookup.Found((12, "records"))
    match value {
        Lookup.Found((count, label)) => { println(count)\n println(label) }
        Lookup.Missing => println("missing")
    }
}
'''), "12\nrecords\n")
        with self.assertRaisesRegex(Diagnostic, "match arm for 'Empty' is unreachable"):
            compile_source('''enum Choice { Empty Full(Int) }
fn main() {
    let value = Choice.Empty
    match value { Choice.Empty => {}\n Choice.Empty => {} }
}
''')

    def test_nested_adt_patterns_prove_exhaustiveness_for_return_analysis(self) -> None:
        self.assertEqual(self._run('''enum Reply { Data(Option<Bool>) Closed }
fn score(reply: Reply) -> Int {
    match reply {
        Reply.Data(Some(true)) => return 1
        Reply.Data(Some(false)) => return 2
        Reply.Data(None) => return 3
        Reply.Closed => return 0
    }
}
fn main() {
    println(score(Reply.Data(Some(false))))
    println(score(Reply.Closed))
}
'''), "2\n0\n")

    def test_nested_adt_patterns_reject_covered_subcases_as_unreachable(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "match arm is unreachable"):
            compile_source('''enum Reply { Data(Option<Int>) Closed }
fn main() {
    match Reply.Data(Some(5)) {
        Reply.Data(Some(_)) => {}
        Reply.Data(Some(5)) => {}
        Reply.Data(None) => {}
        Reply.Closed => {}
    }
}
''')

    def test_recursive_enum_match_exhaustiveness_terminates(self) -> None:
        self.assertEqual(self._run('''enum Tree<T> { Leaf(T) Branch(Tree<T>, Tree<T>) }
fn depth(tree: Tree<Int>) -> Int {
    match tree {
        Tree.Leaf(_) => return 1
        Tree.Branch(_, _) => return 2
    }
}
fn main() { println(depth(Tree.Leaf(3))) }
'''), "1\n")

    def test_tuple_product_patterns_are_checked_recursively(self) -> None:
        self.assertEqual(self._run('''fn code(pair: (Bool, Bool)) -> Int {
    match pair {
        (true, true) => return 1
        (true, false) => return 2
        (false, true) => return 3
        (false, false) => return 4
    }
}
fn main() { println(code((false, true))) }
'''), "3\n")


if __name__ == "__main__":
    unittest.main()
