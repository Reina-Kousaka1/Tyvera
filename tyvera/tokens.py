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
    "return": "RETURN",
    "true": "TRUE",
    "false": "FALSE",
    "and": "AND",
    "or": "OR",
}

SINGLE_CHAR_TOKENS = {
    "(": "LPAREN",
    ")": "RPAREN",
    "{": "LBRACE",
    "}": "RBRACE",
    ",": "COMMA",
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
    "->": "ARROW",
    "==": "EQUAL_EQUAL",
    "!=": "BANG_EQUAL",
    "<=": "LESS_EQUAL",
    ">=": "GREATER_EQUAL",
}
