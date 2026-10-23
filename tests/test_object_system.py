from __future__ import annotations

import io
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class ObjectSystemTests(unittest.TestCase):
    def run_source(self, source: str) -> str:
        program, _ = compile_source(source, "objects.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        return output.getvalue()

    def reject(self, source: str, message: str, stage: str = "type") -> Diagnostic:
        with self.assertRaisesRegex(Diagnostic, message) as caught:
            compile_source(source, "objects-bad.tyv")
        self.assertEqual(caught.exception.stage, stage)
        return caught.exception

    def test_class_fields_constructor_self_and_methods(self):
        source = '''class User {
    let name: String
    var score: Int

    fn init(name: String, score: Int) {
        self.name = name
        self.score = score
    }

    fn addScore(points: Int) -> Void {
        self.score = self.score + points
    }
}

fn main() {
    let user: User = User("Charlotte", 10)
    user.addScore(5)
    println(user.name)
    println(user.score)
}
'''
        self.assertEqual(self.run_source(source), "Charlotte\n15\n")

    def test_constructor_default_return_and_instance_methods(self):
        source = '''class Counter {
    var count: Int
    fn init(start: Int) { self.count = start }
    fn increment() { self.count = self.count + 1 }
    fn value() -> Int { return self.count }
}
fn main() {
    let counter = Counter(3)
    counter.increment()
    println(counter.value())
}
'''
        self.assertEqual(self.run_source(source), "4\n")

    def test_class_equality_uses_instance_identity(self):
        source = '''class User {
let name: String
fn init(name: String) { self.name = name }
}
fn main() {
    let first = User("A")
    let alias: User = first
    println(first == alias)
    println(first == User("A"))
}
'''
        self.assertEqual(self.run_source(source), "true\nfalse\n")

    def test_mutable_and_immutable_fields_are_checked(self):
        self.reject('''class User {
let name: String
var score: Int
fn init(name: String, score: Int) { self.name = name; self.score = score }
}
fn main() { let u = User("A", 1); u.name = "B" }
''', "immutable field 'name'")
        self.reject('''class User {
var score: Int
fn init(score: Int) { self.score = score }
}
fn main() { let u = User(1); u.score = "wrong" }
''', "expected Int, found String")
        self.assertEqual(self.run_source('''class User {
var score: Int
fn init(score: Int) { self.score = score }
}
fn main() { let u = User(1); u.score = 8; println(u.score) }
'''), "8\n")

    def test_fields_are_definitely_initialized_on_constructor_paths(self):
        self.reject('''class User {
let name: String
var score: Int
fn init(name: String, score: Int) { self.name = name }
}
fn main() {}
''', "does not initialize field 'score'")
        self.reject('''class User {
let name: String
fn init(name: String, flag: Bool) { if flag { self.name = name } }
}
fn main() {}
''', "does not initialize field 'name'")
        self.reject('''class User {
let name: String
fn init(name: String) { self.name = name; self.name = name }
}
fn main() {}
''', "initialized more than once")
        self.reject('''class User {
let name: String
fn init(name: String) { println(self.name); self.name = name }
}
fn main() {}
''', "read before initialization")

    def test_conditional_constructor_initialization_must_cover_all_branches(self):
        source = '''class User {
let name: String
fn init(name: String, flag: Bool) {
    if flag { self.name = name } else { self.name = name }
}
}
fn main() { println(User("ok", true).name) }
'''
        self.assertEqual(self.run_source(source), "ok\n")

    def test_constructor_cannot_escape_self_before_all_fields_are_ready(self):
        source = '''class User {
let name: String
var score: Int
fn init(name: String, score: Int) {
    self.name = name
    self.inspect()
    self.score = score
}
fn inspect() {}
}
fn main() {}
'''
        self.reject(source, "'self' cannot call methods before field 'score' is initialized")

    def test_constructor_can_pass_self_after_fields_are_ready(self):
        source = '''class User {
let name: String
fn init(name: String) {
    self.name = name
    inspect(self)
}
}
fn inspect(other: User) {}
fn main() { let user = User("ready"); println(user.name) }
'''
        self.assertEqual(self.run_source(source), "ready\n")

    def test_constructor_is_required_for_fields_and_has_typed_arguments(self):
        self.reject('''class User { let name: String }
fn main() {}''', "requires an 'init' constructor")
        self.reject('''class User {
let name: String
fn init(name: String) { self.name = name }
}
fn main() { let u = User(3) }
''', "expects String, found Int")
        self.reject('''class User {
var n: Int
fn init(n: Int) -> Int { self.n = n; return n }
}
fn main() {}
''', "constructors must return Void")

    def test_constructor_cannot_be_called_as_an_instance_method(self):
        self.reject('''class User {
let name: String
fn init(name: String) { self.name = name }
}
fn main() { let user = User("A"); user.init("B") }
''', "constructors can only run when creating an instance")

    def test_empty_class_can_use_default_constructor(self):
        self.assertEqual(self.run_source('class Marker {}\nfn main() { let marker = Marker(); println("ready") }'), "ready\n")

    def test_constructor_return_requires_all_fields_initialized(self):
        self.reject('''class User {
let name: String
var score: Int
fn init(name: String, score: Int, flag: Bool) {
    self.name = name
    if flag { return }
    self.score = score
}
}
fn main() {}
''', "may return before field 'score' is initialized")

    def test_unknown_fields_methods_and_invalid_receiver_types(self):
        source = '''class User {
let name: String
fn init(name: String) { self.name = name }
}
fn main() { let u = User("A"); println(u.missing) }
'''
        self.reject(source, "has no field 'missing'")
        self.reject('fn main() { let n = 1; n.nope() }', "has no method 'nope'")
        self.reject('fn main() { println(1.name) }', "has no field 'name'")

    def test_self_is_only_available_inside_instance_methods(self):
        self.reject('fn main() { println(self) }', "unknown variable 'self'")
        self.reject('''class User {
let name: String
fn init(name: String) { self.name = name }
fn bad(self: String) {}
}
fn main() {}
''', "'self' is reserved")
        self.reject('''class User {
let name: String
fn init(name: String) { self.name = name }
fn bad() { let self = 3 }
}
fn main() {}
''', "'self' is reserved")

    def test_interface_dispatch_and_interface_typed_values(self):
        source = '''interface Printable {
    fn print() -> Void
}
class User implements Printable {
    let name: String
    fn init(name: String) { self.name = name }
    fn print() -> Void { println(self.name) }
}
fn show(value: Printable) { value.print() }
fn main() {
    let printable: Printable = User("Charlotte")
    show(printable)
}
'''
        self.assertEqual(self.run_source(source), "Charlotte\n")

    def test_multiple_interfaces_are_supported(self):
        source = '''interface Named { fn name() -> String }
interface Scored { fn score() -> Int }
class Player implements Named, Scored {
    let display: String
    fn init(display: String) { self.display = display }
    fn name() -> String { return self.display }
    fn score() -> Int { return 7 }
}
fn main() {
    let named: Named = Player("A")
    let scored: Scored = Player("B")
    println(named.name())
    println(scored.score())
}
'''
        self.assertEqual(self.run_source(source), "A\n7\n")

    def test_interface_missing_or_incompatible_methods_are_rejected(self):
        self.reject('''interface Printable { fn print() -> Void }
class User implements Printable {}
fn main() {}
''', "does not implement 'Printable.print'")
        self.reject('''interface Printable { fn print(value: String) -> Void }
class User implements Printable { fn print(value: Int) -> Void {} }
fn main() {}
''', "parameter types differ")
        self.reject('''interface Printable { fn print() -> Void }
class User implements Printable { fn print() -> Int { return 1 } }
fn main() {}
''', "expected return type Void, found Int")

    def test_interface_names_and_declarations_are_checked(self):
        self.reject('''class User implements Missing {}
fn main() {}''', "unknown interface 'Missing'")
        self.reject('''interface I { fn run() }
fn main() { let value = I() }''', "interfaces cannot be constructed")
        self.reject('''interface I { fn run() }
fn main() { let value: I = 1 }''', "expected I, found Int")
        self.reject('''interface I { fn run() }
class C implements I, I { fn run() {} }
fn main() {}
''', "listed more than once")

    def test_class_and_interface_names_share_a_namespace_with_functions(self):
        self.reject('''class User {}
fn User() {}
fn main() {}''', "function or type 'User' is already declared")
        self.reject('''interface Value {}
class Value {}
fn main() {}''', "type 'Value' is already declared")

    def test_duplicate_members_and_void_fields_are_rejected(self):
        self.reject('''class User {
let name: String
let name: String
fn init(name: String) { self.name = name }
}
fn main() {}
''', "field 'name' is already declared")
        self.reject('''class User {
var score: Int
fn init(score: Int) { self.score = score }
fn score() -> Int { return self.score }
}
fn main() {}
''', "conflicts with a field")
        self.reject('class Empty { let missing: Void }\nfn main() {}', "fields cannot have type Void")

    def test_method_return_types_and_parameter_types_are_checked(self):
        self.reject('''class User {
let id: Int
fn init(id: Int) { self.id = id }
fn get() -> Int { return "bad" }
}
fn main() {}
''', "expected Int, found String")
        self.reject('''class User {
let id: Int
fn init(id: Int) { self.id = id }
fn get(flag: Bool) -> Int { if flag { return self.id } }
}
fn main() {}
''', "must return Int on all reachable paths")
        self.reject('''class User {
let id: Int
fn init(id: Int) { self.id = id }
fn get(other: String) -> Int { return self.id }
}
fn main() { let u = User(1); println(u.get(2)) }
''', "expects String, found Int")

    def test_class_field_diagnostics_have_source_location(self):
        error = self.reject('''class User {
let name: String
fn init(name: String) { self.name = name }
}
fn main() {
    let user = User("A")
    println(user.missing)
}
''', "has no field 'missing'")
        self.assertEqual((error.location.filename, error.location.line), ("objects-bad.tyv", 7))

    def test_malformed_field_declaration_has_syntax_location(self):
        error = self.reject('''class User {
    let name String
}
fn main() {}
''', "expected ':' after field name", stage="syntax")
        self.assertEqual((error.location.filename, error.location.line, error.location.column),
                         ("objects-bad.tyv", 2, 14))


if __name__ == "__main__":
    unittest.main()
