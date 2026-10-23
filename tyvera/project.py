from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .source import Diagnostic, Location


_PROJECT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    name: str
    entry: Path
    root: Path


def read_manifest(root: Path) -> ProjectManifest:
    manifest_path = root / "tyvera.toml"
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise Diagnostic(Location(str(manifest_path), 1, 1), "project", "manifest is not valid UTF-8") from error
    except OSError as error:
        raise Diagnostic(Location(str(manifest_path), 1, 1), "project", f"cannot read manifest: {error.strerror or error}") from error

    values: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise Diagnostic(Location(str(manifest_path), number, 1), "project", "expected 'key = \"value\"' in manifest")
        key, raw_value = (part.strip() for part in line.split("=", 1))
        if key not in {"name", "entry"}:
            raise Diagnostic(Location(str(manifest_path), number, 1), "project", f"unknown manifest key '{key}'")
        if key in values:
            raise Diagnostic(Location(str(manifest_path), number, 1), "project", f"manifest key '{key}' is duplicated")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise Diagnostic(Location(str(manifest_path), number, 1), "project", "manifest values must be quoted strings") from error
        if not isinstance(value, str) or not value:
            raise Diagnostic(Location(str(manifest_path), number, 1), "project", f"manifest '{key}' must be a non-empty string")
        values[key] = value

    for required in ("name", "entry"):
        if required not in values:
            raise Diagnostic(Location(str(manifest_path), 1, 1), "project", f"manifest is missing required key '{required}'")
    if not _PROJECT_NAME.fullmatch(values["name"]):
        raise Diagnostic(Location(str(manifest_path), 1, 1), "project", "project name must be an identifier")
    relative_entry = Path(values["entry"])
    if relative_entry.is_absolute() or relative_entry.suffix != ".tyv":
        raise Diagnostic(Location(str(manifest_path), 1, 1), "project", "entry must be a project-relative .tyv path")
    resolved_root = root.resolve()
    entry = (resolved_root / relative_entry).resolve()
    try:
        entry.relative_to(resolved_root)
    except ValueError as error:
        raise Diagnostic(Location(str(manifest_path), 1, 1), "project", "entry must stay inside the project root") from error
    return ProjectManifest(values["name"], entry, resolved_root)


def find_manifest(start: Path) -> ProjectManifest | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for root in (current, *current.parents):
        path = root / "tyvera.toml"
        if path.is_file():
            return read_manifest(root)
    return None


def create_project(name: str, destination: Path) -> Path:
    if not _PROJECT_NAME.fullmatch(name):
        raise Diagnostic(Location(str(destination), 1, 1), "project", "project name must be an identifier")
    if destination.exists():
        raise Diagnostic(Location(str(destination), 1, 1), "project", "destination already exists; refusing to overwrite it")
    try:
        (destination / "src").mkdir(parents=True)
        (destination / "tyvera.toml").write_text(
            f'name = "{name}"\nentry = "src/main.tyv"\n', encoding="utf-8"
        )
        (destination / "src" / "main.tyv").write_text(
            'import "greeting.tyv" as greeting\n\n'
            'fn main() {\n'
            '    println(greeting.message())\n'
            '}\n',
            encoding="utf-8",
        )
        (destination / "src" / "greeting.tyv").write_text(
            'pub fn message() -> String {\n'
            '    return "Tyvera project ready!"\n'
            '}\n',
            encoding="utf-8",
        )
        (destination / ".gitignore").write_text(".tyvera.secrets\n", encoding="utf-8")
        (destination / ".tyvera.secrets.example").write_text(
            "# Copy to .tyvera.secrets and add local secret values.\n", encoding="utf-8"
        )
    except OSError as error:
        raise Diagnostic(Location(str(destination), 1, 1), "project", f"cannot create project: {error.strerror or error}") from error
    return destination
