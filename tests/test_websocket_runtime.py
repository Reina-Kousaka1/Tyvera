from __future__ import annotations

import io
import socket
import threading
import time
import unittest

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.websocket_protocol import make_accept_value


def read_exact(stream: socket.socket, length: int) -> bytes:
    value = bytearray()
    while len(value) < length:
        part = stream.recv(length - len(value))
        if not part:
            raise EOFError("WebSocket peer closed early")
        value.extend(part)
    return bytes(value)


def read_client_frame(stream: socket.socket) -> tuple[int, bytes]:
    first, second = read_exact(stream, 2)
    if not second & 0x80:
        raise AssertionError("client frames must be masked")
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(read_exact(stream, 2), "big")
    elif length == 127:
        length = int.from_bytes(read_exact(stream, 8), "big")
    mask = read_exact(stream, 4)
    payload = read_exact(stream, length)
    return first & 0x0F, bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))


def server_frame(opcode: int, payload: bytes) -> bytes:
    if len(payload) < 126:
        return bytes((0x80 | opcode, len(payload))) + payload
    if len(payload) <= 0xFFFF:
        return bytes((0x80 | opcode, 126)) + len(payload).to_bytes(2, "big") + payload
    raise AssertionError("test frames should stay below the 16-bit length marker")


def server_control_frame(opcode: int, payload: bytes = b"") -> bytes:
    if opcode not in {8, 9, 10} or len(payload) > 125:
        raise AssertionError("invalid test control frame")
    return bytes((0x80 | opcode, len(payload))) + payload


