from __future__ import annotations

from pathlib import Path
from typing import Protocol, TextIO

from .compiler import CheckedProgram
from .interpreter import Interpreter


class ExecutionBackend(Protocol):
    """Execution boundary for a program that completed Tyvera semantic analysis."""

    name: str

    def execute(
        self,
        checked: CheckedProgram,
        *,
        output: TextIO,
        error_output: TextIO,
        project_root: Path,
    ) -> None: ...


class ReferenceInterpreterBackend:
    """The current Python-hosted reference implementation of Tyvera execution."""

    name = "reference-interpreter"

    def execute(
        self,
        checked: CheckedProgram,
        *,
        output: TextIO,
        error_output: TextIO,
        project_root: Path,
    ) -> None:
        Interpreter(
            checked.program,
            output,
            project_root=project_root,
            error_output=error_output,
        ).run()
