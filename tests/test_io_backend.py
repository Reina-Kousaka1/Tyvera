from __future__ import annotations

import selectors
import socket
import threading
import unittest

from tyvera.io_backend import IOBackend


class IOBackendTests(unittest.TestCase):
    def test_wait_dispatches_socket_readiness_callback(self) -> None:
        backend = IOBackend(worker_count=1, queue_capacity=1)
        reader, writer = socket.socketpair()
        completed: list[str] = []
        try:
            backend.watch(reader, selectors.EVENT_READ, lambda: completed.append("readable"))
            writer.send(b"ready")
            backend.wait(timeout=1)
            self.assertEqual(completed, ["readable"])
            self.assertEqual(reader.recv(5), b"ready")
        finally:
            backend.shutdown()
            reader.close()
            writer.close()

    def test_wakeup_interrupts_a_blocked_selector(self) -> None:
        backend = IOBackend(worker_count=1, queue_capacity=1)
        returned = threading.Event()

        def wait() -> None:
            try:
                backend.wait(timeout=5)
            finally:
                returned.set()

        waiter = threading.Thread(target=wait)
        try:
            waiter.start()
            backend.wake()
            self.assertTrue(returned.wait(timeout=1))
        finally:
            backend.shutdown()
            waiter.join(timeout=1)
        self.assertFalse(waiter.is_alive())


if __name__ == "__main__":
    unittest.main()
