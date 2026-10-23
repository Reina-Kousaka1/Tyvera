from __future__ import annotations

import unittest

from tyvera.ast import Binary, Function, Program
from tyvera.lexer import Lexer
from tyvera.parser import Parser
from tyvera.source import Diagnostic


class LexerTests(unittest.TestCase):
    def test_tokens_comments_and_locations(self) -> None:
        tokens = Lexer('fn main() {\n  // comment\n  println("hi")\n}', "sample.tyv").tokenize()
        identifiers = [token for token in tokens if token.kind == "IDENTIFIER"]
        self.assertEqual([token.lexeme for token in identifiers], ["main", "println"])
        self.assertEqual(identifiers[0].location.line, 1)
        self.assertEqual(identifiers[1].location.line, 3)
        self.assertTrue(any(token.kind == "NEWLINE" for token in tokens))

    def test_unterminated_string_has_source_location(self) -> None:
        with self.assertRaises(Diagnostic) as caught:
            Lexer('fn main() { println("oops) }', "broken.tyv").tokenize()
        self.assertEqual(caught.exception.stage, "lexical")
        self.assertEqual(caught.exception.location.filename, "broken.tyv")
        self.assertEqual(caught.exception.location.line, 1)

    def test_unsupported_escape_is_diagnosed(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "unsupported escape sequence"):
            Lexer('"\\q"').tokenize()

    def test_non_ascii_identifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(Diagnostic, "unexpected character"):
            Lexer("fn café() {}", "ascii.tyv").tokenize()


class ParserTests(unittest.TestCase):
    def test_parses_function_and_operator_precedence(self) -> None:
        program = Parser(Lexer("fn main() { let answer = 2 + 3 * 4 } ").tokenize()).parse()
        self.assertIsInstance(program, Program)
        self.assertEqual(len(program.functions), 1)
        self.assertIsInstance(program.functions[0], Function)
        expression = program.functions[0].body[0].value
        self.assertIsInstance(expression, Binary)
        self.assertEqual(expression.operator, "+")
        self.assertEqual(expression.right.operator, "*")

    def test_missing_closing_parenthesis_is_a_syntax_error(self) -> None:
        source = 'fn main() {\n    println("hello"\n}\n'
        with self.assertRaises(Diagnostic) as caught:
            Parser(Lexer(source, "broken.tyv").tokenize()).parse()
        self.assertEqual(caught.exception.stage, "syntax")
        self.assertEqual(caught.exception.location.filename, "broken.tyv")
        self.assertIn("expected ')'", caught.exception.message)

    def test_unseparated_statements_are_rejected(self) -> None:
        source = 'fn main() { println("one") println("two") }'
        with self.assertRaisesRegex(Diagnostic, "line break or ';'"):
            Parser(Lexer(source).tokenize()).parse()
