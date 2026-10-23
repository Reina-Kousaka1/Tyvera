from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Location:
    filename: str
    line: int
    column: int


class Diagnostic(Exception):
    def __init__(self, location: Location, stage: str, message: str) -> None:
        super().__init__(message)
        self.location = location
        self.stage = stage
        self.message = message

    def render(self) -> str:
        loc = self.location
        return f"{loc.filename}:{loc.line}:{loc.column}: {self.stage} error: {self.message}"
