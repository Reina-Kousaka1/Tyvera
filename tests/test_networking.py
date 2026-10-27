from __future__ import annotations

import io
import os
import socket
import ssl
import subprocess
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class AsyncNetworkingTests(unittest.TestCase):
    def run_source(self, source: str, *, root: Path | None = None) -> str:
        program, _ = compile_source(source, "networking-test.tyv")
        output = io.StringIO()
        Interpreter(program, output, project_root=root).run()
        return output.getvalue()

    def _make_local_tls_server(self, root: Path, handler, *, include_loopback_ip: bool = False):
        openssl = shutil.which("openssl")
        if openssl is None:
            git_openssl = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"
            openssl = str(git_openssl) if git_openssl.is_file() else None
        if openssl is None:
            self.skipTest("OpenSSL is required to create a temporary local TLS certificate")
        certificate, private_key = root / "localhost.crt", root / "localhost.key"
        subject_alt_name = "DNS:localhost,IP:127.0.0.1" if include_loopback_ip else "DNS:localhost"
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(private_key),
             "-out", str(certificate), "-days", "2", "-subj", "/CN=localhost",
             "-addext", f"subjectAltName={subject_alt_name}"],
            check=True, capture_output=True, text=True,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate), str(private_key))
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        failures: list[BaseException] = []

        def serve() -> None:
            connection = None
            try:
                connection, _ = listener.accept()
                connection.settimeout(5)
                with context.wrap_socket(connection, server_side=True) as secure:
                    handler(secure)
            except BaseException as error:
                failures.append(error)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return listener.getsockname()[1], certificate, thread, failures

    @staticmethod
    def _trust_local_certificate(certificate: Path):
        default_context = ssl.create_default_context

        def make_context(*args, **kwargs):
            context = default_context(*args, **kwargs)
            context.load_verify_locations(cafile=str(certificate))
            return context

        return patch("tyvera.interpreter.ssl.create_default_context", side_effect=make_context)

    def test_local_tcp_echo_uses_async_readiness_and_resource_cleanup(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "examples" / "tcp_echo.tyv").read_text(encoding="utf-8")
        self.assertEqual(self.run_source(source), "hello\n from Tyvera\neof\necho complete\n")

    def test_connection_refusal_is_a_tyvera_result(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''fn main() {{
    let connection = tcpConnect("127.0.0.1", {port})
    runTasks()
    match connection.tryResult() {{
        Err(error) => println(error)
        Ok(value) => match move value {{
            Some(result) => match move result {{
                Ok(opened) => {{ opened.close() }}
                Err(error) => println(error)
            }}
            None => println("still pending")
        }}
    }}
}}'''
        self.assertEqual(self.run_source(source), "connection refused\n")

    def test_invalid_port_is_a_controlled_network_result(self) -> None:
        source = '''async fn connect() {
    let result = await tcpConnect("127.0.0.1", 65536)
    match move result {
        Err(error) => println(error)
        Ok(connection) => { connection.close() }
    }
}
fn main() {
    let task = connect()
    runTasks()
}'''
        self.assertEqual(self.run_source(source), "port must be between 1 and 65535\n")

    def test_one_listener_accepts_multiple_concurrent_connections(self) -> None:
        source = '''async fn echo(connection: TcpConnection) {
    let request = await connection.read(16)
    match request {
        Ok(value) => match value {
            Some(bytes) => { await connection.writeAll(bytes) }
            None => println("unexpected EOF")
        }
        Err(error) => println(error)
    }
    connection.close()
}
async fn client(port: Int) {
    let connected = await tcpConnect("127.0.0.1", port)
    match move connected {
        Ok(connection) => {
            await connection.writeAll(bytesFromString("client"))
            let response = await connection.read(16)
            match response {
                Ok(value) => match value {
                    Some(bytes) => {}
                    None => println("unexpected EOF")
                }
                Err(error) => println(error)
            }
            connection.close()
        }
        Err(error) => println(error)
    }
}
async fn serveTwo() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Ok(listener) => match listener.localAddress() {
            Ok(address) => {
                let first = client(address.port())
                let second = client(address.port())
                let one = await listener.accept()
                match move one {
                    Ok(connection) => { await echo(move connection) }
                    Err(error) => println(error)
                }
                let two = await listener.accept()
                match move two {
                    Ok(connection) => { await echo(move connection) }
                    Err(error) => println(error)
                }
                await first
                await second
                listener.close()
                println("accepted two connections")
            }
            Err(error) => println(error)
        }
        Err(error) => println(error)
    }
}
fn main() {
    let task = serveTwo()
    runTasks()
}'''
        self.assertEqual(self.run_source(source), "accepted two connections\n")

    def test_write_all_applies_socket_backpressure_without_dropping_bytes(self) -> None:
        payload_size = 4096
        writes = 2048
        expected = payload_size * writes
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        release_reader = threading.Event()
        received = [0]
        server_errors: list[BaseException] = []

        def serve() -> None:
            try:
                connection, _ = listener.accept()
                with connection:
                    release_reader.wait(5)
                    while True:
                        chunk = connection.recv(64 * 1024)
                        if not chunk:
                            break
                        received[0] += len(chunk)
            except BaseException as error:
                server_errors.append(error)

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        release_timer = threading.Timer(0.05, release_reader.set)
        release_timer.start()
        payload = "x" * payload_size
        source = f'''async fn sendAll(port: Int) {{
    let connected = await tcpConnect("127.0.0.1", port)
    match move connected {{
        Ok(connection) => {{
            var total = 0
            var index = 0
            while index < {writes} {{
                let sent = await connection.writeAll(bytesFromString("{payload}"))
                match sent {{
                    Ok(count) => total = total + count
                    Err(error) => println(error)
                }}
                index = index + 1
            }}
            println(total)
            connection.close()
        }}
        Err(error) => println(error)
    }}
}}
fn main() {{
    let task = sendAll({port})
    runTasks()
}}'''
        try:
            self.assertEqual(self.run_source(source), f"{expected}\n")
            server.join(10)
            self.assertFalse(server.is_alive(), "loopback reader did not finish")
            self.assertEqual(server_errors, [])
            self.assertEqual(received[0], expected)
        finally:
            release_reader.set()
            listener.close()
            release_timer.cancel()

    def test_dns_success_and_invalid_hostname_are_controlled_results(self) -> None:
        source = '''fn main() {
    let local = resolveHost("localhost", 80)
    let invalid = resolveHost("not a valid host name", 80)
    runTasks()
    match local.tryResult() {
        Ok(value) => match value {
            Some(result) => match result {
                Ok(addresses) => println(addresses.length > 0)
                Err(error) => println(error)
            }
            None => println("local lookup pending")
        }
        Err(error) => println(error)
    }
    match invalid.tryResult() {
        Ok(value) => match value {
            Some(result) => match result {
                Ok(_) => println("unexpected DNS success")
                Err(error) => println(error)
            }
            None => println("invalid lookup pending")
        }
        Err(error) => println(error)
    }
}'''
        self.assertEqual(self.run_source(source), "true\nDNS resolution failed\n")

    def test_cancelled_accept_wakes_scheduler_and_closes_owned_listener(self) -> None:
        source = '''async fn waitForConnection(listener: TcpListener) {
    let accepted = await listener.accept()
    match move accepted {
        Ok(connection) => { connection.close() }
        Err(_) => println("accept ended")
    }
}
async fn cancelAfterDelay(target: Task<Void>) {
    await sleepFor(milliseconds(30))
    target.cancel()
}
async fn runCancellation() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Ok(listener) => {
            let waiter = waitForConnection(move listener)
            let cancellation = cancelAfterDelay(waiter)
            await cancellation
            println(waiter.isCancelled())
        }
        Err(error) => println(error)
    }
}
fn main() {
    let task = runCancellation()
    runTasks()
}'''
        self.assertEqual(self.run_source(source), "true\n")

    def test_supervisor_restart_closes_actor_owned_network_wait(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''async fn bindAndWait(port: Int, reply: ReplySender<Int>) {{
    let opened = await tcpListen("127.0.0.1", port, 8)
    match move opened {{
        Ok(listener) => {{
            reply.send(port)
            let accepted = await listener.accept()
            match move accepted {{
                Ok(connection) => {{ connection.close() }}
                Err(_) => {{}}
            }}
            listener.close()
        }}
        Err(_) => reply.send(-1)
    }}
}}
actor Worker {{
    let port: Int
    init(port: Int) {{ self.port = port }}
    on begin(reply: ReplySender<Int>) {{
        let task = bindAndWait(self.port, reply)
    }}
    on crash(reply: ReplySender<Int>) {{
        let failed = 1 / 0
    }}
}}
async fn drive(worker: ActorRef<Worker>) {{
    let started = await worker.request(begin)
    let crashed = await worker.request(crash)
}}
fn main() {{
    let supervisor = oneForOne(1, 1000)
    let worker = supervisor.start(Worker({port}))
    let task = drive(worker)
    match runTasks() {{
        Ok(_) => println("unexpected success")
        Err(_) => println("actor restarted")
    }}
    println(worker.isAlive())
    let reopened = tcpListen("127.0.0.1", {port}, 8)
    runTasks()
    match reopened.tryResult() {{
        Err(error) => println(error)
        Ok(value) => match move value {{
            None => println("listen pending")
            Some(result) => match move result {{
                Err(error) => println(error)
                Ok(listener) => {{
                    println("listener released")
                    listener.close()
                }}
            }}
        }}
    }}
    supervisor.stop()
}}'''
        self.assertEqual(self.run_source(source), "actor restarted\ntrue\nlistener released\n")

    def test_runtime_shutdown_closes_unclaimed_tcp_listener_result(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''fn main() {{
    let listener = tcpListen("127.0.0.1", {port}, 8)
    runTasks()
}}'''
        self.run_source(source)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reopened:
            reopened.bind(("127.0.0.1", port))
            reopened.listen(1)

    def test_inner_scope_cancels_pending_resource_returning_task(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''async fn owner(port: Int) {{
    {{
        let pending = tcpListen("127.0.0.1", port, 8)
    }}
    let reopened = await tcpListen("127.0.0.1", port, 8)
    match move reopened {{
        Ok(listener) => {{
            println("resource task cleaned up")
            listener.close()
        }}
        Err(error) => println(error)
    }}
}}
fn main() {{
    let task = owner({port})
    runTasks()
}}'''
        self.assertEqual(self.run_source(source), "resource task cleaned up\n")

    def test_try_result_consumes_and_cancels_pending_resource_task(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        source = f'''fn main() {{
    let pending = tcpListen("127.0.0.1", {port}, 8)
    let first = pending.tryResult()
    match move first {{
        Err(error) => println(error)
        Ok(value) => match move value {{
            None => println("pending task consumed")
            Some(result) => match move result {{
                Ok(listener) => {{ listener.close() }}
                Err(error) => println(error)
            }}
        }}
    }}
    let reopened = tcpListen("127.0.0.1", {port}, 8)
    runTasks()
    match reopened.tryResult() {{
        Err(error) => println(error)
        Ok(value) => match move value {{
            None => println("listen pending")
            Some(result) => match move result {{
                Ok(listener) => {{
                    println("port released")
                    listener.close()
                }}
                Err(error) => println(error)
            }}
        }}
    }}
}}'''
        self.assertEqual(self.run_source(source), "pending task consumed\nport released\n")

    def test_timeout_cancels_a_pending_accept(self) -> None:
        source = '''fn main() {
    let opening = tcpListen("127.0.0.1", 0, 8)
    runTasks()
    match opening.tryResult() {
        Err(error) => println(error)
        Ok(value) => match move value {
            None => println("listen pending")
            Some(result) => match move result {
                Err(error) => println(error)
                Ok(listener) => {
                    let pending = listener.accept()
                    let timed = pending.withTimeout(milliseconds(40))
                    match runTasks() {
                        Ok(_) => println("unexpected completion")
                        Err(_) => {}
                    }
                    match timed.tryResult() {
                        Err(error) => println(error)
                        Ok(value) => match move value {
                            None => println("timeout pending")
                            Some(result) => match move result {
                                Ok(connection) => { connection.close() }
                                Err(error) => println(error)
                            }
                        }
                    }
                    listener.close()
                }
            }
        }
    }
}'''
        self.assertEqual(self.run_source(source), "task timed out\n")

    def test_async_text_file_operations_share_the_existing_owned_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = '''async fn writeAndRead() {
    let opened = await openTextAsync("async.txt", "write")
    match move opened {
        Ok(file) => {
                    let written = await file.writeAsync("saved asynchronously")
                    match written {
                Ok(_) => {}
                Err(error) => println(error)
            }
            file.close()
        }
        Err(error) => println(error)
    }
    let reader = await openTextAsync("async.txt", "read")
    match move reader {
        Ok(file) => {
                    let contents = await file.readAllAsync()
                    match contents {
                Ok(contents) => println(contents)
                Err(error) => println(error)
            }
            file.close()
        }
        Err(error) => println(error)
    }
}
fn main() {
    let task = writeAndRead()
    runTasks()
}'''
            self.assertEqual(self.run_source(source, root=root), "saved asynchronously\n")
            self.assertEqual((root / "async.txt").read_text(encoding="utf-8"), "saved asynchronously")

    def test_resource_task_result_requires_a_binding_and_cannot_be_copied(self) -> None:
        invalid = '''async fn discard() {
    await tcpConnect("127.0.0.1", 1)
}
fn main() {
}'''
        with self.assertRaisesRegex(Diagnostic, "requires a result binding"):
            compile_source(invalid, "discard-resource-task.tyv")

        moved_twice = '''async fn closeIt(connection: TcpConnection) {}
async fn invalidUse() {
    let opened = await tcpConnect("127.0.0.1", 1)
    match move opened {
        Ok(connection) => {
            let task = closeIt(move connection)
            connection.isOpen()
        }
        Err(_) => {}
    }
}
fn main() {}'''
        with self.assertRaisesRegex(Diagnostic, "was already moved"):
            compile_source(moved_twice, "use-after-move-network.tyv")

    def test_network_resources_remain_excluded_from_aggregates_and_actors(self) -> None:
        invalid_programs = (
            ('''fn main() {
    let connections: List<TcpConnection> = List<TcpConnection>()
}''', "List cannot contain move-only Resource"),
            ('''actor Holder {
    var connection: TcpConnection
}
fn main() {}''', "actor state cannot contain move-only Resource"),
            ('''actor Holder { on keep(connection: TcpConnection) {} }
fn main() {}''', "actor message cannot carry mutable or process-local value type"),
            ('''fn main() {
    let listeners: Map<TcpListener,String> = Map<TcpListener,String>()
}''', "not hashable"),
        )
        for source, message in invalid_programs:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Diagnostic, message):
                    compile_source(source, "invalid-network-resource.tyv")

    def test_ipv6_loopback_echo_when_ipv6_is_available(self) -> None:
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 loopback is unavailable in this environment")
        source = '''async fn client(port: Int) {
    let result = await tcpConnect("::1", port)
    match move result {
        Ok(connection) => {
            await connection.writeAll(bytesFromString("v6"))
            let response = await connection.read(8)
            match response {
                Ok(value) => match value {
                    Some(bytes) => match bytes.toString() {
                        Ok(text) => println(text)
                        Err(error) => println(error)
                    }
                    None => println("unexpected EOF")
                }
                Err(error) => println(error)
            }
            connection.close()
        }
        Err(error) => println(error)
    }
}
async fn runEcho() {
    let result = await tcpListen("::1", 0, 8)
    match move result {
        Ok(listener) => match listener.localAddress() {
            Ok(address) => {
                let clientTask = client(address.port())
                let accepted = await listener.accept()
                match move accepted {
                    Ok(connection) => {
                        let request = await connection.read(8)
                        match request {
                            Ok(value) => match value {
                                Some(bytes) => { await connection.writeAll(bytes) }
                                None => println("unexpected EOF")
                            }
                            Err(error) => println(error)
                        }
                        connection.close()
                    }
                    Err(error) => println(error)
                }
                await clientTask
                listener.close()
            }
            Err(error) => println(error)
        }
        Err(error) => println(error)
    }
}
fn main() {
    let task = runEcho()
    runTasks()
}'''
        self.assertEqual(self.run_source(source), "v6\n")

    def test_tls_validates_trust_and_hostname_and_transfers_binary_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-local-tls-") as temporary:
            root = Path(temporary)
            def echo(secure: ssl.SSLSocket) -> None:
                payload = secure.recv(4)
                secure.sendall(payload)
                # Wait for the client's close_notify so this also covers the
                # orderly TLS resource-close path.
                while secure.recv(1024):
                    pass

            port, certificate, thread, errors = self._make_local_tls_server(root, echo)
            trusted_port = port
            source = f'''async fn exchange() {{
    let opened = await tlsConnect("localhost", {port})
    match move opened {{
        Err(error) => println(error)
        Ok(connection) => {{
            let sent = await connection.writeAll(bytesFromString("ping"))
            match sent {{
                Ok(count) => println(count)
                Err(error) => println(error)
            }}
            let received = await connection.read(16)
            match received {{
                Err(error) => println(error)
                Ok(value) => match value {{
                    None => println("unexpected EOF")
                    Some(bytes) => {{
                        let text = bytes.toString()
                        match text {{
                            Ok(value) => println(value)
                            Err(error) => println(error)
                        }}
                    }}
                }}
            }}
            connection.close()
        }}
    }}
}}
fn main() {{ let task = exchange(); runTasks() }}'''
            with self._trust_local_certificate(certificate):
                output = self.run_source(source, root=root)
            thread.join(5)
            self.assertFalse(thread.is_alive(), "local TLS echo server did not finish")
            self.assertEqual(output, "4\nping\n")
            self.assertEqual(errors, [], output)

            def reject_unexpected_payload(secure: ssl.SSLSocket) -> None:
                secure.recv(1)

            # A trusted certificate for localhost must still fail hostname
            # verification when the caller connects by the IPv4 literal.
            port, certificate, thread, errors = self._make_local_tls_server(root, reject_unexpected_payload)
            wrong_host = source.replace(
                f'tlsConnect("localhost", {trusted_port})',
                f'tlsConnect("127.0.0.1", {port})',
            )
            with self._trust_local_certificate(certificate):
                output = self.run_source(wrong_host, root=root)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(output, "TLS certificate verification failed\n")

    def test_https_client_uses_verified_tls_and_preserves_http_semantics(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-local-https-") as temporary:
            root = Path(temporary)
            server_events: list[str] = []

            def serve_http(secure: ssl.SSLSocket) -> None:
                server_events.append("handshake complete")
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    part = secure.recv(4096)
                    if not part:
                        return
                    request.extend(part)
                server_events.append("request received")
                secure.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\nsecure"
                )

            port, certificate, thread, errors = self._make_local_tls_server(
                root, serve_http, include_loopback_ip=True,
            )
            source = f'''async fn fetch() {{
    let parsed = httpUrl("https://127.0.0.1:{port}/health?mode=local")
    match parsed {{
        Err(error) => println(error)
        Ok(url) => {{
            let response = await httpGet(url, milliseconds(2000))
            match response {{
                Err(error) => println(error)
                Ok(response) => {{
                    println(response.status())
                    let text = response.body().toString()
                    match text {{
                        Ok(value) => println(value)
                        Err(error) => println(error)
                    }}
                }}
            }}
        }}
    }}
}}
fn main() {{ let task = fetch(); runTasks() }}'''
            with self._trust_local_certificate(certificate):
                output = self.run_source(source, root=root)
            thread.join(5)
            self.assertFalse(thread.is_alive(), "local HTTPS server did not finish")
            self.assertEqual(errors, [], f"{output}; events={server_events!r}")
            self.assertEqual(output, "200\nsecure\n")

    def test_tls_rejects_untrusted_local_certificates_by_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tyvera-untrusted-tls-") as temporary:
            root = Path(temporary)

            def wait_for_handshake_failure(secure: ssl.SSLSocket) -> None:
                secure.recv(1)

            port, _, thread, _ = self._make_local_tls_server(root, wait_for_handshake_failure)
            source = f'''async fn connect() {{
    let opened = await tlsConnect("localhost", {port})
    match move opened {{
        Err(error) => println(error)
        Ok(connection) => {{ connection.close(); println("unexpected TLS success") }}
    }}
}}
fn main() {{ let task = connect(); runTasks() }}'''
            self.assertEqual(self.run_source(source, root=root), "TLS certificate verification failed\n")
            thread.join(5)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
