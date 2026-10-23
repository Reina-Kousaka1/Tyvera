from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .compiler import compile_source
from .interpreter import Interpreter
from .source import Diagnostic, Location


def _read_source(filename: str) -> str:
    try:
        return Path(filename).read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise Diagnostic(Location(filename, 1, 1), "io", "source file is not valid UTF-8") from error
    except OSError as error:
        raise Diagnostic(Location(filename, 1, 1), "io", f"cannot read source file: {error.strerror or error}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tyvera", description="Run and check Tyvera source files.")
    parser.add_argument("--version", action="version", version="tyvera 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)
    run_command = commands.add_parser("run", help="type-check and run a .tyv file")
    run_command.add_argument("file", help="Tyvera source file (.tyv)")
    check_command = commands.add_parser("check", help="type-check a .tyv file without running it")
    check_command.add_argument("file", help="Tyvera source file (.tyv)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    filename = arguments.file
    try:
        source = _read_source(filename)
        program, _ = compile_source(source, filename)
        if arguments.command == "run":
            Interpreter(program, sys.stdout).run()
        else:
            print(f"checked {filename}")
    except Diagnostic as error:
        print(error.render(), file=sys.stderr)
        return 1
    return 0
