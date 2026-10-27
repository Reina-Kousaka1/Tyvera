from __future__ import annotations

import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tyvera.compiler import compile_checked_project
from tyvera.backend import ReferenceInterpreterBackend
from tyvera.http_protocol import HttpUrl
from tyvera.interpreter import Interpreter
from tyvera.websocket_protocol import make_accept_value


ROOT = Path(__file__).resolve().parents[1]
DISCORD_ROOT = ROOT / "libraries" / "discord"


def read_exact(stream: ssl.SSLSocket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        part = stream.recv(length - len(result))
        if not part:
            raise EOFError("fake Gateway peer closed early")
        result.extend(part)
    return bytes(result)


def read_client_frame(stream: ssl.SSLSocket) -> tuple[int, bytes]:
    first, second = read_exact(stream, 2)
    if not second & 0x80:
        raise AssertionError("WebSocket client frames must be masked")
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(read_exact(stream, 2), "big")
    elif length == 127:
        length = int.from_bytes(read_exact(stream, 8), "big")
    mask = read_exact(stream, 4)
    payload = read_exact(stream, length)
    return first & 0x0F, bytes(value ^ mask[index & 3] for index, value in enumerate(payload))


def server_frame(payload: bytes) -> bytes:
    if len(payload) < 126:
        header = bytes((0x81, len(payload)))
    elif len(payload) <= 0xFFFF:
        header = bytes((0x81, 126)) + len(payload).to_bytes(2, "big")
    else:
        raise AssertionError("test Gateway payload exceeds the 16-bit length form")
    return header + payload


def server_close_frame() -> bytes:
    payload = (1000).to_bytes(2, "big")
    return bytes((0x88, len(payload))) + payload


class DiscordGatewayIntegrationTests(unittest.TestCase):
    def test_local_verified_wss_identify_heartbeat_ack_and_ready(self) -> None:
        openssl = shutil.which("openssl")
        if openssl is None:
            git_openssl = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"
            openssl = str(git_openssl) if git_openssl.is_file() else None
        if openssl is None:
            self.skipTest("OpenSSL is required to create a temporary fake Gateway certificate")

        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-gateway-", dir=DISCORD_ROOT) as temporary:
            directory = Path(temporary)
            certificate = directory / "fake-gateway.crt"
            private_key = directory / "fake-gateway.key"
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(private_key),
                 "-out", str(certificate), "-days", "2", "-subj", "/CN=gateway.discord.gg",
                 "-addext", "subjectAltName=DNS:gateway.discord.gg"],
                check=True, capture_output=True, text=True,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(certificate), str(private_key))

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            port = listener.getsockname()[1]
            captured: dict[str, object] = {}
            failures: list[BaseException] = []

            def serve() -> None:
                raw = None
                secure = None
                try:
                    raw, _ = listener.accept()
                    raw.settimeout(5)
                    secure = server_context.wrap_socket(raw, server_side=True)
                    secure.settimeout(5)
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(secure.recv(4096))
                    header_lines = bytes(request).split(b"\r\n")
                    headers: dict[str, str] = {}
                    for line in header_lines[1:]:
                        if not line:
                            break
                        name, _, value = line.partition(b":")
                        headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
                    captured["request_target"] = header_lines[0]
                    captured["authorization_header"] = headers.get("authorization")
                    key = headers["sec-websocket-key"]
                    secure.sendall(
                        ("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                         "Upgrade: websocket\r\n"
                         f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode("ascii")
                    )
                    secure.sendall(server_frame(b'{"op":10,"d":{"heartbeat_interval":150}}'))
                    opcode, identify_payload = read_client_frame(secure)
                    captured["identify_opcode"] = opcode
                    identify = json.loads(identify_payload.decode("utf-8"))
                    captured["identify"] = identify
                    heartbeat_opcode, heartbeat_payload = read_client_frame(secure)
                    captured["heartbeat_opcode"] = heartbeat_opcode
                    captured["heartbeat"] = json.loads(heartbeat_payload.decode("utf-8"))
                    ready = (b'{"op":0,"s":41,"t":"READY","d":{"session_id":"fake-session",'
                             b'"resume_gateway_url":"wss://gateway.example.invalid"}}')
                    secure.sendall(
                        server_frame(b'{"op":11,"d":null}') + server_frame(ready)
                    )
                    close_opcode, close_payload = read_client_frame(secure)
                    captured["close"] = (close_opcode, close_payload)
                    secure.sendall(server_close_frame())
                except BaseException as error:
                    failures.append(error)
                finally:
                    try:
                        if secure is not None:
                            secure.close()
                        elif raw is not None:
                            raw.close()
                    except OSError:
                        pass
                    listener.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            entry = directory / "gateway-test.tyv"
            entry.write_text(
                f'''import "../src/client.tyv" as discord
async fn run() {{
    match environment("TYVERA_FAKE_DISCORD_TOKEN") {{
        None => eprintln("missing fake test token")
        Some(token) => {{
            let intents = discord.defaultIntents()
            intents.append(discord.GatewayIntent.MessageContent)
            intents.append(discord.GatewayIntent.GuildMessages)
            let result = await discord.runGateway(token, intents, 1, seconds(3))
    match result {{
        Err(error) => eprintln(error)
        Ok(summary) => {{
            println(summary.ready)
            println(summary.sessionId == Some("fake-session"))
            println(summary.resumeGatewayUrl == Some("wss://gateway.example.invalid"))
            println(summary.lastSequence == Some(41))
            println(summary.dispatchCount)
            println(summary.messageCount)
            println(summary.heartbeatCount)
        }}
    }}
        }}
    }}
}}
fn main() {{ let task = run(); runTasks() }}
''',
                encoding="utf-8",
            )
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output = io.StringIO()
            errors = io.StringIO()
            default_context = ssl.create_default_context
            original_websocket_task = Interpreter._websocket_connect_task
            original_getaddrinfo = socket.getaddrinfo

            def route_gateway(self, address, timeout, location):
                if address.startswith("wss://gateway.discord.gg/"):
                    address = address.replace(
                        "wss://gateway.discord.gg/", f"wss://gateway.discord.gg:{port}/", 1,
                    )
                return original_websocket_task(self, address, timeout, location)

            def local_dns(host, selected_port, *args, **kwargs):
                if host == "gateway.discord.gg":
                    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", selected_port))]
                return original_getaddrinfo(host, selected_port, *args, **kwargs)

            def trusted_context(*args, **kwargs):
                context = default_context(*args, **kwargs)
                context.load_verify_locations(cafile=str(certificate))
                return context

            try:
                with patch.object(Interpreter, "_websocket_connect_task", route_gateway), \
                        patch("tyvera.interpreter.socket.getaddrinfo", side_effect=local_dns), \
                        patch("tyvera.interpreter.ssl.create_default_context", side_effect=trusted_context), \
                        patch.dict(os.environ, {"TYVERA_FAKE_DISCORD_TOKEN": "fake-gateway-token"}):
                    ReferenceInterpreterBackend().execute(
                        checked, output=output, error_output=errors, project_root=DISCORD_ROOT,
                    )
            finally:
                thread.join(7)
                if thread.is_alive():
                    listener.close()

            self.assertFalse(thread.is_alive(), "fake Discord Gateway did not finish")
            self.assertEqual(failures, [])
            self.assertEqual(output.getvalue(), "true\ntrue\ntrue\ntrue\n1\n0\n1\n")
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(captured.get("authorization_header"), None)
            self.assertEqual(captured.get("identify_opcode"), 1)
            self.assertEqual(captured.get("identify", {}).get("op"), 2)
            self.assertEqual(captured.get("identify", {}).get("d", {}).get("token"), "fake-gateway-token")
            self.assertEqual(captured.get("identify", {}).get("d", {}).get("intents"), 33281)
            self.assertEqual(captured.get("heartbeat_opcode"), 1)
            self.assertEqual(captured.get("heartbeat"), {"op": 1, "d": None})
            self.assertEqual(captured.get("close", (None,))[0], 8)
            rendered = output.getvalue() + errors.getvalue()
            self.assertNotIn("fake-gateway-token", rendered)

    def test_local_verified_rest_transport_maps_rate_limits_without_retries(self) -> None:
        openssl = shutil.which("openssl")
        if openssl is None:
            git_openssl = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"
            openssl = str(git_openssl) if git_openssl.is_file() else None
        if openssl is None:
            self.skipTest("OpenSSL is required to create a temporary fake REST certificate")

        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-rest-", dir=DISCORD_ROOT) as temporary:
            directory = Path(temporary)
            certificate = directory / "discord.crt"
            private_key = directory / "discord.key"
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(private_key),
                 "-out", str(certificate), "-days", "2", "-subj", "/CN=discord.com",
                 "-addext", "subjectAltName=DNS:discord.com"],
                check=True, capture_output=True, text=True,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(certificate), str(private_key))
            captured: list[tuple[str, str | None, bytes]] = []

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, format: str, *args: object) -> None:
                    return

                def _reply(self, status: int, body: bytes, headers: dict[str, str]) -> None:
                    captured.append((self.path, self.headers.get("Authorization"), self.rfile.read(
                        int(self.headers.get("Content-Length", "0"))
                    )))
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True

                def do_GET(self) -> None:
                    if self.path == "/api/v10/users/@me":
                        self._reply(200, b'{"id":"fake-user","username":"test-bot"}', {
                            "Content-Type": "application/json",
                            "X-RateLimit-Bucket": "local-bucket",
                            "X-RateLimit-Remaining": "4",
                            "X-RateLimit-Reset-After": "1.25",
                        })
                    else:
                        self._reply(404, b'{"message":"unknown endpoint"}', {"Content-Type": "application/json"})

                def do_POST(self) -> None:
                    self._reply(429, b'{"message":"slow down","retry_after":0.5,"global":true}', {
                        "Content-Type": "application/json",
                        "X-RateLimit-Bucket": "local-bucket",
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset-After": "0.5",
                        "Retry-After": "0.5",
                        "X-RateLimit-Global": "true",
                    })

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            local_port = server.server_address[1]
            server.socket = server_context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            entry = directory / "rest-test.tyv"
            entry.write_text(r'''import "../src/rest.tyv" as rest
async fn run() {
    match environment("TYVERA_FAKE_DISCORD_REST_TOKEN") {
        None => println("missing token")
        Some(token) => {
            let lookup = await rest.requestBot(token, "GET", "users/@me", bytesFromString(""), seconds(3))
            match lookup {
                Err(_) => println("lookup failed")
                Ok(response) => {
                    println(response.status)
                    println(response.rateLimitBucket == Some("local-bucket"))
                    println(response.rateLimitRemaining == Some("4"))
                    println(response.rateLimitResetAfter == Some("1.25"))
                    match response.body.toString() {
                        Err(_) => println("invalid body")
                        Ok(text) => println(text)
                    }
                }
            }
            let sent = await rest.requestBot(token, "POST", "channels/1/messages", bytesFromString("{\"content\":\"hello\"}"), seconds(3))
            match sent {
                Err(_) => println("post failed")
                Ok(response) => {
                    println(response.status)
                    println(response.rateLimitBucket == Some("local-bucket"))
                    println(response.rateLimitRemaining == Some("0"))
                    println(response.globalRateLimit)
                    match rest.decodeResponseFailure(response) {
                        Err(_) => println("invalid failure")
                        Ok(value) => match value {
                            None => println("missing failure")
                            Some(failure) => {
                                println(failure.status)
                                println(failure.retryAfterSeconds == Some(0.5))
                                println(failure.global)
                            }
                        }
                    }
                }
            }
        }
    }
}
fn main() { let task = run(); runTasks() }
''', encoding="utf-8")
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output = io.StringIO()
            errors = io.StringIO()
            original_http_task = Interpreter._http_client_task
            original_getaddrinfo = socket.getaddrinfo

            def route_https(self, url, request, timeout, location):
                if url.host == "discord.com" and url.port == 443:
                    url = HttpUrl(url.scheme, url.host, local_port, url.target)
                return original_http_task(self, url, request, timeout, location)

            def local_dns(host, port, *args, **kwargs):
                if host == "discord.com":
                    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
                return original_getaddrinfo(host, port, *args, **kwargs)

            default_context = ssl.create_default_context

            def trusted_context(*args, **kwargs):
                context = default_context(*args, **kwargs)
                context.load_verify_locations(cafile=str(certificate))
                return context

            try:
                with patch.object(Interpreter, "_http_client_task", route_https), \
                        patch("tyvera.interpreter.socket.getaddrinfo", side_effect=local_dns), \
                        patch("tyvera.interpreter.ssl.create_default_context", side_effect=trusted_context), \
                        patch.dict(os.environ, {"TYVERA_FAKE_DISCORD_REST_TOKEN": "fake-rest-token"}):
                    ReferenceInterpreterBackend().execute(
                        checked, output=output, error_output=errors, project_root=DISCORD_ROOT,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

            self.assertFalse(thread.is_alive(), "fake Discord REST server did not stop")
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(output.getvalue(),
                             '200\ntrue\ntrue\ntrue\n{"id":"fake-user","username":"test-bot"}\n'
                             '429\ntrue\ntrue\ntrue\n429\ntrue\ntrue\n')
            self.assertEqual([path for path, _, _ in captured], [
                "/api/v10/users/@me", "/api/v10/channels/1/messages",
            ])
            self.assertTrue(all(auth == "Bot fake-rest-token" for _, auth, _ in captured))
            self.assertEqual(captured[0][2], b"")
            self.assertEqual(captured[1][2], b'{"content":"hello"}')
            self.assertNotIn("fake-rest-token", output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()
