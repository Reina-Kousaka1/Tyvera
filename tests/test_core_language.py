"""Core semantics through the public checked compilation pipeline."""
import io
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class CoreLanguageTests(unittest.TestCase):
    def run_source(self, source):
        program, _ = compile_source(source, "core.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        return output.getvalue()

    def reject(self, source, message):
        with self.assertRaisesRegex(Diagnostic, message) as caught:
            compile_source(source, "bad.tyv")
        self.assertEqual(caught.exception.stage, "type")
        return caught.exception

    def test_if_true_and_false(self):
        self.assertEqual(self.run_source('fn main() {\nif false { println("bad") }\nif true { println("yes") }\n}'), "yes\n")

    def test_if_else_both_branches(self):
        for condition, expected in [("true", "yes"), ("false", "no")]:
            with self.subTest(condition=condition):
                self.assertEqual(self.run_source(f'fn main() {{ if {condition} {{ println("yes") }} else {{ println("no") }} }}'), expected + "\n")

    def test_else_if_and_newline_else(self):
        source = '''fn main() {
if false { println("bad") }
else if true { println("middle") }
else { println("bad") }
println("end")
}'''
        self.assertEqual(self.run_source(source), "middle\nend\n")

    def test_condition_type_and_source_location(self):
        for keyword in ["if", "while"]:
            with self.subTest(keyword=keyword):
                error = self.reject(f'fn main() {{\n    {keyword} 42 {{ println("bad") }}\n}}', "expected Bool, found Int")
                self.assertEqual((error.location.filename, error.location.line, error.location.column), ("bad.tyv", 2, len(keyword) + 6))

    def test_all_non_bool_conditions_rejected(self):
        for expression in ['"true"', '1.0', 'println("x")']:
            for keyword in ["if", "while"]:
                with self.subTest(expression=expression, keyword=keyword):
                    self.reject(f'fn main() {{ {keyword} {expression} {{}} }}', "expected Bool")

    def test_block_variable_does_not_escape(self):
        for block in ['if true { let message = "hi" }', '{ let message = "hi" }', 'while false { let message = "hi" }']:
            with self.subTest(block=block):
                self.reject(f'fn main() {{\n{block}\nprintln(message)\n}}', "unknown variable 'message'")

    def test_nested_shadowing_restores_outer_binding(self):
        self.assertEqual(self.run_source('''fn main() {
let name = "Charlotte"
if true {
    let name = "Tyvera"
    println(name)
}
println(name)
}'''), "Tyvera\nCharlotte\n")

    def test_shadow_initializer_uses_outer_binding(self):
        self.assertEqual(self.run_source('fn main() { let n = 2; { let n = n + 1; println(n) }; println(n) }'), "3\n2\n")

    def test_same_scope_duplicates_rejected(self):
        for body in ['let n = 1; var n = 2', 'var n = 1; let n = 2']:
            self.reject(f'fn main() {{ {body} }}', "already declared")

    def test_parameter_redeclaration_rejected(self):
        self.reject('fn f(n: Int) { let n = 1 }\nfn main() {}', "already declared")

    def test_let_and_parameters_are_immutable(self):
        self.reject('fn main() { let n = 1; n = 2 }', "immutable")
        self.reject('fn f(n: Int) { n = 2 }\nfn main() {}', "immutable")

    def test_var_reassign_and_outer_mutation(self):
        self.assertEqual(self.run_source('fn main() { var n: Int = 1; if true { n = n + 2 }; println(n) }'), "3\n")

    def test_assignment_targets_nearest_binding(self):
        self.assertEqual(self.run_source('fn main() { var n = 1; { var n = 2; n = 3; println(n) }; println(n) }'), "3\n1\n")

    def test_immutable_shadow_blocks_outer_mutation(self):
        self.reject('fn main() { var n = 1; { let n = 2; n = 3 } }', "immutable")

    def test_assignment_types_are_exact(self):
        for declaration, value in [('var n: Int = 0', '"hello"'), ('var n = 0', '1.0'), ('var n: Float = 0.0', '1')]:
            with self.subTest(declaration=declaration, value=value):
                self.reject(f'fn main() {{ {declaration}; n = {value} }}', "expected")

    def test_unknown_assignment_and_use_before_declaration(self):
        self.reject('fn main() { n = 1 }', "unknown variable")
        self.reject('fn main() { println(n); var n = 1 }', "unknown variable")

    def test_no_dynamic_caller_scope(self):
        self.reject('fn f() { println(n) }\nfn main() { let n = 1; f() }', "unknown variable")

    def test_arithmetic_precedence_and_result_types(self):
        self.assertEqual(self.run_source('''fn main() {
println(2 + 3 * 4)
println((2 + 3) * 4)
println(10 - 3 - 2)
println(-2 * 3)
let quotient: Float = 5 / 2
println(quotient)
let mixed: Float = 1 + 2.5
println(mixed)
println(4.5 - 0.5)
println(2.0 * 3.0)
println(-7 % 3)
println(7 % -3)
println(-7.5 % 2.0)
println(7 % -2.5)
println(14 % 5 * 2)
println("Ty" + "vera")
}'''), "14\n20\n5\n-6\n2.5\n3.5\n4.0\n6.0\n-1\n1\n-1.5\n2.0\n8\nTyvera\n")

    def test_remainder_by_zero_is_a_located_runtime_error(self):
        for expression in ("7 % 0", "7.0 % -0.0"):
            with self.subTest(expression=expression):
                program, _ = compile_source(f"fn main() {{ println({expression}) }}", "remainder.tyv")
                with self.assertRaisesRegex(Diagnostic, "remainder by zero") as caught:
                    Interpreter(program, io.StringIO()).run()
                self.assertEqual(caught.exception.location.filename, "remainder.tyv")

    def test_integer_literals_do_not_depend_on_python_bigint_digit_limits(self):
        with self.assertRaisesRegex(Diagnostic, "Int literal is outside the signed 64-bit range"):
            compile_source(f"fn main() {{ println({'9' * 5000}) }}", "large-int.tyv")
        with self.assertRaisesRegex(Diagnostic, "Int literal is outside the signed 64-bit range"):
            compile_source("fn main() { println(-9223372036854775809) }", "negative-large-int.tyv")
        output = self.run_source(f"fn main() {{ println({'0' * 5000}1) }}")
        self.assertEqual(output, "1\n")

    def test_comparisons(self):
        self.assertEqual(self.run_source('''fn main() {
println(1 == 1.0)
println(1 != 2)
println(1 < 2)
println(2 <= 2)
println(3 > 2)
println(3 >= 3)
println("a" == "a")
println(true != false)
}'''), "true\n" * 8)

    def test_mixed_comparison_does_not_round_integer(self):
        self.assertEqual(self.run_source('fn main() { println(9007199254740993 == 9007199254740992.0); println(9007199254740993 > 9007199254740992.0) }'), "false\ntrue\n")

    def test_function_calls_get_independent_mutable_locals(self):
        self.assertEqual(self.run_source('fn f() -> Int { var n = 0; n = n + 1; return n }\nfn main() { println(f()); println(f()) }'), "1\n1\n")

    def test_recursive_control_flow(self):
        self.assertEqual(self.run_source('fn sum(n: Int) -> Int { if n == 0 { return 0 }; return n + sum(n - 1) }\nfn main() { println(sum(10)) }'), "55\n")

    def test_recursion_limit_is_a_located_diagnostic(self):
        source = 'fn recurse() { recurse() }\nfn main() { recurse() }'
        program, _ = compile_source(source, "recursion.tyv")
        for limit in [5, 256]:
            with self.subTest(limit=limit), self.assertRaises(Diagnostic) as caught:
                Interpreter(program, io.StringIO(), max_call_depth=limit).run()
            self.assertEqual(caught.exception.stage, "runtime")
            self.assertEqual(caught.exception.location.filename, "recursion.tyv")

    def test_scoped_bindings_can_shadow_with_a_new_type(self):
        self.assertEqual(self.run_source('fn main() { let n = 1; { var n = "text"; n = "new"; println(n) }; println(n) }'), "new\n1\n")

    def test_nonliteral_conditions_are_conservative(self):
        self.reject('fn f() -> Int { if 1 == 1 { return 1 } }\nfn main() {}', "must return Int")

    def test_statement_diagnostics_locate_assignment_and_return(self):
        for source, message, line, column in [
            ('fn main() {\n    let n = 1\n    n = 2\n}', 'immutable', 3, 5),
            ('fn f() -> Int {\n    return "bad"\n}\nfn main() {}', 'expected Int', 2, 5),
        ]:
            error = self.reject(source, message)
            self.assertEqual((error.location.line, error.location.column), (line, column))

    def test_boolean_operator_precedence(self):
        self.assertEqual(self.run_source('fn main() { println(true || false && false); println(!true || false); println(1 < 2 && 3 >= 3) }'), "true\nfalse\ntrue\n")

    def test_both_symbolic_boolean_operators_short_circuit(self):
        self.assertEqual(self.run_source('fn fail() -> Bool { return 1 / 0 == 0 }\nfn main() { println(false && fail()); println(true || fail()) }'), "false\ntrue\n")

    def test_invalid_operator_types(self):
        for expr in ['1 + true', '"a" - "b"', 'true * 1', '!1', '1 && true', 'false || 2', '"a" < "b"', 'true == 1', '"a" == 1', '-"a"']:
            with self.subTest(expr=expr):
                self.reject(f'fn main() {{ println({expr}) }}', "expects|cannot compare")

    def test_void_canonical_and_unit_compatibility(self):
        _, signatures = compile_source('fn a() -> Unit { return }\nfn b() -> Void { return }\nfn main() -> Void { a(); b() }')
        self.assertEqual({s.return_type for s in signatures.values()}, {"Void"})

    def test_void_not_a_value_type(self):
        for source in ['fn f(n: Void) {}\nfn main() {}', 'fn main() { let n: Void = println(1) }', 'fn main() { var n = println(1) }', 'fn main() { println(println(1)) }']:
            with self.subTest(source=source):
                self.reject(source, "Void")

    def test_if_else_returns_and_nested_calls(self):
        self.assertEqual(self.run_source('''fn max(a: Int, b: Int) -> Int {
if a > b { return a } else { return b }
}
fn main() { println(max(10, 2)); println(max(1, 9)) }
'''), "10\n9\n")

    def test_early_return_skips_remaining_statements(self):
        self.assertEqual(self.run_source('fn f(n: Int) -> Int { if n > 0 { return n }; return 0 }\nfn main() { println(f(2)); println(f(-1)); return; println("bad") }'), "2\n0\n")

    def test_missing_branch_return(self):
        self.reject('fn f(n: Int) -> Int { if n > 0 { return n } }\nfn main() {}', "must return Int on all reachable paths")

    def test_nested_missing_return(self):
        self.reject('fn f(b: Bool) -> Int { if b { if b { return 1 } } else { return 2 } }\nfn main() {}', "must return Int")

    def test_wrong_and_missing_return_values(self):
        for signature, statement in [('Int', 'return "bad"'), ('Int', 'return'), ('Void', 'return 1')]:
            self.reject(f'fn f() -> {signature} {{ {statement} }}\nfn main() {{}}', "expected")

    def test_unreachable_code_is_still_typechecked(self):
        self.reject('fn main() { return; println(unknown) }', "unknown variable")
        self.reject('fn main() { if false { let n: Int = "bad" } }', "expected Int")

    def test_nested_control_flow_and_return(self):
        self.assertEqual(self.run_source('''fn f(b: Bool) -> Int {
if b { while true { if b { return 7 } else { return 8 } } }
else { return 9 }
}
fn main() { println(f(true)); println(f(false)) }'''), "7\n9\n")

    def test_while_mutation_and_fresh_iteration_scope(self):
        self.assertEqual(self.run_source('''fn main() {
var n = 0
while n < 3 {
let previous = n
n = n + 1
println(previous)
}
println(n)
}'''), "0\n1\n2\n3\n")

    def test_break_and_continue(self):
        self.assertEqual(self.run_source('''fn main() {
var n = 0
while true {
n = n + 1
if n == 2 { continue }
if n == 4 { break }
println(n)
}
println(n)
}'''), "1\n3\n4\n")

    def test_break_only_exits_innermost_loop(self):
        self.assertEqual(self.run_source('fn main() { var n = 0; while n < 2 { n = n + 1; while true { break }; println(n) } }'), "1\n2\n")

    def test_loop_controls_require_lexical_loop(self):
        for control in ["break", "continue"]:
            self.reject(f'fn main() {{ {control} }}', "only valid inside a loop")
            self.reject(f'fn f() {{ {control} }}\nfn main() {{ while true {{ f(); break }} }}', "only valid inside a loop")

    def test_return_paths_through_loops(self):
        for body in ['while false { return 1 }', 'while true { break }', 'while b { return 1 }', 'while true { if b { break }; return 1 }']:
            self.reject(f'fn f(b: Bool) -> Int {{ {body} }}\nfn main() {{}}', "must return Int")
        self.run_source('fn f() -> Int { while true { return 1 } }\nfn main() { println(f()) }')

    def test_infinite_loop_does_not_fall_through(self):
        compile_source('fn f() -> Int { while true { continue } }\nfn main() {}')
        # A break in a nested loop does not escape the outer infinite loop.
        compile_source('fn f() -> Int { while true { while true { break } } }\nfn main() {}')

    def test_syntax_error_has_exact_location(self):
        with self.assertRaises(Diagnostic) as caught:
            compile_source('fn main() {\n    if true println("bad")\n}', "syntax.tyv")
        error = caught.exception
        self.assertEqual(error.stage, "syntax")
        self.assertEqual((error.location.filename, error.location.line, error.location.column), ("syntax.tyv", 2, 13))

    def test_malformed_control_flow_and_assignment(self):
        for body in ['else {}', 'if true {', 'while true', 'var n =', 'var n = 0; n =', 'break 1', 'if true {} println(1)']:
            with self.subTest(body=body), self.assertRaises(Diagnostic) as caught:
                compile_source(f'fn main() {{ {body} }}', "syntax.tyv")
            self.assertEqual(caught.exception.stage, "syntax")

    def test_float_overflow_and_int_underflow(self):
        for expr, message in [('(' + '9' * 200 + '.0) * (' + '9' * 200 + '.0)', 'Float overflow'), ('-9223372036854775808 - 1', 'Int overflow')]:
            with self.subTest(expr=expr), self.assertRaisesRegex(Diagnostic, message):
                self.run_source(f'fn main() {{ println({expr}) }}')


if __name__ == "__main__":
    unittest.main()
