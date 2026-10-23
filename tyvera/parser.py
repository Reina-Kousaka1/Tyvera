from __future__ import annotations

from .ast import (
    Binary,
    Call,
    Expression,
    ExpressionStatement,
    Function,
    LetStatement,
    Literal,
    Name,
    Parameter,
    Program,
    ReturnStatement,
    Statement,
    Unary,
)
from .source import Diagnostic
from .tokens import Token


_PRECEDENCE = {
    "OR": 1,
    "AND": 2,
    "EQUAL_EQUAL": 3,
    "BANG_EQUAL": 3,
    "LESS": 4,
    "LESS_EQUAL": 4,
    "GREATER": 4,
    "GREATER_EQUAL": 4,
    "PLUS": 5,
    "MINUS": 5,
    "STAR": 6,
    "SLASH": 6,
}
_OPERATOR = {
    "OR": "or",
    "AND": "and",
    "EQUAL_EQUAL": "==",
    "BANG_EQUAL": "!=",
    "LESS": "<",
    "LESS_EQUAL": "<=",
    "GREATER": ">",
    "GREATER_EQUAL": ">=",
    "PLUS": "+",
    "MINUS": "-",
    "STAR": "*",
    "SLASH": "/",
}


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def parse(self) -> Program:
        functions: list[Function] = []
        self._skip_separators()
        while not self._check("EOF"):
            if not self._check("FN"):
                self._error(self._current(), "expected a function declaration beginning with 'fn'")
            functions.append(self._function())
            if not self._check_any("NEWLINE", "SEMICOLON", "EOF"):
                self._error(self._current(), "expected a line break after the function body")
            self._skip_separators()
        return Program(functions)

    def _function(self) -> Function:
        fn_token = self._expect("FN", "expected 'fn'")
        name_token = self._expect("IDENTIFIER", "expected a function name after 'fn'")
        self._expect("LPAREN", "expected '(' after function name")
        parameters: list[Parameter] = []
        if not self._check("RPAREN"):
            while True:
                parameter_name = self._expect("IDENTIFIER", "expected a parameter name")
                self._expect("COLON", "expected ':' after parameter name")
                type_token = self._expect("IDENTIFIER", "expected a type name after ':'")
                parameters.append(Parameter(parameter_name.lexeme, type_token.lexeme, parameter_name.location))
                if not self._match("COMMA"):
                    break
                if self._check("RPAREN"):
                    break
        self._expect("RPAREN", "expected ')' after parameters")
        return_type = "Unit"
        if self._match("ARROW"):
            return_type = self._expect("IDENTIFIER", "expected a return type after '->'").lexeme
        body = self._block()
        return Function(name_token.lexeme, parameters, return_type, body, fn_token.location)

    def _block(self) -> list[Statement]:
        self._expect("LBRACE", "expected '{' to start the function body")
        statements: list[Statement] = []
        self._skip_separators()
        while not self._check_any("RBRACE", "EOF"):
            statements.append(self._statement())
            if not self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF"):
                self._error(self._current(), "expected a line break or ';' after the statement")
            self._skip_separators()
        self._expect("RBRACE", "expected '}' to close the function body")
        return statements

    def _statement(self) -> Statement:
        if self._match("LET"):
            return self._let_statement(self._previous())
        if self._match("RETURN"):
            return self._return_statement(self._previous())
        token = self._current()
        expression = self._expression()
        if not isinstance(expression, Call):
            self._error(token, "only a function call can be used as an expression statement")
        return ExpressionStatement(expression, token.location)

    def _let_statement(self, let_token: Token) -> LetStatement:
        name = self._expect("IDENTIFIER", "expected a variable name after 'let'")
        annotation: str | None = None
        if self._match("COLON"):
            annotation = self._expect("IDENTIFIER", "expected a type name after ':'").lexeme
        self._expect("EQUAL", "expected '=' in variable declaration")
        value = self._expression()
        return LetStatement(name.lexeme, annotation, value, let_token.location)

    def _return_statement(self, return_token: Token) -> ReturnStatement:
        if self._check_any("NEWLINE", "SEMICOLON", "RBRACE", "EOF"):
            return ReturnStatement(None, return_token.location)
        return ReturnStatement(self._expression(), return_token.location)

    def _expression(self, minimum_precedence: int = 0) -> Expression:
        expression = self._prefix()
        while True:
            if self._check("LPAREN") and isinstance(expression, Name):
                expression = self._call(expression)
                continue
            precedence = _PRECEDENCE.get(self._current().kind, -1)
            if precedence < minimum_precedence:
                break
            operator_token = self._advance()
            right = self._expression(precedence + 1)
            expression = Binary(
                expression,
                _OPERATOR[operator_token.kind],
                right,
                operator_token.location,
            )
        return expression

    def _prefix(self) -> Expression:
        token = self._advance()
        if token.kind == "INTEGER":
            return Literal(token.value, "Int", token.location)
        if token.kind == "FLOAT":
            return Literal(token.value, "Float", token.location)
        if token.kind == "STRING":
            return Literal(token.value, "String", token.location)
        if token.kind in {"TRUE", "FALSE"}:
            return Literal(token.kind == "TRUE", "Bool", token.location)
        if token.kind == "IDENTIFIER":
            return Name(token.lexeme, token.location)
        if token.kind in {"MINUS", "BANG"}:
            operator = "-" if token.kind == "MINUS" else "!"
            return Unary(operator, self._expression(7), token.location)
        if token.kind == "LPAREN":
            expression = self._expression()
            self._expect("RPAREN", "expected ')' after expression")
            return expression
        self._error(token, "expected an expression")

    def _call(self, callee: Name) -> Call:
        self._expect("LPAREN", "expected '(' after function name")
        arguments: list[Expression] = []
        if not self._check("RPAREN"):
            while True:
                arguments.append(self._expression())
                if not self._match("COMMA"):
                    break
                if self._check("RPAREN"):
                    break
        self._expect("RPAREN", "expected ')' after function arguments")
        return Call(callee.identifier, arguments, callee.location)

    def _skip_separators(self) -> None:
        while self._match("NEWLINE", "SEMICOLON"):
            pass

    def _check(self, kind: str) -> bool:
        return self._current().kind == kind

    def _check_any(self, *kinds: str) -> bool:
        return self._current().kind in kinds

    def _match(self, *kinds: str) -> bool:
        if self._check_any(*kinds):
            self._advance()
            return True
        return False

    def _expect(self, kind: str, message: str) -> Token:
        if self._check(kind):
            return self._advance()
        self._error(self._current(), message)

    def _advance(self) -> Token:
        token = self._current()
        if token.kind != "EOF":
            self.index += 1
        return token

    def _current(self) -> Token:
        return self.tokens[min(self.index, len(self.tokens) - 1)]

    def _previous(self) -> Token:
        return self.tokens[max(0, self.index - 1)]

    @staticmethod
    def _error(token: Token, message: str) -> None:
        raise Diagnostic(token.location, "syntax", message)
