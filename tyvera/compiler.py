from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .ast import Program
from .lexer import Lexer
from .parser import Parser
from .typesys import Signature, TypeChecker


@dataclass(frozen=True, slots=True)
class CheckedProgram:
    """An AST that completed semantic analysis and its top-level function signatures."""

    program: Program
    function_signatures: Mapping[str, Signature]


def compile_source(source: str, filename: str = "<memory>") -> tuple[Program, dict[str, Signature]]:
    """Parse and statically check one Tyvera source file."""
    checked = compile_checked_source(source, filename)
    return checked.program, dict(checked.function_signatures)


def compile_checked_source(source: str, filename: str = "<memory>") -> CheckedProgram:
    """Return the checked AST at the compiler-to-backend boundary."""
    tokens = Lexer(source, filename).tokenize()
    program = Parser(tokens).parse()
    if program.imports:
        from .source import Diagnostic

        raise Diagnostic(program.imports[0].location, "module", "imports require project compilation")
    signatures = TypeChecker(program, filename).check()
    return CheckedProgram(program, signatures)


def compile_project(entry_file: str | Path, project_root: str | Path) -> tuple[Program, dict[str, Signature]]:
    """Load, link, and type-check an entry file and all explicit imports."""
    checked = compile_checked_project(entry_file, project_root)
    return checked.program, dict(checked.function_signatures)


def compile_checked_project(entry_file: str | Path, project_root: str | Path) -> CheckedProgram:
    """Load, link, and type-check a project before selecting an execution backend."""
    from .modules import ModuleLoader

    entry = Path(entry_file)
    if not entry.is_absolute():
        entry = Path(project_root) / entry
    root = Path(project_root)
    program = ModuleLoader(root, entry).load()
    signatures = TypeChecker(program, str(entry)).check()
    return CheckedProgram(program, signatures)

