from __future__ import annotations

import math

from .source import Diagnostic, Location
from .tokens import KEYWORDS, SINGLE_CHAR_TOKENS, TWO_CHAR_TOKENS, Token


class Lexer:
    def __init__(self, source: str, filename: str = "<memory>") -> None:
        self.source = source
        self.filename = filename
        self.index = 0
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []

    def tokenize(self) -> list[Token]:
        while not self._at_end():
            char = self._peek()
            if char in " \t\r":
                self._advance()
            elif char == "\n":
                location = self._location()
                self._advance()
                self.tokens.append(Token("NEWLINE", "\\n", None, location))
            elif char == "/" and self._peek_next() == "/":
                self._skip_comment()
            elif (char.isascii() and char.isalpha()) or char == "_":
                self._identifier()
            elif char in "0123456789":
                self._number()
            elif char == '"':
                self._string()
            else:
                self._symbol()
        self.tokens.append(Token("EOF", "", None, self._location()))
        return self.tokens

    def _at_end(self) -> bool:
        return self.index >= len(self.source)

    def _peek(self) -> str:
        return "\0" if self._at_end() else self.source[self.index]

    def _peek_next(self) -> str:
        return "\0" if self.index + 1 >= len(self.source) else self.source[self.index + 1]

    def _location(self) -> Location:
        return Location(self.filename, self.line, self.column)

    def _advance(self) -> str:
        char = self.source[self.index]
        self.index += 1
        if char == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return char

    def _skip_comment(self) -> None:
        while not self._at_end() and self._peek() != "\n":
            self._advance()

    def _identifier(self) -> None:
        location = self._location()
        start = self.index
        while (self._peek().isascii() and self._peek().isalnum()) or self._peek() == "_":
            self._advance()
        lexeme = self.source[start:self.index]
        kind = KEYWORDS.get(lexeme, "IDENTIFIER")
        value = lexeme if kind == "IDENTIFIER" else None
        self.tokens.append(Token(kind, lexeme, value, location))

    def _number(self) -> None:
        location = self._location()
        start = self.index
        while self._peek() in "0123456789":
            self._advance()
        is_float = False
        if self._peek() == "." and self._peek_next() in "0123456789":
            is_float = True
            self._advance()
            while self._peek() in "0123456789":
                self._advance()
        lexeme = self.source[start:self.index]
        if is_float:
            value = float(lexeme)
        else:
            # Keep integer parsing independent of Python's configurable limit
            # for converting very long decimal strings. One extra magnitude
            # is retained for the unary spelling of Int's minimum value.
            digits = lexeme.lstrip("0") or "0"
            if len(digits) > 19 or (len(digits) == 19 and digits > "9223372036854775808"):
                value = (1 << 63) + 1
            else:
                value = int(digits)
        if is_float and not math.isfinite(value):
            raise Diagnostic(location, "lexical", "Float literal is outside the supported range")
        self.tokens.append(Token("FLOAT" if is_float else "INTEGER", lexeme, value, location))

    def _string(self) -> None:
        location = self._location()
        start = self.index
        self._advance()  # opening quote
        pieces: list[str] = []
        escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
        while not self._at_end() and self._peek() != '"':
            character_location = self._location()
            char = self._advance()
            if char == "\n":
                raise Diagnostic(location, "lexical", "string literal cannot cross a line")
            if 0xD800 <= ord(char) <= 0xDFFF:
                raise Diagnostic(character_location, "lexical", "string literal contains a Unicode surrogate, not a scalar value")
            if char == "\\":
                if self._at_end():
                    raise Diagnostic(location, "lexical", "unterminated escape in string literal")
                escape_location = self._location()
                escaped = self._advance()
                if escaped not in escapes:
                    raise Diagnostic(escape_location, "lexical", f"unsupported escape sequence \\{escaped}")
                pieces.append(escapes[escaped])
            else:
                pieces.append(char)
        if self._at_end():
            raise Diagnostic(location, "lexical", "unterminated string literal")
        self._advance()  # closing quote
        lexeme = self.source[start:self.index]
        self.tokens.append(Token("STRING", lexeme, "".join(pieces), location))

    def _symbol(self) -> None:
        location = self._location()
        two = self._peek() + self._peek_next()
        if two in TWO_CHAR_TOKENS:
            self._advance()
            self._advance()
            self.tokens.append(Token(TWO_CHAR_TOKENS[two], two, None, location))
            return
        char = self._advance()
        kind = SINGLE_CHAR_TOKENS.get(char)
        if kind is None:
            raise Diagnostic(location, "lexical", f"unexpected character {char!r}")
        self.tokens.append(Token(kind, char, None, location))
