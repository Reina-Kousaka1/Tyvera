from __future__ import annotations

import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tyvera.compiler import compile_checked_project
from tyvera.backend import ReferenceInterpreterBackend
from tyvera.cpp_backend import CppNativeBackend
from tyvera.http_protocol import HttpUrl
from tyvera.interpreter import Interpreter
from tyvera.lowering import lower_checked_program
from tyvera.websocket_protocol import make_accept_value


ROOT = Path(__file__).resolve().parents[1]
DISCORD_ROOT = ROOT / "libraries" / "discord"


def _native_cpp_available() -> bool:
    return sys.platform.startswith("linux") and any(
        shutil.which(command) for command in ("clang++", "g++", "c++")
    ) and shutil.which("cc") is not None


def _native_local_route_override(directory: Path) -> Path:
    """Build a test-only DNS route shim for native fake Discord services."""
    source = directory / "discord_local_route.c"
    library = directory / "discord_local_route.so"
    source.write_text(r'''#define _GNU_SOURCE
#include <dlfcn.h>
#include <netdb.h>
#include <stdlib.h>
#include <string.h>

typedef int (*getaddrinfo_fn)(const char *, const char *,
                              const struct addrinfo *, struct addrinfo **);

int getaddrinfo(const char *node, const char *service,
                const struct addrinfo *hints, struct addrinfo **result) {
    static getaddrinfo_fn real_getaddrinfo = NULL;
    if (real_getaddrinfo == NULL) {
        real_getaddrinfo = (getaddrinfo_fn)dlsym(RTLD_NEXT, "getaddrinfo");
    }
    const char *port = getenv("TYVERA_TEST_DISCORD_PORT");
    if (node != NULL && port != NULL &&
        (strcmp(node, "discord.com") == 0 ||
         strcmp(node, "gateway.discord.gg") == 0 ||
         strcmp(node, "gateway-us-east1-b.discord.gg") == 0)) {
        return real_getaddrinfo("127.0.0.1", port, hints, result);
    }
    return real_getaddrinfo(node, service, hints, result);
}
''', encoding="utf-8")
    compiler = shutil.which("cc")
    if compiler is None:
        raise unittest.SkipTest("a C compiler is required for native Discord route tests")
    subprocess.run(
        [compiler, "-shared", "-fPIC", str(source), "-o", str(library), "-ldl"],
        check=True, capture_output=True, text=True,
    )
    return library


def _native_discord_run(
    checked, output: Path, directory: Path, port: int, certificate: Path,
    runtime_environment: dict[str, str],
):
    native_executable = directory / (output.name + "-native")
    CppNativeBackend().build(
        lower_checked_program(checked), native_executable, project_root=DISCORD_ROOT,
    )
    route_library = _native_local_route_override(directory)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.pop("DISCORD_TOKEN", None)
    environment["TYVERA_TEST_DISCORD_PORT"] = str(port)
    environment["SSL_CERT_FILE"] = str(certificate)
    environment["LD_PRELOAD"] = str(route_library)
    environment.update(runtime_environment)
    return subprocess.run(
        [str(native_executable)], cwd=DISCORD_ROOT, capture_output=True,
        text=True, encoding="utf-8", env=environment, check=False, timeout=60,
    )


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


def server_close_frame(code: int = 1000, reason: str = "") -> bytes:
    payload = code.to_bytes(2, "big") + reason.encode("utf-8")
    if len(payload) > 125:
        raise AssertionError("test close payload exceeds the WebSocket control-frame limit")
    return bytes((0x88, len(payload))) + payload


