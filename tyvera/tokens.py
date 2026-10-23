from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .source import Location


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    lexeme: str
    value: Any
    location: Location


KEYWORDS = {
    "fn": "FN",
    "let": "LET",
    "var": "VAR",
    "if": "IF",
    "else": "ELSE",
    "while": "WHILE",
    "for": "FOR",
    "in": "IN",
    "break": "BREAK",
    "continue": "CONTINUE",
    "return": "RETURN",
    "true": "TRUE",
    "false": "FALSE",
    "and": "AND",
    "or": "OR",
    "class": "CLASS",
    "interface": "INTERFACE",
    "implements": "IMPLEMENTS",
    "import": "IMPORT",
    "as": "AS",
    "pub": "PUB",
}

SINGLE_CHAR_TOKENS = {
    "(": "LPAREN",
    ")": "RPAREN",
    "{": "LBRACE",
    "}": "RBRACE",
    ",": "COMMA",
    ".": "DOT",
    ":": "COLON",
    ";": "SEMICOLON",
    "+": "PLUS",
    "-": "MINUS",
    "*": "STAR",
    "/": "SLASH",
    "!": "BANG",
    "=": "EQUAL",
    "<": "LESS",
    ">": "GREATER",
}

TWO_CHAR_TOKENS = {
    "&&": "AND",
    "||": "OR",
    "->": "ARROW",
    "==": "EQUAL_EQUAL",
    "!=": "BANG_EQUAL",
    "<=": "LESS_EQUAL",
    ">=": "GREATER_EQUAL",
}
