"""Platform adapter for non-blocking readiness and bounded worker operations.

The reference interpreter owns scheduling policy. This module only reports
readiness and worker completion; it does not define Tyvera language semantics.
"""

from __future__ import annotations

import selectors
import socket
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, TypeVar


T = TypeVar("T")


class IOBackendError(RuntimeError):
    """Controlled adapter-capacity or lifecycle rejection."""


@dataclass(slots=True)
class _SocketCallbacks:
    readable: Callable[[], None] | None = None
    writable: Callable[[], None] | None = None

    @property
    def events(self) -> int:
        return ((selectors.EVENT_READ if self.readable is not None else 0)
                | (selectors.EVENT_WRITE if self.writable is not None else 0))


class IOBackend:
    """Selector and worker adapter with an explicit wakeup path."""

    def __init__(self, worker_count: int = 4, queue_capacity: int = 64) -> None:
        self._selector = selectors.DefaultSelector()
        self._wake_reader, self._wake_writer = socket.socketpair()
        self._wake_reader.setblocking(False)
        self._wake_writer.setblocking(False)
        self._selector.register(self._wake_reader, selectors.EVENT_READ, None)
        self._executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tyvera-io")
        self._lock = threading.Lock()
        self._completions: deque[Callable[[], None]] = deque()
        self._watches: dict[socket.socket, _SocketCallbacks] = {}
        self._worker_count = 0
        self._worker_slots = threading.BoundedSemaphore(worker_count + queue_capacity)
        self._closed = False

    @property
    def has_pending(self) -> bool:
        return bool(self._watches) or self._worker_count > 0 or bool(self._completions)

    def submit(self, operation: Callable[[], T], completion: Callable[[T | None, BaseException | None], None]) -> Future[T]:
        if self._closed:
            raise IOBackendError("I/O runtime is closed")
        if not self._worker_slots.acquire(blocking=False):
            raise IOBackendError("I/O worker capacity is full")
        with self._lock:
            self._worker_count += 1
        try:
            future = self._executor.submit(operation)
        except RuntimeError as error:
            with self._lock:
                self._worker_count -= 1
            self._worker_slots.release()
            raise IOBackendError("I/O runtime is closed") from error

        def finished(done: Future[T]) -> None:
            try:
                value, error = done.result(), None
            except BaseException as caught:  # converted at the Tyvera boundary
                value, error = None, caught

            def deliver() -> None:
                with self._lock:
                    self._worker_count -= 1
                self._worker_slots.release()
                completion(value, error)

            self._enqueue(deliver)

        future.add_done_callback(finished)
        return future

    def watch(self, stream: socket.socket, event: int, callback: Callable[[], None]) -> None:
        if self._closed:
            raise RuntimeError("I/O runtime is closed")
        callbacks = self._watches.get(stream)
        if callbacks is None:
            callbacks = _SocketCallbacks()
            self._watches[stream] = callbacks
        if event == selectors.EVENT_READ:
            if callbacks.readable is not None:
                raise RuntimeError("a read operation is already pending for this connection")
            callbacks.readable = callback
        elif event == selectors.EVENT_WRITE:
            if callbacks.writable is not None:
                raise RuntimeError("a write operation is already pending for this connection")
            callbacks.writable = callback
        else:
            raise ValueError("unsupported readiness event")
        self._update_watch(stream, callbacks)

    def unwatch(self, stream: socket.socket, event: int, callback: Callable[[], None] | None = None) -> None:
        callbacks = self._watches.get(stream)
        if callbacks is None:
            return
        if event == selectors.EVENT_READ and (callback is None or callbacks.readable is callback):
            callbacks.readable = None
        elif event == selectors.EVENT_WRITE and (callback is None or callbacks.writable is callback):
            callbacks.writable = None
        self._update_watch(stream, callbacks)

    def wait(self, timeout: float | None) -> None:
        self._drain_completions()
        for key, mask in self._selector.select(timeout):
            if key.fileobj is self._wake_reader:
                self._drain_wakeup()
                continue
            stream = key.fileobj
            callbacks = self._watches.get(stream)
            if callbacks is None:
                continue
            ready: list[Callable[[], None]] = []
            if mask & selectors.EVENT_READ and callbacks.readable is not None:
                ready.append(callbacks.readable)
                callbacks.readable = None
            if mask & selectors.EVENT_WRITE and callbacks.writable is not None:
                ready.append(callbacks.writable)
                callbacks.writable = None
            self._update_watch(stream, callbacks)
            for callback in ready:
                callback()
        self._drain_completions()

    def wake(self) -> None:
        """Wake a blocked selector so it can observe runtime shutdown."""
        if self._closed:
            return
        try:
            self._wake_writer.send(b"\0")
        except (BlockingIOError, OSError):
            pass

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for stream in tuple(self._watches):
            self._remove_watch(stream)
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._drain_completions()
        self._selector.unregister(self._wake_reader)
        self._selector.close()
        self._wake_reader.close()
        self._wake_writer.close()

    def _update_watch(self, stream: socket.socket, callbacks: _SocketCallbacks) -> None:
        events = callbacks.events
        if not events:
            self._remove_watch(stream)
            return
        try:
            self._selector.modify(stream, events, callbacks)
        except KeyError:
            self._selector.register(stream, events, callbacks)
        except (OSError, ValueError):
            self._remove_watch(stream)
            raise

    def _remove_watch(self, stream: socket.socket) -> None:
        self._watches.pop(stream, None)
        try:
            self._selector.unregister(stream)
        except (KeyError, OSError, ValueError):
            pass

    def _enqueue(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._completions.append(callback)
        try:
            self._wake_writer.send(b"\0")
        except (BlockingIOError, OSError):
            pass

    def _drain_wakeup(self) -> None:
        while True:
            try:
                if not self._wake_reader.recv(4096):
                    return
            except (BlockingIOError, OSError):
                return

    def _drain_completions(self) -> None:
        while True:
            with self._lock:
                if not self._completions:
                    return
                callback = self._completions.popleft()
            callback()
