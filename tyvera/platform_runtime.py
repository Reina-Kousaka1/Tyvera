"""Portable host services for the Tyvera bootstrap runtime.

This module is an implementation boundary, not a statement that host details
are part of Tyvera language semantics.  User-visible APIs must normalize
failures before exposing them to Tyvera programs.
"""

from __future__ import annotations

import os
import platform as host_platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from .source import Diagnostic, Location


class OperatingSystem(Enum):
    LINUX = "linux"
    WINDOWS = "windows"
    MACOS = "macos"
    OTHER = "other"


class Architecture(Enum):
    X86_64 = "x86_64"
    ARM64 = "aarch64"
    X86 = "x86"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class HostPlatform:
    operating_system: OperatingSystem
    architecture: Architecture

    @property
    def is_linux(self) -> bool:
        return self.operating_system is OperatingSystem.LINUX

    @property
    def linux_architecture_supported(self) -> bool:
        return self.is_linux and self.architecture in {Architecture.X86_64, Architecture.ARM64}


def detect_host_platform(system: str | None = None, machine: str | None = None) -> HostPlatform:
    """Return normalized OS/ISA names without depending on a distribution name."""
    system = sys.platform if system is None else system
    machine = host_platform.machine() if machine is None else machine
    if system.startswith("linux"):
        operating_system = OperatingSystem.LINUX
    elif system in {"win32", "cygwin"}:
        operating_system = OperatingSystem.WINDOWS
    elif system == "darwin":
        operating_system = OperatingSystem.MACOS
    else:
        operating_system = OperatingSystem.OTHER

    normalized_machine = machine.strip().lower().replace("-", "_")
    if normalized_machine in {"x86_64", "amd64", "x64"}:
        architecture = Architecture.X86_64
    elif normalized_machine in {"aarch64", "arm64"}:
        architecture = Architecture.ARM64
    elif normalized_machine in {"i386", "i486", "i586", "i686", "x86"}:
        architecture = Architecture.X86
    else:
        architecture = Architecture.OTHER
    return HostPlatform(operating_system, architecture)


def monotonic_seconds() -> float:
    """Monotonic elapsed time for deadlines; wall-clock time is never used."""
    return time.monotonic_ns() / 1_000_000_000


def monotonic_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def environment_value(name: str) -> str | None:
    """Read one process environment value without formatting it in an error."""
    if not _ENVIRONMENT_NAME.fullmatch(name):
        return None
    try:
        return os.environ.get(name)
    except (OSError, ValueError):
        return None


class ShutdownDiagnostic(Diagnostic):
    """A controlled signal shutdown retaining conventional process status."""

    def __init__(self, location: Location, signal_number: int, cleanup_message: str | None = None) -> None:
        self.signal_number = signal_number
        self.exit_code = 128 + signal_number
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = "signal"
        message = f"runtime shutdown requested by {signal_name}"
        if cleanup_message:
            message += f"; resource cleanup also failed: {cleanup_message}"
        super().__init__(location, "runtime", message)

    def with_cleanup_failure(self, message: str) -> ShutdownDiagnostic:
        return ShutdownDiagnostic(self.location, self.signal_number, message)


class ShutdownController:
    """Converts SIGINT/SIGTERM into a cooperative runtime request.

    The installed handler only records the first signal and nudges a runtime
    wakeup descriptor.  Interpreter code observes the request at safe points.
    """

    def __init__(self) -> None:
        self._signal_number: int | None = None
        self._waker: Callable[[], None] | None = None

    @property
    def signal_number(self) -> int | None:
        return self._signal_number

    def set_waker(self, waker: Callable[[], None]) -> None:
        self._waker = waker

    def request(self, signal_number: int) -> None:
        # Python dispatches process signal handlers on the main thread. Keep
        # this path lock-free so a signal cannot deadlock on a lock held by the
        # interrupted runtime turn.
        if self._signal_number is None:
            self._signal_number = signal_number
        if self._waker is not None:
            try:
                self._waker()
            except (OSError, RuntimeError):
                pass

    def raise_if_requested(self, location: Location) -> None:
        signal_number = self._signal_number
        if signal_number is not None:
            raise ShutdownDiagnostic(location, signal_number)

    @contextmanager
    def installed(self) -> Iterator[ShutdownController]:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("process signal handlers can only be installed on the main thread")
        selected = [getattr(signal, name) for name in ("SIGINT", "SIGTERM") if hasattr(signal, name)]
        previous = {number: signal.getsignal(number) for number in selected}

        def handler(number: int, _frame: object) -> None:
            self.request(number)

        try:
            for number in selected:
                signal.signal(number, handler)
            yield self
        finally:
            for number, old_handler in previous.items():
                signal.signal(number, old_handler)


