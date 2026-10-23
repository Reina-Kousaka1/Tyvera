from __future__ import annotations

import json
import os
import re
from pathlib import Path


_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SecretProviderError(Exception):
    """A secret-source error whose text is guaranteed not to contain values."""


class SecretProvider:
    """Resolve local project secrets first, then operating-system variables."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.local_file = self.project_root / ".tyvera.secrets"

    def get(self, name: str) -> str | None:
        if not _SECRET_NAME.fullmatch(name):
            raise SecretProviderError("secret name must use uppercase letters, digits, and underscores")
        local = self._read_local()
        if name in local:
            return local[name]
        return os.environ.get(name)

    def _read_local(self) -> dict[str, str]:
        try:
            content = self.local_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as error:
            raise SecretProviderError("local secret file is not valid UTF-8") from error
        except OSError as error:
            raise SecretProviderError("cannot read local secret file") from error

        values: dict[str, str] = {}
        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise SecretProviderError(f"invalid local secret file syntax on line {line_number}")
            key, raw_value = (part.strip() for part in line.split("=", 1))
            if not _SECRET_NAME.fullmatch(key):
                raise SecretProviderError(f"invalid secret name on line {line_number}")
            if key in values:
                raise SecretProviderError(f"duplicate secret name on line {line_number}")
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError as error:
                # The JSON parser's message can quote source text; keep it out of diagnostics.
                raise SecretProviderError(f"invalid quoted secret value on line {line_number}") from error
            if not isinstance(value, str):
                raise SecretProviderError(f"secret value on line {line_number} must be a quoted string")
            values[key] = value
        return values
