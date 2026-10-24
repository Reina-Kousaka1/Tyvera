from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .ast import Program
from .lexer import Lexer
from .parser import Parser
from .source import Location
from .typesys import Signature, TypeChecker


@dataclass(frozen=True, slots=True)
class CheckedProgram:
    """An AST plus source-located semantic metadata produced by type checking."""

    program: Program
    function_signatures: Mapping[str, Signature]
    expression_types: Mapping[Location, str] = field(default_factory=dict)
    generic_substitutions: Mapping[Location, Mapping[str, str]] = field(default_factory=dict)
    type_alias_targets: Mapping[str, str] = field(default_factory=dict)
    match_coverage: Mapping[Location, frozenset[str]] = field(default_factory=dict)
    match_exhaustiveness: Mapping[Location, bool] = field(default_factory=dict)


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
    checker = TypeChecker(program, filename)
    signatures = checker.check()
    return CheckedProgram(
        program, MappingProxyType(dict(signatures)), MappingProxyType(dict(checker.expression_types)),
        MappingProxyType({location: MappingProxyType(dict(arguments))
                          for location, arguments in checker.generic_substitutions.items()}),
        MappingProxyType({name: declaration.target_type for name, declaration in checker.type_aliases.items()}),
        MappingProxyType(dict(checker.match_coverage)), MappingProxyType(dict(checker.match_exhaustiveness)),
    )


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
    checker = TypeChecker(program, str(entry))
    signatures = checker.check()
    return CheckedProgram(
        program, MappingProxyType(dict(signatures)), MappingProxyType(dict(checker.expression_types)),
        MappingProxyType({location: MappingProxyType(dict(arguments))
                          for location, arguments in checker.generic_substitutions.items()}),
        MappingProxyType({name: declaration.target_type for name, declaration in checker.type_aliases.items()}),
        MappingProxyType(dict(checker.match_coverage)), MappingProxyType(dict(checker.match_exhaustiveness)),
    )