class DiscordGatewayIntegrationTests(unittest.TestCase):
    def test_close_code_policy_and_resume_url_validation(self) -> None:
        native_available = _native_cpp_available()
        native_result = None
        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-policy-", dir=DISCORD_ROOT) as temporary:
            entry = Path(temporary) / "policy.tyv"
            entry.write_text(r'''import "../src/gateway.tyv" as gateway
async fn typedConnectShape(url: WebSocketUrl) {
    let pending = webSocketConnect(url, seconds(1))
}
fn main() {
    println(gateway.closeCodeAction(Some(4000), true, false) == gateway.GatewayRecoveryAction.Resume)
    println(gateway.closeCodeAction(Some(4003), true, false) == gateway.GatewayRecoveryAction.Resume)
    println(gateway.closeCodeAction(Some(4005), true, false) == gateway.GatewayRecoveryAction.Resume)
    println(gateway.closeCodeAction(Some(4007), true, false) == gateway.GatewayRecoveryAction.Identify)
    println(gateway.closeCodeAction(Some(4004), true, false) == gateway.GatewayRecoveryAction.Fatal)
    println(gateway.closeCodeAction(Some(1000), true, false) == gateway.GatewayRecoveryAction.Stop)
    println(gateway.closeCodeAction(None<Int>(), true, true) == gateway.GatewayRecoveryAction.Stop)
    println(gateway.invalidSessionAction(true, true) == gateway.GatewayRecoveryAction.Resume)
    println(gateway.invalidSessionAction(false, true) == gateway.GatewayRecoveryAction.Identify)
    println(gateway.reconnectRequestAction(false) == gateway.GatewayRecoveryAction.Identify)
    match webSocketUrl("wss://gateway-us-east1-b.discord.gg") {
        Err(_) => println("parse failed")
        Ok(url) => println(url.scheme() == "wss" && url.port() == 443 && url.hostEndsWith(".discord.gg"))
    }
    match webSocketUrl("wss://gateway.discord.gg.attacker.invalid") {
        Err(_) => println("malformed")
        Ok(url) => println(url.hostEndsWith(".discord.gg"))
    }
    match webSocketUrl("wss://user@gateway.discord.gg") {
        Err(_) => println("userinfo rejected")
        Ok(_) => println("userinfo accepted")
    }
}
''', encoding="utf-8")
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output = io.StringIO()
            errors = io.StringIO()
            ReferenceInterpreterBackend().execute(checked, output=output, error_output=errors,
                                                  project_root=DISCORD_ROOT)
            if native_available:
                executable = Path(temporary) / "policy-native"
                CppNativeBackend().build(lower_checked_program(checked), executable, project_root=DISCORD_ROOT)
                environment = os.environ.copy()
                environment.pop("PYTHONPATH", None)
                environment.pop("PYTHONHOME", None)
                environment.pop("DISCORD_TOKEN", None)
                native_result = subprocess.run(
                    [str(executable)], cwd=DISCORD_ROOT, capture_output=True, text=True,
                    encoding="utf-8", env=environment, check=False, timeout=60,
                )
        self.assertEqual(errors.getvalue(), "")
        expected_output = "true\ntrue\ntrue\ntrue\ntrue\ntrue\ntrue\ntrue\ntrue\ntrue\ntrue\nfalse\nuserinfo rejected\n"
        self.assertEqual(output.getvalue(), expected_output)
        if native_result is not None:
            self.assertEqual(
                (native_result.returncode, native_result.stdout, native_result.stderr),
                (0, expected_output, ""),
            )

    def test_local_verified_wss_identify_heartbeat_ack_and_ready(self) -> None:
        native_available = _native_cpp_available()
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
            connection_count = 2 if native_available else 1
            listener.listen(connection_count)
            listener.settimeout(60)
            port = listener.getsockname()[1]
            captured: list[dict[str, object]] = []
            failures: list[BaseException] = []

            def serve() -> None:
                for _ in range(connection_count):
                    raw = None
                    secure = None
                    session: dict[str, object] = {}
                    try:
                        raw, _ = listener.accept()
                        raw.settimeout(10)
                        secure = server_context.wrap_socket(raw, server_side=True)
                        secure.settimeout(10)
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
                        session["request_target"] = header_lines[0]
                        session["authorization_header"] = headers.get("authorization")
                        key = headers["sec-websocket-key"]
                        secure.sendall(
                            ("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                             "Upgrade: websocket\r\n"
                             f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode("ascii")
                        )
                        secure.sendall(server_frame(b'{"op":10,"d":{"heartbeat_interval":150}}'))
                        opcode, identify_payload = read_client_frame(secure)
                        session["identify_opcode"] = opcode
                        identify = json.loads(identify_payload.decode("utf-8"))
                        session["identify"] = identify
                        heartbeat_opcode, heartbeat_payload = read_client_frame(secure)
                        session["heartbeat_opcode"] = heartbeat_opcode
                        session["heartbeat"] = json.loads(heartbeat_payload.decode("utf-8"))
                        ready = (b'{"op":0,"s":41,"t":"READY","d":{"session_id":"fake-session",'
                                 b'"resume_gateway_url":"wss://gateway.example.invalid"}}')
                        secure.sendall(
                            server_frame(b'{"op":11,"d":null}') + server_frame(ready)
                        )
                        close_opcode, close_payload = read_client_frame(secure)
                        session["close"] = (close_opcode, close_payload)
                        secure.sendall(server_close_frame())
                        captured.append(session)
                    except BaseException as error:
                        failures.append(RuntimeError(
                            f"fake Discord peer failed after {len(captured)} captured interactions: "
                            f"{type(error).__name__}: {error}"
                        ))
                        break
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
async fn runGateway(token: Secret<String>, intents: List<discord.GatewayIntent>, reply: ReplySender<Int>) {{
    let result = await discord.runGateway(token, intents, 1, seconds(3))
    match result {{
        Err(error) => {{
            eprintln(error)
            reply.send(-1)
        }}
        Ok(summary) => {{
            println(summary.ready)
            println(summary.sessionId == Some("fake-session"))
            println(summary.resumeGatewayUrl == Some("wss://gateway.example.invalid"))
            println(summary.lastSequence == Some(41))
            println(summary.dispatchCount)
            println(summary.messageCount)
            println(summary.heartbeatCount)
            println(summary.events.length)
            reply.send(summary.dispatchCount)
        }}
    }}
}}
actor GatewayRunner {{
    on run(reply: ReplySender<Int>) {{
        match environment("TYVERA_FAKE_DISCORD_TOKEN") {{
            None => {{
                eprintln("missing fake test token")
                reply.send(-1)
            }}
            Some(token) => {{
                let intents = discord.defaultIntents()
                intents.append(discord.GatewayIntent.MessageContent)
                intents.append(discord.GatewayIntent.GuildMessages)
                let task = runGateway(token, intents, reply)
            }}
        }}
    }}
}}
async fn run() {{
    let supervisor = oneForOne(1, 1000)
    let worker = supervisor.start(GatewayRunner(), transientChildPolicy())
    let pending = worker.request(run)
    let dispatchCount: Int = await pending
    worker.stop()
}}
fn main() {{
    let task = run()
    match runTasks() {{
        Ok(_) => {{}}
        Err(error) => eprintln("runTasks failed: " + error)
    }}
}}''',
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
                native_result = None
                if native_available:
                    native_result = _native_discord_run(
                        checked, directory / "gateway", directory, port, certificate,
                        {"TYVERA_FAKE_DISCORD_TOKEN": "fake-gateway-token"},
                    )
            finally:
                thread.join(30)
                if thread.is_alive():
                    listener.close()

            self.assertFalse(thread.is_alive(), "fake Discord Gateway did not finish")
            if native_result is not None:
                native_stdout = native_result.stdout
                native_stderr = native_result.stderr
                for fake_token in ("fake-gateway-token", "fake-resume-token", "fake-heartbeat-token", "fake-rest-token"):
                    native_stdout = native_stdout.replace(fake_token, "<redacted>")
                    native_stderr = native_stderr.replace(fake_token, "<redacted>")
                self.assertEqual(
                    (native_result.returncode, native_result.stdout == output.getvalue(),
                     native_result.stderr == errors.getvalue()),
                    (0, True, True),
                    f"native Discord output mismatch: stdout={native_stdout!r}, stderr={native_stderr!r}, peer_failures={failures!r}",
                )
            self.assertEqual(failures, [])
            self.assertEqual(output.getvalue(), "true\ntrue\ntrue\ntrue\n1\n0\n1\n1\n")
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(len(captured), connection_count)
            for session in captured:
                self.assertEqual(session.get("authorization_header"), None)
                self.assertEqual(session.get("identify_opcode"), 1)
                self.assertEqual(session.get("identify", {}).get("op"), 2)
                self.assertEqual(session.get("identify", {}).get("d", {}).get("token"), "fake-gateway-token")
                self.assertEqual(session.get("identify", {}).get("d", {}).get("intents"), 33281)
                self.assertEqual(session.get("heartbeat_opcode"), 1)
                self.assertEqual(session.get("heartbeat"), {"op": 1, "d": None})
                self.assertEqual(session.get("close", (None,))[0], 8)
            rendered = output.getvalue() + errors.getvalue()
            self.assertNotIn("fake-gateway-token", rendered)
            if native_result is not None:
                self.assertEqual((native_result.returncode, native_result.stdout, native_result.stderr),
                                 (0, output.getvalue(), errors.getvalue()))
                self.assertNotIn("fake-gateway-token", native_result.stdout + native_result.stderr)

    def test_gateway_summary_exposes_peer_close_reason_with_native_parity(self) -> None:
        native_available = _native_cpp_available()
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("OpenSSL is required to create a temporary fake Gateway certificate")

        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-close-reason-", dir=DISCORD_ROOT) as temporary:
            directory = Path(temporary)
            certificate = directory / "gateway.crt"
            private_key = directory / "gateway.key"
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
            connection_count = 2 if native_available else 1
            listener.listen(connection_count)
            listener.settimeout(60)
            port = listener.getsockname()[1]
            failures: list[BaseException] = []

            def serve() -> None:
                try:
                    for _ in range(connection_count):
                        raw, _ = listener.accept()
                        raw.settimeout(10)
                        secure = server_context.wrap_socket(raw, server_side=True)
                        secure.settimeout(10)
                        request = bytearray()
                        while b"\r\n\r\n" not in request:
                            chunk = secure.recv(4096)
                            if not chunk:
                                raise RuntimeError("client closed before the WebSocket handshake")
                            request.extend(chunk)
                        headers: dict[str, str] = {}
                        for line in bytes(request).split(b"\r\n")[1:]:
                            if not line:
                                break
                            name, _, value = line.partition(b":")
                            headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
                        key = headers["sec-websocket-key"]
                        secure.sendall(
                            ("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                             "Upgrade: websocket\r\n"
                             f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode("ascii")
                        )
                        secure.sendall(server_close_frame(1000, "maintenance complete"))
                        secure.close()
                except BaseException as error:
                    failures.append(RuntimeError(
                        f"fake close-reason peer failed: {type(error).__name__}: {error}"
                    ))
                finally:
                    listener.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            entry = directory / "close-reason.tyv"
            entry.write_text(r'''import "../src/client.tyv" as discord
async fn run() {
    match environment("TYVERA_FAKE_DISCORD_TOKEN") {
        None => println("missing fake token")
        Some(token) => {
            let result = await discord.runGatewayReliable(token, discord.defaultIntents(), 1, seconds(3), 0, 0, 0)
            match result {
                Err(_) => println("gateway failed")
                Ok(summary) => {
                    println(summary.ready)
                    println(summary.closeCode == Some(1000))
                    match summary.closeReason {
                        Some(reason) => println(reason)
                        None => println("missing close reason")
                    }
                }
            }
        }
    }
}
fn main() {
    let task = run()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln("runTasks failed: " + error)
    }
}
''', encoding="utf-8")
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output = io.StringIO()
            errors = io.StringIO()
            original_connect = Interpreter._websocket_connect_task
            original_getaddrinfo = socket.getaddrinfo
            default_context = ssl.create_default_context

            def route_gateway(self, address, timeout, location):
                prefix = "wss://gateway.discord.gg/"
                if address.startswith(prefix):
                    address = address.replace(prefix, f"wss://gateway.discord.gg:{port}/", 1)
                return original_connect(self, address, timeout, location)

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
                        patch.dict(os.environ, {"TYVERA_FAKE_DISCORD_TOKEN": "fake-close-reason-token"}):
                    ReferenceInterpreterBackend().execute(
                        checked, output=output, error_output=errors, project_root=DISCORD_ROOT,
                    )
                reference_result = (0, output.getvalue(), errors.getvalue())
                native_result = None
                if native_available:
                    native_result = _native_discord_run(
                        checked, directory / "gateway-close-reason", directory, port, certificate,
                        {"TYVERA_FAKE_DISCORD_TOKEN": "fake-close-reason-token"},
                    )
            finally:
                listener.close()
                thread.join(30)

            self.assertFalse(thread.is_alive(), "fake close-reason Gateway server did not stop")
            self.assertEqual(failures, [])
            expected = "false\ntrue\nmaintenance complete\n"
            self.assertEqual((0, output.getvalue(), errors.getvalue()), (0, expected, ""))
            if native_result is not None:
                self.assertEqual(
                    (native_result.returncode, native_result.stdout, native_result.stderr),
                    reference_result,
                )

    def test_local_gateway_reconnect_resumes_then_reidentifies_invalid_session(self) -> None:
        native_available = _native_cpp_available()
        openssl = shutil.which("openssl")
        if openssl is None:
            git_openssl = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"
            openssl = str(git_openssl) if git_openssl.is_file() else None
        if openssl is None:
            self.skipTest("OpenSSL is required to create a temporary fake Gateway certificate")

        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-resume-", dir=DISCORD_ROOT) as temporary:
            directory = Path(temporary)
            certificate = directory / "gateway.crt"
            private_key = directory / "gateway.key"
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(private_key),
                 "-out", str(certificate), "-days", "2", "-subj", "/CN=gateway.discord.gg",
                 "-addext", "subjectAltName=DNS:gateway.discord.gg,DNS:gateway-us-east1-b.discord.gg"],
                check=True, capture_output=True, text=True,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(certificate), str(private_key))
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            connection_count = 2 if native_available else 1
            listener.listen(4 * connection_count)
            listener.settimeout(60)
            port = listener.getsockname()[1]
            captured: list[dict[str, object]] = []
            attempts: list[int] = []
            failures: list[BaseException] = []

            def serve() -> None:
                try:
                    for _ in range(connection_count):
                        for index in range(4):
                            raw, _ = listener.accept()
                            attempts.append(index)
                            raw.settimeout(10)
                            secure = server_context.wrap_socket(raw, server_side=True)
                            secure.settimeout(10)
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
                            key = headers["sec-websocket-key"]
                            secure.sendall(
                                ("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                                 "Upgrade: websocket\r\n"
                                 f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode("ascii")
                            )
                            if index == 0:
                                secure.sendall(server_close_frame(1001))
                                secure.close()
                                continue
                            session_index = index - 1
                            secure.sendall(server_frame(b'{"op":10,"d":{"heartbeat_interval":5000}}'))
                            opcode, payload = read_client_frame(secure)
                            captured.append({"opcode": opcode, "payload": json.loads(payload.decode("utf-8"))})
                            if session_index == 0:
                                secure.sendall(server_frame(
                                    b'{"op":0,"s":41,"t":"READY","d":{"session_id":"resume-session",'
                                    b'"resume_gateway_url":"wss://gateway-us-east1-b.discord.gg"}}'
                                ))
                                secure.sendall(server_frame(b'{"op":7,"d":null}'))
                            elif session_index == 1:
                                secure.sendall(server_frame(b'{"op":9,"d":false}'))
                            else:
                                secure.sendall(server_frame(
                                    b'{"op":0,"s":42,"t":"READY","d":{"session_id":"new-session",'
                                    b'"resume_gateway_url":"wss://gateway.discord.gg"}}'
                                ))
                                close_opcode, close_payload = read_client_frame(secure)
                                captured.append({"close": (close_opcode, close_payload)})
                                secure.sendall(server_close_frame())
                            secure.close()
                except BaseException as error:
                    failures.append(RuntimeError(
                        f"fake Discord peer failed after {len(captured)} captured interactions: "
                        f"{type(error).__name__}: {error}"
                    ))
                finally:
                    listener.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            entry = directory / "resume-test.tyv"
            entry.write_text(r'''import "../src/client.tyv" as discord
