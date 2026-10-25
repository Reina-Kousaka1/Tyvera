from __future__ import annotations

import io
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from tyvera.compiler import compile_source
from tyvera.interpreter import Interpreter
from tyvera.source import Diagnostic


class AsyncNetworkingTests(unittest.TestCase):
    def run_source(self, source: str, *, root: Path | None = None) -> str:
        program, _ = compile_source(source, "networking-test.tyv")
        output = io.StringIO()
        Interpreter(program, output, project_root=root).run()
        return output.getvalue()

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


if __name__ == "__main__":
    unittest.main()
