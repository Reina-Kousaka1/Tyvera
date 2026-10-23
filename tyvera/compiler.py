from __future__ import annotations

from .ast import Program
from .lexer import Lexer
from .parser import Parser
from .typesys import Signature, TypeChecker


def compile_source(source: str, filename: str = "<memory>") -> tuple[Program, dict[str, Signature]]:
    """Parse and statically check one Tyvera source file."""
    tokens = Lexer(source, filename).tokenize()
    program = Parser(tokens).parse()
    signatures = TypeChecker(program, filename).check()
    return program, signatures