import "../src/gateway.tyv" as gateway
async fn run() {
    match environment("TYVERA_FAKE_DISCORD_TOKEN") {
        None => eprintln("missing fake token")
        Some(token) => {
            let result = await discord.runGatewayReliable(token, discord.defaultIntents(), 2, seconds(4), 3, 0, 0)
            match result {
                Err(error) => eprintln(error)
                Ok(summary) => {
                    println(summary.ready)
                    println(summary.sessionId == Some("new-session"))
                    println(summary.lastSequence == Some(42))
                    println(summary.dispatchCount)
                    println(summary.events.length)
                    match summary.events.get(0) {
                        None => println("first event missing")
                        Some(event) => match event {
                            gateway.GatewayDispatch.Ready(session) => println(session)
                            _ => println("first event was not READY")
                        }
                    }
                    println(summary.recoveryAction == gateway.GatewayRecoveryAction.Stop)
                }
            }
        }
    }
}
fn main() {
    let task = run()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln("runTasks failed: " + error)
    }
}
''', encoding="utf-8")
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output = io.StringIO()
            errors = io.StringIO()
            original_connect = Interpreter._websocket_connect_task
            original_getaddrinfo = socket.getaddrinfo
            default_context = ssl.create_default_context

            def route_gateway(self, address, timeout, location):
                for host in ("gateway.discord.gg", "gateway-us-east1-b.discord.gg"):
                    prefix = f"wss://{host}/"
                    if address.startswith(prefix):
                        address = address.replace(prefix, f"wss://{host}:{port}/", 1)
                        break
                return original_connect(self, address, timeout, location)

            def local_dns(host, selected_port, *args, **kwargs):
                if host in {"gateway.discord.gg", "gateway-us-east1-b.discord.gg"}:
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
                        patch.dict(os.environ, {"TYVERA_FAKE_DISCORD_TOKEN": "fake-resume-token"}):
                    ReferenceInterpreterBackend().execute(
                        checked, output=output, error_output=errors, project_root=DISCORD_ROOT,
                    )
                reference_result = (0, output.getvalue(), errors.getvalue())
                native_result = None
                if native_available:
                    native_result = _native_discord_run(
                        checked, directory / "gateway-resume", directory, port, certificate,
                        {"TYVERA_FAKE_DISCORD_TOKEN": "fake-resume-token"},
                    )
            finally:
                thread.join(45)
                if thread.is_alive():
                    listener.close()

        self.assertFalse(thread.is_alive(), "fake Gateway resume server did not finish")
        if native_result is not None:
            native_stdout = native_result.stdout
            native_stderr = native_result.stderr
            for fake_token in ("fake-gateway-token", "fake-resume-token", "fake-heartbeat-token", "fake-rest-token"):
                native_stdout = native_stdout.replace(fake_token, "<redacted>")
                native_stderr = native_stderr.replace(fake_token, "<redacted>")
            self.assertEqual(
                (native_result.returncode, native_result.stdout == output.getvalue(),
                 native_result.stderr == errors.getvalue()),
                (0, True, True),
                f"native Discord output mismatch: stdout={native_stdout!r}, stderr={native_stderr!r}, peer_failures={failures!r}",
            )
        self.assertEqual(failures, [])
        self.assertEqual(attempts, [0, 1, 2, 3] * connection_count)
        self.assertEqual(output.getvalue(), "true\ntrue\ntrue\n2\n2\nresume-session\ntrue\n")
        self.assertEqual(errors.getvalue(), "")
        per_run = 4
        self.assertEqual(len(captured), per_run * connection_count)
        self.assertEqual(captured[0]["opcode"], 1)
        self.assertEqual(captured[0]["payload"]["op"], 2)
        self.assertEqual(captured[1]["opcode"], 1)
        self.assertEqual(captured[1]["payload"]["op"], 6)
        self.assertEqual(captured[1]["payload"]["d"], {
            "token": "fake-resume-token", "session_id": "resume-session", "seq": 41,
        })
        self.assertEqual(captured[2]["opcode"], 1)
        self.assertEqual(captured[2]["payload"]["op"], 2)
        self.assertIn("close", captured[3])
        self.assertNotIn("fake-resume-token", output.getvalue() + errors.getvalue())
        if native_result is not None:
            self.assertEqual(
                (native_result.returncode, native_result.stdout, native_result.stderr),
                reference_result,
            )
            self.assertEqual(captured[per_run:], captured[:per_run])
            self.assertNotIn("fake-resume-token", native_result.stdout + native_result.stderr)

    def test_missing_heartbeat_ack_terminates_connection_and_respects_retry_budget(self) -> None:
        native_available = _native_cpp_available()
        openssl = shutil.which("openssl")
        if openssl is None:
            git_openssl = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"
            openssl = str(git_openssl) if git_openssl.is_file() else None
        if openssl is None:
            self.skipTest("OpenSSL is required to create a temporary fake Gateway certificate")

        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-heartbeat-", dir=DISCORD_ROOT) as temporary:
            directory = Path(temporary)
            certificate = directory / "gateway.crt"
            private_key = directory / "gateway.key"
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
            connection_count = 2 if native_available else 1
            listener.listen(connection_count)
            listener.settimeout(60)
            port = listener.getsockname()[1]
            captured: list[dict[str, object]] = []
            failures: list[BaseException] = []

            def serve() -> None:
                for _ in range(connection_count):
                    raw = None
                    secure = None
                    try:
                        raw, _ = listener.accept()
                        raw.settimeout(15)
                        secure = server_context.wrap_socket(raw, server_side=True)
                        secure.settimeout(15)
                        request = bytearray()
                        while b"\r\n\r\n" not in request:
                            request.extend(secure.recv(4096))
                        headers: dict[str, str] = {}
                        for line in bytes(request).split(b"\r\n")[1:]:
                            if not line:
                                break
                            name, _, value = line.partition(b":")
                            headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
                        key = headers["sec-websocket-key"]
                        secure.sendall(
                            ("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
                             "Upgrade: websocket\r\n"
                             f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode("ascii")
                        )
                        secure.sendall(server_frame(b'{"op":10,"d":{"heartbeat_interval":500}}'))
                        opcode, payload = read_client_frame(secure)
                        captured.append({"opcode": opcode, "payload": json.loads(payload.decode("utf-8"))})
                        opcode, payload = read_client_frame(secure)
                        captured.append({"opcode": opcode, "payload": json.loads(payload.decode("utf-8"))})
                        try:
                            while secure.recv(4096):
                                pass
                        except (OSError, ssl.SSLError):
                            pass
                    except BaseException as error:
                        failures.append(RuntimeError(
                            f"fake Discord peer failed after {len(captured)} captured interactions: "
                            f"{type(error).__name__}: {error}"
                        ))
                        break
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
            entry = directory / "missing-ack.tyv"
            entry.write_text(r'''import "../src/client.tyv" as discord