class WebSocketRuntimeTests(unittest.TestCase):
    def test_typed_client_handshake_send_ping_pong_messages_and_graceful_close(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        errors: list[BaseException] = []
        events: list[str] = []

        def serve() -> None:
            try:
                stream, _ = listener.accept()
                with stream:
                    stream.settimeout(5)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        chunk = stream.recv(4096)
                        if not chunk:
                            raise EOFError("client closed during Upgrade")
                        request.extend(chunk)
                    headers = {}
                    for line in bytes(request).split(b"\r\n")[1:]:
                        if not line:
                            break
                        name, _, value = line.partition(b":")
                        headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
                    self.assertEqual(headers.get("upgrade", "").lower(), "websocket")
                    key = headers.get("sec-websocket-key", "")
                    self.assertTrue(key)
                    stream.sendall(
                        ("HTTP/1.1 101 Switching Protocols\r\n"
                         "Connection: keep-alive, Upgrade\r\n"
                         "Upgrade: websocket\r\n"
                         f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode("ascii")
                    )
                    events.append("upgrade")
                    self.assertEqual(read_client_frame(stream), (1, b"identify"))
                    stream.sendall(server_control_frame(9, b"probe") + server_frame(1, b"hello") + server_frame(2, b"\x00\xff"))
                    events.append("ping-sent")
                    self.assertEqual(read_client_frame(stream), (10, b"probe"))
                    events.append("pong-received")
                    self.assertEqual(read_client_frame(stream), (8, (1000).to_bytes(2, "big")))
                    stream.sendall(server_control_frame(8, (1000).to_bytes(2, "big")))
                    events.append("close-received")
            except BaseException as error:
                errors.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        source = f'''async fn client() {{
    let opened = await webSocketConnect("ws://127.0.0.1:{port}/gateway?v=10&encoding=json", milliseconds(2000))
    match move opened {{
        Err(error) => println(error)
        Ok(connection) => {{
            let sent = await connection.sendText("identify")
            match sent {{
                Err(error) => println(error)
                Ok(count) => println(count)
            }}
            let first = await connection.receive()
            match first {{
                Err(error) => println(error)
                Ok(value) => match value {{
                    None => println("unexpected close")
                    Some(message) => {{
                        println(message.kind())
                        let text = message.text()
                        match text {{
                            Err(error) => println(error)
                            Ok(value) => println(value)
                        }}
                    }}
                }}
            }}
            let second = await connection.receive()
            match second {{
                Err(error) => println(error)
                Ok(value) => match value {{
                    None => println("unexpected close")
                    Some(message) => println(message.kind())
                }}
            }}
            let closed = await connection.closeGracefully()
            match closed {{
                Err(error) => println(error)
                Ok(_) => println("closed")
            }}
        }}
    }}
}}
fn main() {{ let task = client(); runTasks() }}'''
        try:
            program, _ = compile_source(source, "websocket-client.tyv")
            output = io.StringIO()
            Interpreter(program, output).run()
        finally:
            thread.join(6)
            if thread.is_alive():
                listener.close()
        self.assertFalse(thread.is_alive(), "local WebSocket peer did not finish")
        self.assertEqual(errors, [], f"events={events!r}; output={output.getvalue()!r}")
        self.assertEqual(events, ["upgrade", "ping-sent", "pong-received", "close-received"])
        self.assertEqual(output.getvalue(), "8\ntext\nhello\nbinary\nclosed\n")

    def test_invalid_accept_is_reported_as_result_and_socket_is_released(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        failures: list[BaseException] = []

        def reject() -> None:
            try:
                stream, _ = listener.accept()
                with stream:
                    stream.settimeout(4)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(stream.recv(4096))
                    stream.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                        b"Upgrade: websocket\r\nSec-WebSocket-Accept: invalid\r\n\r\n"
                    )
            except BaseException as error:
                failures.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=reject, daemon=True)
        thread.start()
        source = f'''async fn client() {{
    let opened = await webSocketConnect("ws://127.0.0.1:{port}/", milliseconds(2000))
    match move opened {{
        Err(error) => println(error)
        Ok(connection) => {{ connection.close(); println("unexpected success") }}
    }}
}}
fn main() {{ let task = client(); runTasks() }}'''
        try:
            program, _ = compile_source(source, "bad-websocket-accept.tyv")
            output = io.StringIO()
            Interpreter(program, output).run()
        finally:
            thread.join(5)
            if thread.is_alive():
                listener.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(output.getvalue(), "invalid WebSocket accept value\n")

    def test_receive_within_timeout_preserves_connection_for_heartbeat_loops(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                stream, _ = listener.accept()
                with stream:
                    stream.settimeout(4)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(stream.recv(4096))
                    headers = {}
                    for line in bytes(request).split(b"\r\n")[1:]:
                        if not line:
                            break
                        name, _, value = line.partition(b":")
                        headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
                    key = headers["sec-websocket-key"]
                    stream.sendall(
                        ("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                         "Upgrade: websocket\r\n"
                         f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode("ascii")
                    )
                    time.sleep(0.12)
                    stream.sendall(server_frame(1, b"heartbeat-window-survived"))
                    self.assertEqual(read_client_frame(stream), (8, (1000).to_bytes(2, "big")))
                    stream.sendall(server_control_frame(8, (1000).to_bytes(2, "big")))
            except BaseException as error:
                failures.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        source = f'''async fn client() {{
    let opened = await webSocketConnect("ws://127.0.0.1:{port}/", milliseconds(2000))
    match move opened {{
        Err(error) => println(error)
        Ok(connection) => {{
            let first = await connection.receiveWithin(milliseconds(20))
            match first {{
                Err(error) => println(error)
                Ok(_) => println("unexpected early frame")
            }}
            println(connection.isOpen())
            let second = await connection.receive()
            match second {{
                Err(error) => println(error)
                Ok(value) => match value {{
                    None => println("closed")
                    Some(message) => match message.text() {{
                        Err(error) => println(error)
                        Ok(text) => println(text)
                    }}
                }}
            }}
            let closed = await connection.closeGracefully()
            match closed {{
                Err(error) => println(error)
                Ok(_) => println("closed cleanly")
            }}
        }}
    }}
}}
fn main() {{ let task = client(); runTasks() }}'''
        try:
            program, _ = compile_source(source, "websocket-receive-timeout.tyv")
            output = io.StringIO()
            Interpreter(program, output).run()
        finally:
            thread.join(5)
            if thread.is_alive():
                listener.close()
        self.assertFalse(thread.is_alive(), "local WebSocket peer did not finish")
        self.assertEqual(failures, [])
        self.assertEqual(
            output.getvalue(),
            "WebSocket receive timed out\ntrue\nheartbeat-window-survived\nclosed cleanly\n",
        )

    def test_remote_close_is_acknowledged_and_reported_as_end_of_stream(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                stream, _ = listener.accept()
                with stream:
                    stream.settimeout(4)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(stream.recv(4096))
                    headers = {}
                    for line in bytes(request).split(b"\r\n")[1:]:
                        if not line:
                            break
                        name, _, value = line.partition(b":")
                        headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
                    key = headers["sec-websocket-key"]
                    stream.sendall(
                        ("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                         "Upgrade: websocket\r\n"
                         f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode("ascii")
                    )
                    stream.sendall(server_control_frame(8, (1001).to_bytes(2, "big") + b"going away"))
                    self.assertEqual(read_client_frame(stream), (8, (1001).to_bytes(2, "big") + b"going away"))
            except BaseException as error:
                failures.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        source = f'''async fn client() {{
    let opened = await webSocketConnect("ws://127.0.0.1:{port}/", milliseconds(2000))
    match move opened {{
        Err(error) => println(error)
        Ok(connection) => {{
            let received = await connection.receive()
            match received {{
                Err(error) => println(error)
                Ok(value) => match value {{
                    None => println("peer closed")
                    Some(_) => println("unexpected message")
                }}
            }}
            println(connection.isOpen())
        }}
    }}
}}
fn main() {{ let task = client(); runTasks() }}'''
        try:
            program, _ = compile_source(source, "websocket-remote-close.tyv")
            output = io.StringIO()
            Interpreter(program, output).run()
        finally:
            thread.join(5)
            if thread.is_alive():
                listener.close()
        self.assertFalse(thread.is_alive(), "local WebSocket peer did not finish")
        self.assertEqual(failures, [])
        self.assertEqual(output.getvalue(), "peer closed\nfalse\n")


if __name__ == "__main__":
    unittest.main()