class ChildProcessError(RuntimeError):
    """A normalized process error that does not include argv or environment."""


class ChildProcessTimeout(ChildProcessError):
    pass


@dataclass(frozen=True, slots=True)
class ChildProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


class ChildProcess:
    """Owned child with argv-based launch and bounded stdout/stderr capture."""

    def __init__(self, process: subprocess.Popen[bytes], output_limit: int) -> None:
        self._process = process
        self._output_limit = output_limit
        self._lock = threading.Lock()
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._stdout_truncated = False
        self._stderr_truncated = False
        self._readers = [
            threading.Thread(target=self._drain, args=(process.stdout, self._stdout, "stdout"), daemon=True),
            threading.Thread(target=self._drain, args=(process.stderr, self._stderr, "stderr"), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    @classmethod
    def start(
        cls,
        executable: str,
        arguments: Sequence[str] = (),
        *,
        cwd: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        output_limit: int = 1_048_576,
    ) -> ChildProcess:
        if not executable or "\0" in executable:
            raise ChildProcessError("child process could not be started")
        if not 0 <= output_limit <= 16_777_216:
            raise ChildProcessError("child output limit must be between 0 and 16777216 bytes")
        argv = [executable, *arguments]
        if any(not isinstance(item, str) or "\0" in item for item in argv):
            raise ChildProcessError("child process arguments are invalid")
        child_environment = dict(environment or {})
        if any(not isinstance(key, str) or not _ENVIRONMENT_NAME.fullmatch(key)
               or not isinstance(value, str) or "\0" in value for key, value in child_environment.items()):
            raise ChildProcessError("child process environment is invalid")

        resolved_executable = executable
        candidate = Path(executable)
        if not candidate.is_absolute() and len(candidate.parts) == 1:
            resolved_executable = shutil.which(executable, path=child_environment.get("PATH", "")) or executable
        try:
            process = subprocess.Popen(
                [resolved_executable, *arguments],
                executable=resolved_executable,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=None if cwd is None else str(cwd),
                env=child_environment,
                shell=False,
                close_fds=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise ChildProcessError("child process could not be started") from error
        return cls(process, output_limit)

    @property
    def is_running(self) -> bool:
        return self._process.poll() is None

    def wait(self, timeout: float | None = None) -> ChildProcessResult:
        if timeout is not None and timeout < 0:
            raise ChildProcessError("process wait timeout must be zero or greater")
        try:
            exit_code = self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise ChildProcessTimeout("child process did not exit before the deadline") from error
        self._join_readers()
        with self._lock:
            return ChildProcessResult(
                exit_code,
                bytes(self._stdout),
                bytes(self._stderr),
                self._stdout_truncated,
                self._stderr_truncated,
            )

    def terminate(self, grace_seconds: float = 1.0) -> ChildProcessResult:
        if grace_seconds < 0:
            raise ChildProcessError("termination grace period must be zero or greater")
        if self.is_running:
            try:
                self._process.terminate()
            except OSError as error:
                if self.is_running:
                    raise ChildProcessError("child process could not be terminated") from error
            try:
                return self.wait(grace_seconds)
            except ChildProcessTimeout:
                try:
                    self._process.kill()
                except OSError as error:
                    if self.is_running:
                        raise ChildProcessError("child process could not be stopped") from error
        return self.wait()

    def close(self) -> None:
        if self.is_running:
            self.terminate()
        else:
            self._join_readers()

    def __enter__(self) -> ChildProcess:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _join_readers(self) -> None:
        for reader in self._readers:
            reader.join()

    def _drain(self, stream, destination: bytearray, name: str) -> None:
        if stream is None:
            return
        truncated = False
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            with self._lock:
                remaining = self._output_limit - len(destination)
                if remaining > 0:
                    destination.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    truncated = True
        stream.close()
        if truncated:
            with self._lock:
                if name == "stdout":
                    self._stdout_truncated = True
                else:
                    self._stderr_truncated = True