async fn run() {
    match environment("TYVERA_FAKE_DISCORD_TOKEN") {
        None => println("missing fake token")
        Some(token) => {
            let result = await discord.runGatewayReliable(token, discord.defaultIntents(), 1, seconds(3), 0, 0, 0)
            match result {
                Err(error) => eprintln(error)
                Ok(_) => println("unexpected success")
            }
        }
    }
}
fn main() {
    let task = run()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln("runTasks failed: " + error)
    }
}
''', encoding="utf-8")
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output = io.StringIO()
            errors = io.StringIO()
            native_result = None
            original_connect = Interpreter._websocket_connect_task
            original_getaddrinfo = socket.getaddrinfo
            default_context = ssl.create_default_context

            def route_gateway(self, address, timeout, location):
                prefix = "wss://gateway.discord.gg/"
                if address.startswith(prefix):
                    address = address.replace(prefix, f"wss://gateway.discord.gg:{port}/", 1)
                return original_connect(self, address, timeout, location)

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
                        patch.dict(os.environ, {"TYVERA_FAKE_DISCORD_TOKEN": "fake-heartbeat-token"}):
                    ReferenceInterpreterBackend().execute(
                        checked, output=output, error_output=errors, project_root=DISCORD_ROOT,
                    )
                if native_available:
                    native_result = _native_discord_run(
                        checked, directory / "missing-ack", directory, port, certificate,
                        {"TYVERA_FAKE_DISCORD_TOKEN": "fake-heartbeat-token"},
                    )
            finally:
                thread.join(30)
                if thread.is_alive():
                    listener.close()

        self.assertFalse(thread.is_alive(), "fake Gateway heartbeat peer did not observe connection cleanup")
        if native_result is not None:
            native_stdout = native_result.stdout
            native_stderr = native_result.stderr
            for fake_token in ("fake-gateway-token", "fake-resume-token", "fake-heartbeat-token", "fake-rest-token"):
                native_stdout = native_stdout.replace(fake_token, "<redacted>")
                native_stderr = native_stderr.replace(fake_token, "<redacted>")
            self.assertEqual(
                (native_result.returncode, native_result.stdout == output.getvalue(),
                 native_result.stderr == errors.getvalue()),
                (0, True, True),
                f"native Discord output mismatch: stdout={native_stdout!r}, stderr={native_stderr!r}, peer_failures={failures!r}",
            )
        self.assertEqual(failures, [])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "Discord Gateway reconnect limit exceeded\n")
        self.assertEqual(captured[0]["payload"]["op"], 2)
        self.assertEqual(captured[1], {"opcode": 1, "payload": {"op": 1, "d": None}})
        self.assertNotIn("fake-heartbeat-token", output.getvalue() + errors.getvalue())
        self.assertEqual(len(captured), 2 * connection_count)
        if native_result is not None:
            self.assertEqual(
                (native_result.returncode, native_result.stdout, native_result.stderr),
                (0, output.getvalue(), errors.getvalue()),
            )
            self.assertEqual(captured[2:], captured[:2])
            self.assertNotIn("fake-heartbeat-token", native_result.stdout + native_result.stderr)

    def test_local_verified_rest_transport_maps_rate_limits_without_retries(self) -> None:
        native_available = _native_cpp_available()
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
            retry_attempts = 0
            header_retry_attempts = 0
            integer_retry_attempts = 0

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
                    nonlocal retry_attempts, header_retry_attempts, integer_retry_attempts
                    status_responses = {
                        "/api/v10/channels/9/messages": (204, b""),
                        "/api/v10/channels/10/messages": (400, b'{"message":"bad request","code":400}'),
                        "/api/v10/channels/11/messages": (401, b'{"message":"unauthorized","code":401}'),
                        "/api/v10/channels/12/messages": (403, b'{"message":"forbidden","code":403}'),
                        "/api/v10/channels/13/messages": (404, b'{"message":"not found","code":404}'),
                        "/api/v10/channels/14/messages": (429, b'{"message":"rate limited","retry_after":0.25,"global":true}'),
                        "/api/v10/channels/15/messages": (500, b'{"message":"server error","code":500}'),
                        "/api/v10/channels/16/messages": (502, b'{"message":"bad gateway","code":502}'),
                        "/api/v10/channels/17/messages": (503, b'{"message":"unavailable","code":503}'),
                        "/api/v10/channels/18/messages": (502, b'{not-json'),
                    }
                    if self.path in status_responses:
                        status, response_body = status_responses[self.path]
                        self._reply(status, response_body, {"Content-Type": "application/json"})
                        return
                    if self.path == "/api/v10/channels/3/messages":
                        self._reply(500, b'{"message":"temporary failure"}', {
                            "Content-Type": "application/json",
                        })
                        return
                    if self.path == "/api/v10/channels/2/messages":
                        retry_attempts += 1
                        if retry_attempts == 2:
                            self._reply(200, b'{"id":"retry-success"}', {"Content-Type": "application/json"})
                            return
                        retry_body = b'{"message":"slow down","retry_after":0.0,"global":true}'
                    elif self.path == "/api/v10/channels/5/messages":
                        header_retry_attempts += 1
                        if header_retry_attempts == 2:
                            self._reply(200, b'{"id":"header-retry-success"}', {"Content-Type": "application/json"})
                            return
                        self._reply(429, b'{"message":"slow down"}', {
                            "Content-Type": "application/json", "Retry-After": "0.0",
                        })
                        return
                    elif self.path == "/api/v10/channels/6/messages":
                        self._reply(429, b'{"message":"slow down"}', {
                            "Content-Type": "application/json", "Retry-After": "120.1",
                        })
                        return
                    elif self.path == "/api/v10/channels/7/messages":
                        integer_retry_attempts += 1
                        if integer_retry_attempts == 2:
                            self._reply(200, b'{"id":"integer-retry-success"}', {"Content-Type": "application/json"})
                            return
                        self._reply(429, b'{"message":"slow down"}', {
                            "Content-Type": "application/json", "Retry-After": "0",
                        })
                        return
                    elif self.path == "/api/v10/channels/8/messages":
                        self._reply(429, b'{"message":"slow down"}', {
                            "Content-Type": "application/json",
                            "Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT",
                        })
                        return
                    else:
                        retry_body = b'{"message":"slow down","retry_after":0.5,"global":true}'
                    self._reply(429, retry_body, {
                        "Content-Type": "application/json",
                        "X-RateLimit-Bucket": "local-bucket",
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset-After": "0.5",
                        "Retry-After": "120.1" if self.path == "/api/v10/channels/2/messages" else "0.5",
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
async fn inspectFailure(token: Secret<String>, endpoint: String) {
    let result = await rest.requestBot(token, "POST", endpoint, bytesFromString("{}"), seconds(3))
    match result {
        Err(_) => println("transport failed")
        Ok(response) => {
            println(response.status)
            match rest.decodeResponseFailure(response) {
                Err(_) => println("decode rejected")
                Ok(failure) => match failure {
                    None => println("no failure")
                    Some(value) => {
                        println(value.status)
                        println(value.message)
                    }
                }
            }
        }
    }
}
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
            let retried = await rest.requestBotWithRetries(token, "POST", "channels/2/messages", bytesFromString("{\"content\":\"retry\"}"), seconds(3), 2)
            match retried {
                Err(_) => println("retry failed")
                Ok(response) => println(response.status)
            }
            let notRetried = await rest.requestBotWithRetries(token, "POST", "channels/3/messages", bytesFromString("{}"), seconds(3), 2)
            match notRetried {
                Err(_) => println("unexpected transport failure")
                Ok(response) => println(response.status)
            }
            let headerRetried = await rest.requestBotWithRetries(token, "POST", "channels/5/messages", bytesFromString("{}"), seconds(3), 2)
            match headerRetried {
                Err(_) => println("header retry failed")
                Ok(response) => println(response.status)
            }
            let overLimitHeader = await rest.requestBotWithRetries(token, "POST", "channels/6/messages", bytesFromString("{}"), seconds(3), 2)
            match overLimitHeader {
                Err(_) => println("header limit request failed")
                Ok(response) => println(response.status)
            }
            let integerHeaderRetried = await rest.requestBotWithRetries(token, "POST", "channels/7/messages", bytesFromString("{}"), seconds(3), 2)
            match integerHeaderRetried {
                Err(_) => println("integer header retry failed")
                Ok(response) => println(response.status)
            }
            let dateHeaderNotRetried = await rest.requestBotWithRetries(token, "POST", "channels/8/messages", bytesFromString("{}"), seconds(3), 2)
            match dateHeaderNotRetried {
                Err(_) => println("date header request failed")
                Ok(response) => println(response.status)
            }
            await inspectFailure(token, "channels/9/messages")
            await inspectFailure(token, "channels/10/messages")
            await inspectFailure(token, "channels/11/messages")
            await inspectFailure(token, "channels/12/messages")
            await inspectFailure(token, "channels/13/messages")
            await inspectFailure(token, "channels/14/messages")
            await inspectFailure(token, "channels/15/messages")
            await inspectFailure(token, "channels/16/messages")
            await inspectFailure(token, "channels/17/messages")
            await inspectFailure(token, "channels/18/messages")
            let badBudget = await rest.requestBotWithRetries(token, "POST", "channels/4/messages", bytesFromString("{}"), seconds(3), 6)
            match badBudget {
                Err(_) => println("retry budget rejected")
                Ok(_) => println("retry budget accepted")
            }
        }
    }
}
fn main() {
    let task = run()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln("runTasks failed: " + error)
    }
}
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
                reference_result = (0, output.getvalue(), errors.getvalue())
                native_result = None
                if native_available:
                    retry_attempts = 0
                    header_retry_attempts = 0
                    integer_retry_attempts = 0
                    native_result = _native_discord_run(
                        checked, directory / "rest", directory, local_port, certificate,
                        {"TYVERA_FAKE_DISCORD_REST_TOKEN": "fake-rest-token"},
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

            self.assertFalse(thread.is_alive(), "fake Discord REST server did not stop")
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(output.getvalue(),
                             '200\ntrue\ntrue\ntrue\n{"id":"fake-user","username":"test-bot"}\n'
                             '429\ntrue\ntrue\ntrue\n429\ntrue\ntrue\n200\n500\n200\n429\n200\n429\n'
                             '204\nno failure\n400\n400\nbad request\n401\n401\nunauthorized\n403\n403\nforbidden\n404\n404\nnot found\n429\n429\nrate limited\n500\n500\nserver error\n502\n502\nbad gateway\n503\n503\nunavailable\n502\ndecode rejected\nretry budget rejected\n')
            expected_paths = [
                "/api/v10/users/@me", "/api/v10/channels/1/messages",
                "/api/v10/channels/2/messages", "/api/v10/channels/2/messages",
                "/api/v10/channels/3/messages",
                "/api/v10/channels/5/messages", "/api/v10/channels/5/messages",
                "/api/v10/channels/6/messages",
                "/api/v10/channels/7/messages", "/api/v10/channels/7/messages",
                "/api/v10/channels/8/messages",
                "/api/v10/channels/9/messages",
                "/api/v10/channels/10/messages",
                "/api/v10/channels/11/messages",
                "/api/v10/channels/12/messages",
                "/api/v10/channels/13/messages",
                "/api/v10/channels/14/messages",
                "/api/v10/channels/15/messages",
                "/api/v10/channels/16/messages",
                "/api/v10/channels/17/messages",
                "/api/v10/channels/18/messages",
            ]
            self.assertEqual(
                [path for path, _, _ in captured], expected_paths * (2 if native_result is not None else 1),
            )
            self.assertEqual(retry_attempts, 2)
            self.assertEqual(header_retry_attempts, 2)
            self.assertEqual(integer_retry_attempts, 2)
            self.assertTrue(all(auth == "Bot fake-rest-token" for _, auth, _ in captured))
            self.assertEqual(captured[0][2], b"")
            self.assertEqual(captured[1][2], b'{"content":"hello"}')
            self.assertNotIn("fake-rest-token", output.getvalue() + errors.getvalue())
            if native_result is not None:
                self.assertEqual(
                    (native_result.returncode, native_result.stdout, native_result.stderr),
                    reference_result,
                )
                self.assertEqual(captured[21:], captured[:21])
                self.assertNotIn("fake-rest-token", native_result.stdout + native_result.stderr)



    def test_rate_limit_coordinator_enforces_scope_fifo_with_native_parity(self) -> None:
        if not _native_cpp_available():
            self.skipTest("native rate-limit coordinator parity requires a C++17 compiler")
        source = r'''import "../src/rest.tyv" as rest
