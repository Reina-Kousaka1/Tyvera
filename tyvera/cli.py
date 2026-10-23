from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .compiler import compile_project
from .interpreter import Interpreter
from .project import create_project, find_manifest
from .source import Diagnostic, Location


def _read_source(filename: str) -> str:
    try:
        return Path(filename).read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise Diagnostic(Location(filename, 1, 1), "io", "source file is not valid UTF-8") from error
    except OSError as error:
        raise Diagnostic(Location(filename, 1, 1), "io", f"cannot read source file: {error.strerror or error}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tyvera", description="Check and run Tyvera projects and source files.")
    parser.add_argument("--version", action="version", version="tyvera 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)
    run_command = commands.add_parser("run", help="type-check and run a project or .tyv file")
    run_command.add_argument("path", nargs="?", default=".", help="project directory or Tyvera source file")
    check_command = commands.add_parser("check", help="type-check a project or .tyv file without running it")
    check_command.add_argument("path", nargs="?", default=".", help="project directory or Tyvera source file")
    new_command = commands.add_parser("new", help="create a small Tyvera project")
    new_command.add_argument("name", help="new project directory and project name")
    return parser


def _entry_and_root(path_argument: str) -> tuple[Path, Path, str]:
    candidate = Path(path_argument).resolve()
    if candidate.is_dir():
        manifest = find_manifest(candidate)
        if manifest is None:
            raise Diagnostic(Location(str(candidate), 1, 1), "project", "no tyvera.toml found in this directory or its parents")
        if not manifest.entry.is_file():
            raise Diagnostic(Location(str(manifest.entry), 1, 1), "project", "manifest entry file does not exist")
        return manifest.entry, manifest.root, manifest.entry.relative_to(manifest.root).as_posix()

    if candidate.suffix != ".tyv":
        raise Diagnostic(Location(str(candidate), 1, 1), "project", "expected a project directory or .tyv source file")
    if not candidate.is_file():
        raise Diagnostic(Location(str(candidate), 1, 1), "io", "cannot read source file: file does not exist")
    manifest = find_manifest(candidate.parent)
    root = manifest.root if manifest is not None else candidate.parent
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise Diagnostic(Location(str(candidate), 1, 1), "project", "source file must stay inside the project root") from error
    label = candidate.relative_to(Path.cwd().resolve()).as_posix() if candidate.is_relative_to(Path.cwd().resolve()) else candidate.name
    return candidate, root, label


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "new":
        destination = Path.cwd() / arguments.name
        try:
            create_project(arguments.name, destination)
        except Diagnostic as error:
            print(error.render(), file=sys.stderr)
            return 1
        print(f"created {arguments.name}")
        return 0

    try:
        entry, root, label = _entry_and_root(arguments.path)
        program, _ = compile_project(entry, root)
        if arguments.command == "run":
            Interpreter(program, sys.stdout).run()
        else:
            print(f"checked {label}")
    except Diagnostic as error:
        print(error.render(), file=sys.stderr)
        return 1
    return 0
