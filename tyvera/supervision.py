"""Shared, host-independent supervision vocabulary for the reference runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RestartStrategy(Enum):
    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"
    REST_FOR_ONE = "rest_for_one"


class RestartPolicy(Enum):
    PERMANENT = "permanent"
    TRANSIENT = "transient"
    TEMPORARY = "temporary"


class CrashKind(Enum):
    NORMAL_STOP = "normal_stop"
    RUNTIME_ERROR = "runtime_error"
    TIMEOUT = "timeout"
    CHILD_FAILURE = "child_failure"
    NETWORK_FAILURE = "network_failure"
    SUPERVISOR_ESCALATION = "supervisor_escalation"
    CANCELLATION = "cancellation"


@dataclass(frozen=True, slots=True)
class CrashReason:
    kind: CrashKind
    message: str
    filename: str | None = None
    line: int | None = None
    column: int | None = None

    @classmethod
    def runtime_error(cls, message: str, filename: str, line: int, column: int) -> CrashReason:
        return cls(CrashKind.RUNTIME_ERROR, message, filename, line, column)

    @classmethod
    def normal_stop(cls) -> CrashReason:
        return cls(CrashKind.NORMAL_STOP, "child stopped normally")

    @classmethod
    def escalation(cls, message: str) -> CrashReason:
        return cls(CrashKind.SUPERVISOR_ESCALATION, message)

    def render(self) -> str:
        if self.filename is None:
            return f"{self.kind.value}: {self.message}"
        return f"{self.kind.value} at {self.filename}:{self.line}:{self.column}: {self.message}"


class RuntimeEventKind(Enum):
    ACTOR_STARTED = "actor_started"
    ACTOR_STOPPED = "actor_stopped"
    ACTOR_CRASHED = "actor_crashed"
    ACTOR_RESTARTED = "actor_restarted"
    SUPERVISOR_STARTED = "supervisor_started"
    SUPERVISOR_RESTARTED = "supervisor_restarted"
    SUPERVISOR_STOPPED = "supervisor_stopped"
    SUPERVISOR_ESCALATED = "supervisor_escalated"
    RUNTIME_SHUTDOWN = "runtime_shutdown"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    kind: RuntimeEventKind
    entity: str
    strategy: RestartStrategy | None = None
    restart_count: int | None = None
    reason: CrashReason | None = None


@dataclass(frozen=True, slots=True)
class ExponentialBackoff:
    base_milliseconds: int
    maximum_milliseconds: int

    def delay_for(self, restart_number: int) -> int:
        """Return capped deterministic exponential delay; restart_number is 1-based."""
        if restart_number <= 0:
            return 0
        delay = self.base_milliseconds
        for _ in range(restart_number - 1):
            if delay >= self.maximum_milliseconds - delay:
                return self.maximum_milliseconds
            delay *= 2
        return min(delay, self.maximum_milliseconds)