async fn run() {
    let coordinator = rest.newRestRateLimitCoordinator()
    let firstTicket = await coordinator.request(reserveWaiter, "channel:10", 60000)
    let secondTicket = await coordinator.request(reserveWaiter, "channel:10", 60000)
    let secondEarly = await coordinator.request(acquire, "channels/:channel_id/messages", "channel:10", secondTicket, 60000)
    println(secondEarly > 0)
    let firstPermit = await coordinator.request(acquire, "channels/:channel_id/messages", "channel:10", firstTicket, 60000)
    println(firstPermit)
    let firstComplete = coordinator.request(complete, firstTicket, "channels/:channel_id/messages", "channel:10", None<String>(), None<Int>(), None<Int>(), 200, false, None<Int>())
    let firstCompleteValue = await firstComplete
    let secondPermit = await coordinator.request(acquire, "channels/:channel_id/messages", "channel:10", secondTicket, 60000)
    println(secondPermit)
    let secondComplete = coordinator.request(complete, secondTicket, "channels/:channel_id/messages", "channel:10", None<String>(), None<Int>(), None<Int>(), 200, false, None<Int>())
    let secondCompleteValue = await secondComplete

    let olderTicket = await coordinator.request(reserveWaiter, "channel:20", 60000)
    let laterTicket = await coordinator.request(reserveWaiter, "channel:20", 60000)
    let independentTicket = await coordinator.request(reserveWaiter, "channel:21", 60000)
    let laterEarly = await coordinator.request(acquire, "channels/:channel_id/messages", "channel:20", laterTicket, 60000)
    println(laterEarly > 0)
    let independentPermit = await coordinator.request(acquire, "channels/:channel_id/messages", "channel:21", independentTicket, 60000)
    println(independentPermit)
    let independentComplete = coordinator.request(complete, independentTicket, "channels/:channel_id/messages", "channel:21", None<String>(), None<Int>(), None<Int>(), 200, false, None<Int>())
    let independentCompleteValue = await independentComplete
    let olderPermit = await coordinator.request(acquire, "channels/:channel_id/messages", "channel:20", olderTicket, 60000)
    println(olderPermit)
    let olderComplete = coordinator.request(complete, olderTicket, "channels/:channel_id/messages", "channel:20", None<String>(), None<Int>(), None<Int>(), 200, false, None<Int>())
    let olderCompleteValue = await olderComplete
    let laterPermit = await coordinator.request(acquire, "channels/:channel_id/messages", "channel:20", laterTicket, 60000)
    println(laterPermit)
    let laterComplete = coordinator.request(complete, laterTicket, "channels/:channel_id/messages", "channel:20", None<String>(), None<Int>(), None<Int>(), 200, false, None<Int>())
    let laterCompleteValue = await laterComplete

    let cancelledTicket = await coordinator.request(reserveWaiter, "channel:30", 60000)
    let cancelled = await coordinator.request(cancelWaiter, cancelledTicket)
    println(cancelled)
    let cancelledAcquire = await coordinator.request(acquire, "channels/:channel_id/messages", "channel:30", cancelledTicket, 60000)
    println(cancelledAcquire)
}
fn main() {
    let pending = run()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-fifo-", dir=DISCORD_ROOT) as temporary:
            directory = Path(temporary)
            entry = directory / "fifo.tyv"
            entry.write_text(source, encoding="utf-8")
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output = io.StringIO()
            errors = io.StringIO()
            ReferenceInterpreterBackend().execute(
                checked, output=output, error_output=errors, project_root=DISCORD_ROOT,
            )
            reference = (0, output.getvalue(), errors.getvalue())
            executable = directory / "fifo-native"
            CppNativeBackend().build(
                lower_checked_program(checked), executable, project_root=DISCORD_ROOT,
            )
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            environment.pop("DISCORD_TOKEN", None)
            if os.name != "nt":
                environment["PATH"] = ""
            native = subprocess.run(
                [str(executable)], cwd=DISCORD_ROOT, capture_output=True, text=True,
                encoding="utf-8", env=environment, check=False, timeout=30,
            )
            native_result = (native.returncode, native.stdout, native.stderr)
            expected = "true\n0\n0\ntrue\n0\n0\n0\ntrue\n-2\n"
            self.assertEqual(reference, (0, expected, ""))
            self.assertEqual(native_result, reference)

    def test_rate_limit_tickets_and_completions_cannot_replay_new_reservations(self) -> None:
        source = r'''import "../src/rest.tyv" as rest
async fn verify() {
    let c = rest.newRestRateLimitCoordinator()
    let route = "channels/:channel_id/messages"
    let scope = "channel:10"
    let first = await c.request(reserveWaiter, scope, 60000)
    let firstPermit = await c.request(acquire, route, scope, first, 60000)
    println(firstPermit == 0)
    let firstDone = await c.request(complete, first, route, scope, None<String>(), None<Int>(), None<Int>(), 200, false, None<Int>())
    println(firstDone == 1)
    let second = await c.request(reserveWaiter, scope, 60000)
    println(second > first)
    let staleAcquire = await c.request(acquire, route, scope, first, 60000)
    println(staleAcquire == -2)
    let secondPermit = await c.request(acquire, route, scope, second, 60000)
    println(secondPermit == 0)
    let staleDone = await c.request(complete, first, route, scope, None<String>(), Some(100), None<Int>(), 200, false, None<Int>())
    println(staleDone == 0)
    let third = await c.request(reserveWaiter, scope, 60000)
    let thirdWait = await c.request(acquire, route, scope, third, 60000)
    println(thirdWait > 0)
    let secondDone = await c.request(complete, second, route, scope, None<String>(), None<Int>(), None<Int>(), 200, false, None<Int>())
    println(secondDone == 1)
    let thirdPermit = await c.request(acquire, route, scope, third, 60000)
    println(thirdPermit == 0)
    let thirdDone = await c.request(complete, third, route, scope, None<String>(), None<Int>(), None<Int>(), 200, false, None<Int>())
    println(thirdDone == 1)
    let cancelled = await c.request(reserveWaiter, scope, 60000)
    let removed = await c.request(cancelWaiter, cancelled)
    println(removed)
    let replacement = await c.request(reserveWaiter, scope, 60000)
    println(replacement > cancelled)
    let replayed = await c.request(acquire, route, scope, cancelled, 60000)
    println(replayed == -2)
    let replacementPermit = await c.request(acquire, route, scope, replacement, 60000)
    println(replacementPermit == 0)
}
fn main() {
    let task = verify()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-ticket-ownership-", dir=DISCORD_ROOT) as temporary:
            directory = Path(temporary)
            entry = directory / "ownership.tyv"
            entry.write_text(source, encoding="utf-8")
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output, errors = io.StringIO(), io.StringIO()
            ReferenceInterpreterBackend().execute(
                checked, output=output, error_output=errors, project_root=DISCORD_ROOT,
            )
            reference = (0, output.getvalue(), errors.getvalue())
            self.assertEqual(reference, (0, "true\n" * 14, ""))
            if _native_cpp_available():
                executable = directory / "ownership-native"
                CppNativeBackend().build(
                    lower_checked_program(checked), executable, project_root=DISCORD_ROOT,
                )
                environment = os.environ.copy()
                for name in ("PYTHONPATH", "PYTHONHOME", "DISCORD_TOKEN"):
                    environment.pop(name, None)
                if os.name != "nt":
                    environment["PATH"] = ""
                native = subprocess.run(
                    [str(executable)], cwd=DISCORD_ROOT, capture_output=True, text=True,
                    encoding="utf-8", env=environment, check=False, timeout=30,
                )
                self.assertEqual((native.returncode, native.stdout, native.stderr), reference)

    def test_concurrent_global_rate_limits_keep_the_longest_deadline(self) -> None:
        source = r'''import "../src/rest.tyv" as rest
async fn verify() {
    let c = rest.newRestRateLimitCoordinator()
    let route = "channels/:channel_id/messages"
    let first = await c.request(reserveWaiter, "channel:1", 120000)
    let second = await c.request(reserveWaiter, "channel:2", 120000)
    let firstPermit = await c.request(acquire, route, "channel:1", first, 60000)
    let secondPermit = await c.request(acquire, route, "channel:2", second, 60000)
    println(firstPermit == 0 && secondPermit == 0)
    let longer = await c.request(complete, first, route, "channel:1", None<String>(), None<Int>(), None<Int>(), 429, true, Some(60000))
    let shorter = await c.request(complete, second, route, "channel:2", None<String>(), None<Int>(), None<Int>(), 429, true, Some(1000))
    println(longer == 1 && shorter == 1)
    let waiting = await c.request(reserveWaiter, "channel:3", 120000)
    let wait = await c.request(acquire, route, "channel:3", waiting, 60000)
    println(wait > 50000)
    let removed = await c.request(cancelWaiter, waiting)
    println(removed)
}
fn main() {
    let task = verify()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln(error)
    }
}
'''
        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-global-deadline-", dir=DISCORD_ROOT) as temporary:
            directory = Path(temporary)
            entry = directory / "global.tyv"
            entry.write_text(source, encoding="utf-8")
            checked = compile_checked_project(entry, DISCORD_ROOT)
            output, errors = io.StringIO(), io.StringIO()
            ReferenceInterpreterBackend().execute(
                checked, output=output, error_output=errors, project_root=DISCORD_ROOT,
            )
            reference = (0, output.getvalue(), errors.getvalue())
            self.assertEqual(reference, (0, "true\n" * 4, ""))
            if _native_cpp_available():
                executable = directory / "global-native"
                CppNativeBackend().build(
                    lower_checked_program(checked), executable, project_root=DISCORD_ROOT,
                )
                native = subprocess.run(
                    [str(executable)], cwd=DISCORD_ROOT, capture_output=True, text=True,
                    encoding="utf-8", check=False, timeout=30,
                )
                self.assertEqual((native.returncode, native.stdout, native.stderr), reference)


    def test_coordinated_rest_waits_for_global_and_bucket_limits_with_native_parity(self) -> None:
        native_available = _native_cpp_available()
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("OpenSSL is required to create a temporary fake REST certificate")

        with tempfile.TemporaryDirectory(prefix=".tyvera-discord-coordinator-", dir=DISCORD_ROOT) as temporary:
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
            lock = threading.Lock()
            state = {"count": 0, "active": 0, "maximum_active": 0, "arrivals": [], "paths": []}

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, format: str, *args: object) -> None:
                    return

                def do_GET(self) -> None:
                    with lock:
                        index = state["count"]
                        state["count"] += 1
                        state["arrivals"].append(time.monotonic())
                        state["paths"].append(self.path)
                        state["active"] += 1
                        state["maximum_active"] = max(state["maximum_active"], state["active"])
                    released = False
                    try:
                        # A small server-side hold makes an accidental concurrent
                        # request visible without relying on a tight timing bound.
                        time.sleep(0.05)
                        pair = index % 5
                        if pair == 0:
                            status = 429
                            body = b'{"message":"global limit","retry_after":0.35,"global":true}'
                            headers = {
                                "X-RateLimit-Bucket": "global-bucket",
                                "X-RateLimit-Remaining": "0",
                                "X-RateLimit-Reset-After": "0.35",
                                "Retry-After": "0.35",
                                "X-RateLimit-Global": "true",
                            }
                        elif pair == 2:
                            status = 200
                            body = b'{"ok":true}'
                            headers = {
                                "X-RateLimit-Bucket": "channel-bucket",
                                "X-RateLimit-Remaining": "0",
                                "X-RateLimit-Reset-After": "0.35",
                            }
                        else:
                            status = 200
                            body = b'{"ok":true}'
                            headers = {
                                "X-RateLimit-Bucket": "other-bucket" if pair == 1 else "channel-bucket",
                                "X-RateLimit-Remaining": "3",
                                "X-RateLimit-Reset-After": "0.35",
                            }
                        with lock:
                            state["active"] -= 1
                            released = True
                        self.send_response(status)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Connection", "close")
                        self.send_header("Content-Type", "application/json")
                        for name, value in headers.items():
                            self.send_header(name, value)
                        self.end_headers()
                        self.wfile.write(body)
                        self.close_connection = True
                    finally:
                        if not released:
                            with lock:
                                state["active"] -= 1

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            local_port = server.server_address[1]
            server.socket = server_context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            entry = directory / "coordinator-test.tyv"
            entry.write_text(r'''import "../src/rest.tyv" as rest
