from __future__ import annotations

from pathlib import Path
from typing import Protocol, TextIO

from .compiler import CheckedProgram
from .interpreter import Interpreter
from .platform_runtime import ShutdownController


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
        shutdown_controller: ShutdownController | None = None,
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
        shutdown_controller: ShutdownController | None = None,
    ) -> None:
        Interpreter(
            checked.program,
            output,
            project_root=project_root,
            error_output=error_output,
            shutdown_controller=shutdown_controller,
        ).run()
