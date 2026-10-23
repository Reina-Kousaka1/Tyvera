from __future__ import annotations

from pathlib import Path

from .ast import Program
from .lexer import Lexer
from .parser import Parser
from .typesys import Signature, TypeChecker


def compile_source(source: str, filename: str = "<memory>") -> tuple[Program, dict[str, Signature]]:
    """Parse and statically check one Tyvera source file."""
    tokens = Lexer(source, filename).tokenize()
    program = Parser(tokens).parse()
    if program.imports:
        from .source import Diagnostic

        raise Diagnostic(program.imports[0].location, "module", "imports require project compilation")
    signatures = TypeChecker(program, filename).check()
    return program, signatures


def compile_project(entry_file: str | Path, project_root: str | Path) -> tuple[Program, dict[str, Signature]]:
    """Load, link, and type-check an entry file and all explicit imports."""
    from .modules import ModuleLoader

    entry = Path(entry_file)
    if not entry.is_absolute():
        entry = Path(project_root) / entry
    root = Path(project_root)
    program = ModuleLoader(root, entry).load()
    signatures = TypeChecker(program, str(entry)).check()
    return program, signatures