async fn run() {
    match environment("TYVERA_FAKE_DISCORD_REST_TOKEN") {
        None => println("missing token")
        Some(token) => {
            let coordinator = rest.newRestRateLimitCoordinator()
            let invalidTimeout = await rest.requestBotCoordinated(coordinator, "channels/:channel_id/messages", "channel:10", token, "GET", "channels/10/messages", bytesFromString(""), milliseconds(0))
            match invalidTimeout {
                Err(_) => println("timeout rejected")
                Ok(_) => println("timeout accepted")
            }
            let global = await rest.requestBotCoordinated(coordinator, "channels/:channel_id/messages", "channel:10", token, "GET", "channels/10/messages", bytesFromString(""), seconds(3))
            match global {
                Err(_) => println("global request failed")
                Ok(response) => println(response.status)
            }
            let afterGlobal = await rest.requestBotCoordinated(coordinator, "channels/:channel_id/messages", "channel:11", token, "GET", "channels/11/messages", bytesFromString(""), seconds(3))
            match afterGlobal {
                Err(_) => println("global wait failed")
                Ok(response) => println(response.status)
            }
            let exhausted = await rest.requestBotCoordinated(coordinator, "channels/:channel_id/messages", "channel:12", token, "GET", "channels/12/messages", bytesFromString(""), seconds(3))
            match exhausted {
                Err(_) => println("bucket request failed")
                Ok(response) => println(response.status)
            }
            let first = rest.requestBotCoordinated(coordinator, "channels/:channel_id/messages", "channel:12", token, "GET", "channels/12/messages/first", bytesFromString(""), seconds(3))
            let second = rest.requestBotCoordinated(coordinator, "channels/:channel_id/messages", "channel:12", token, "GET", "channels/12/messages/second", bytesFromString(""), seconds(3))
            let firstResult = await first
            match firstResult {
                Err(_) => println("first queued request failed")
                Ok(response) => println(response.status)
            }
            let secondResult = await second
            match secondResult {
                Err(_) => println("second queued request failed")
                Ok(response) => println(response.status)
            }
            let bucketRead = coordinator.request(observedBucket, "channels/:channel_id/messages", "channel:12")
            let observed = await bucketRead
            println(observed == Some("channel-bucket"))
        }
    }
}
fn main() {
    let task = run()
    match runTasks() {
        Ok(_) => {}
        Err(error) => eprintln("runTasks failed: " + error)
    }
}
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
                reference_result = (0, output.getvalue(), errors.getvalue())
                native_result = None
                if native_available:
                    native_result = _native_discord_run(
                        checked, directory / "coordinator", directory, local_port, certificate,
                        {"TYVERA_FAKE_DISCORD_REST_TOKEN": "fake-rest-token"},
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

            self.assertFalse(thread.is_alive(), "fake Discord REST server did not stop")
            expected = "timeout rejected\n429\n200\n200\n200\n200\ntrue\n"
            self.assertEqual((0, output.getvalue(), errors.getvalue()), (0, expected, ""))
            with lock:
                arrivals = list(state["arrivals"])
                request_paths = list(state["paths"])
                maximum_active = state["maximum_active"]
            self.assertEqual(len(arrivals), 5 * (2 if native_result is not None else 1))
            self.assertEqual(maximum_active, 1)
            for offset in range(0, len(arrivals), 5):
                group = arrivals[offset:offset + 5]
                self.assertGreaterEqual(group[1] - group[0], 0.20)
                self.assertGreaterEqual(min(group[3], group[4]) - group[2], 0.20)
                # Concurrent tasks are ordered by reservation arrival, not source creation.
                # The ticket-level test above verifies the coordinator's FIFO contract.
                self.assertCountEqual(
                    request_paths[offset + 3:offset + 5],
                    ["/api/v10/channels/12/messages/first", "/api/v10/channels/12/messages/second"],
                )
            if native_result is not None:
                self.assertEqual(
                    (native_result.returncode, native_result.stdout, native_result.stderr),
                    reference_result,
                )

if __name__ == "__main__":
    unittest.main()
