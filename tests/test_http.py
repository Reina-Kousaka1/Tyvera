from __future__ import annotations

import io
import socket
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tyvera.compiler import compile_source
from tyvera.http_protocol import (
    HttpHeaders,
    HttpLimits,
    HttpMessageDecoder,
    HttpMethod,
    HttpProtocolError,
    HttpRequest,
    HttpResponse,
    HttpRouter,
    encode_request,
    encode_response,
    make_method,
    parse_url,
)
from tyvera.interpreter import Interpreter


class HttpProtocolTests(unittest.TestCase):
    def test_incremental_request_decoder_and_repeated_query_values(self) -> None:
        decoder = HttpMessageDecoder(HttpLimits())
        decoder.feed(b"POST /items/blue?q=one&q=two HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\n\r\nbo")
        self.assertIsNone(decoder.next_message())
        decoder.feed(b"dy")
        request = decoder.next_message()
        self.assertIsInstance(request, HttpRequest)
        assert isinstance(request, HttpRequest)
        self.assertEqual(request._path, "/items/blue")
        self.assertEqual(request.query_values("q"), ["one", "two"])
        self.assertEqual(request.body, b"body")

    def test_decoder_accepts_pipelined_messages_one_at_a_time(self) -> None:
        decoder = HttpMessageDecoder(HttpLimits())
        decoder.feed(
            b"GET /one HTTP/1.1\r\nHost: localhost\r\n\r\n"
            b"GET /two HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        first = decoder.next_message()
        second = decoder.next_message()
        self.assertEqual(first.target, "/one")
        self.assertEqual(second.target, "/two")

    def test_decoder_rejects_ambiguous_framing_and_over_limit_body(self) -> None:
        decoder = HttpMessageDecoder(HttpLimits())
        decoder.feed(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\nx")
        with self.assertRaisesRegex(HttpProtocolError, "ambiguous"):
            decoder.next_message()

        limited = HttpMessageDecoder(HttpLimits(body_bytes=4))
        limited.feed(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\n12345")
        with self.assertRaisesRegex(HttpProtocolError, "body exceeds"):
            limited.next_message()

    def test_response_decoder_accepts_fragmented_chunked_body_and_trailers(self) -> None:
        decoder = HttpMessageDecoder(HttpLimits(), response=True)
        wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"4;name=value\r\nWiki\r\n5\r\npedia\r\n0\r\nX-Checksum: ok\r\n\r\n"
                b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        messages = []
        for byte in wire:
            decoder.feed(bytes((byte,)))
            message = decoder.next_message()
            if message is not None:
                messages.append(message)
        self.assertEqual(len(messages), 2)
        response = messages[0]
        self.assertIsInstance(response, HttpResponse)
        assert isinstance(response, HttpResponse)
        self.assertEqual(response.body, b"Wikipedia")
        second = messages[1]
        self.assertIsInstance(second, HttpResponse)
        assert isinstance(second, HttpResponse)
        self.assertEqual((second.status, second.body), (204, b""))

    def test_chunked_decoder_enforces_body_metadata_and_trailer_limits(self) -> None:
        oversized = HttpMessageDecoder(HttpLimits(body_bytes=4), response=True)
        oversized.feed(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n12345\r\n0\r\n\r\n")
        with self.assertRaisesRegex(HttpProtocolError, "body exceeds"):
            oversized.next_message()

        forbidden = HttpMessageDecoder(HttpLimits(), response=True)
        forbidden.feed(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nContent-Length: 0\r\n\r\n")
        with self.assertRaisesRegex(HttpProtocolError, "forbidden HTTP trailer"):
            forbidden.next_message()

        malformed = HttpMessageDecoder(HttpLimits(), response=True)
        malformed.feed(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZ\r\n")
        with self.assertRaisesRegex(HttpProtocolError, "chunk size"):
            malformed.next_message()

        unsupported = HttpMessageDecoder(HttpLimits(), response=True)
        unsupported.feed(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n")
        with self.assertRaisesRegex(HttpProtocolError, "unsupported HTTP transfer"):
            unsupported.next_message()

    def test_large_content_length_response_is_not_mistaken_for_oversized_headers(self) -> None:
        body = b"x" * 65_536
        decoder = HttpMessageDecoder(HttpLimits(body_bytes=len(body)), response=True)
        decoder.feed(b"HTTP/1.1 200 OK\r\nContent-Length: 65536\r\n\r\n" + body)
        response = decoder.next_message()
        self.assertIsInstance(response, HttpResponse)
        assert isinstance(response, HttpResponse)
        self.assertEqual(response.body, body)

    def test_decoder_rejects_duplicate_content_length_and_malformed_target(self) -> None:
        decoder = HttpMessageDecoder(HttpLimits())
        decoder.feed(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx")
        with self.assertRaisesRegex(HttpProtocolError, "Content-Length"):
            decoder.next_message()
        with self.assertRaisesRegex(ValueError, "percent encoding"):
            parse_url("http://localhost/a%Q0")

    def test_decoder_rejects_malformed_lines_limits_and_truncated_messages(self) -> None:
        for payload in (
            b"GET / HTTP/1.1\nHost: localhost\n\n",
            b"GET / HTTP/1.1\r\nInvalid Header: x\r\nHost: localhost\r\n\r\n",
            b"G(ET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
            b"GET /%FF HTTP/1.1\r\nHost: localhost\r\n\r\n",
        ):
            decoder = HttpMessageDecoder(HttpLimits())
            with self.subTest(payload=payload), self.assertRaises(HttpProtocolError):
                decoder.feed(payload)
                decoder.next_message()

        long_start = HttpMessageDecoder(HttpLimits(request_line_bytes=256))
        with self.assertRaisesRegex(HttpProtocolError, "start line"):
            long_start.feed(b"G" * 257)
        long_header = HttpMessageDecoder(HttpLimits(header_bytes=1024))
        long_header.feed(b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Large: " + b"x" * 1025 + b"\r\n\r\n")
        with self.assertRaisesRegex(HttpProtocolError, "header"):
            long_header.next_message()
        too_many_headers = HttpMessageDecoder(HttpLimits(header_count=1))
        too_many_headers.feed(b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Extra: x\r\n\r\n")
        with self.assertRaisesRegex(HttpProtocolError, "header count"):
            too_many_headers.next_message()

        truncated = HttpMessageDecoder(HttpLimits())
        truncated.feed(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\nx")
        self.assertIsNone(truncated.next_message())
        truncated.finish()
        with self.assertRaisesRegex(HttpProtocolError, "closed during HTTP body"):
            truncated.next_message()

        invalid_status = HttpMessageDecoder(HttpLimits(), response=True)
        invalid_status.feed(b"HTTP/1.1 nope Invalid\r\nContent-Length: 0\r\n\r\n")
        with self.assertRaisesRegex(HttpProtocolError, "status code"):
            invalid_status.next_message()

    def test_url_supports_ipv6_and_preserves_empty_query(self) -> None:
        value = parse_url("http://[::1]:8080/path?")
        self.assertEqual(value.host, "::1")
        self.assertEqual(value.port, 8080)
        self.assertEqual(value.target, "/path?")
        query_only = parse_url("HTTP://LOCALHOST?x=1")
        self.assertEqual((query_only.host, query_only.target), ("localhost", "/?x=1"))
        for invalid in (
            "http://user@localhost/", "http://localhost:/", "http://localhost:0/",
            "http://example..com/", "http://[not-ipv6]/", "http://localhost/#fragment",
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                parse_url(invalid)

    def test_http_10_and_11_keep_alive_rules_and_close_override(self) -> None:
        headers = HttpHeaders()
        self.assertTrue(HttpRequest(make_method("GET"), "/", headers, b"").is_keep_alive())
        self.assertFalse(HttpRequest(make_method("GET"), "/", headers, b"", "HTTP/1.0").is_keep_alive())
        headers.set("Connection", "keep-alive, close")
        self.assertFalse(HttpRequest(make_method("GET"), "/", headers, b"").is_keep_alive())
        headers.set("Connection", "keep-alive")
        self.assertTrue(HttpRequest(make_method("GET"), "/", headers, b"", "HTTP/1.0").is_keep_alive())

    def test_request_and_response_encoders_apply_framing_and_head_rules(self) -> None:
        headers = HttpHeaders()
        headers.set("Host", "localhost")
        request = encode_request(make_method("POST"), "/items", headers, b"x")
        self.assertIn(b"content-length: 1\r\n", request)
        self.assertTrue(request.endswith(b"\r\nx"))

        response = HttpResponse(200, HttpHeaders(), b"hello")
        head = encode_response(response, request_method="HEAD")
        self.assertIn(b"content-length: 5\r\n", head)
        self.assertTrue(head.endswith(b"\r\n\r\n"))

    def test_router_matches_parameters_and_reports_not_found_and_method_not_allowed(self) -> None:
        get = make_method("GET")
        post = make_method("POST")
        router = HttpRouter()
        router.add(get, "/users/:user", HttpResponse(200, HttpHeaders(), b"user"))
        headers = HttpHeaders()
        headers.set("Host", "localhost")
        request = HttpRequest(get, "/users/42", headers, b"")
        self.assertEqual(router.dispatch(request).status, 200)
        self.assertEqual(request.route_parameters, {"user": "42"})
        self.assertEqual(router.dispatch(HttpRequest(get, "/missing", headers, b"")).status, 404)
        denied = router.dispatch(HttpRequest(post, "/users/42", headers, b""))
        self.assertEqual(denied.status, 405)
        self.assertEqual(denied.headers.get("allow"), "GET")


class HttpTyveraApiTests(unittest.TestCase):
    def test_local_http_server_example_serves_concurrent_clients_and_shuts_down(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "examples" / "http_server.tyv").read_text(encoding="utf-8")
        program, _ = compile_source(source, str(root / "examples" / "http_server.tyv"))
        output = io.StringIO()
        peers = []
        dispatch = HttpRouter.dispatch

        def observe_peer(router: HttpRouter, request: HttpRequest) -> HttpResponse:
            peers.append(request._remote_address)
            return dispatch(router, request)

        with patch.object(HttpRouter, "dispatch", observe_peer):
            Interpreter(program, output, project_root=root).run()
        lines = output.getvalue().splitlines()
        self.assertEqual(lines.count("200"), 2)
        self.assertEqual(lines.count("201"), 1)
        self.assertEqual(lines.count("Hello from Tyvera"), 2)
        self.assertEqual(lines.count("Created"), 1)
        self.assertEqual(lines[-1], "HTTP server stopped")
        self.assertEqual(len(peers), 3)
        self.assertTrue(all(peer is not None and peer.host == "127.0.0.1" for peer in peers))

    def test_idle_connection_times_out_with_a_controlled_408_response(self) -> None:
        source = '''async fn idleClient(port: Int) {
    let connected = await tcpConnect("127.0.0.1", port)
    match move connected {
        Err(error) => println(error)
        Ok(connection) => {
            let received = await connection.read(4096)
            match received {
                Err(error) => println(error)
                Ok(chunk) => match chunk {
                    None => println("unexpected EOF")
                    Some(bytes) => {
                        let method = httpMethod("GET")
                        match method {
                            Err(error) => println(error)
                            Ok(value) => {
                                let decoder = httpResponseDecoder(httpDefaultLimits(), value)
                                decoder.feed(bytes)
                                let parsed = decoder.nextResponse()
                                match parsed {
                                    Err(error) => println(error)
                                    Ok(response) => match response {
                                        None => println("incomplete timeout response")
                                        Some(value) => println(value.status())
                                    }
                                }
                            }
                        }
                    }
                }
            }
            connection.close()
        }
    }
}
async fn serveIdle() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(error) => println(error)
        Ok(listener) => {
            let address = listener.localAddress()
            match address {
                Err(error) => { println(error); listener.close() }
                Ok(value) => {
                    let configured = httpServer(move listener, httpRouter(), httpDefaultLimits(), milliseconds(250))
                    match move configured {
                        Err(error) => println(error)
                        Ok(server) => {
                            let serving = server.start()
                            let client = idleClient(value.port())
                            await client
                            server.shutdown()
                            let stopped = await serving
                            server.close()
                        }
                    }
                }
            }
        }
    }
}
fn main() { let task = serveIdle(); runTasks() }'''
        program, _ = compile_source(source, "http-timeout.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        self.assertEqual(output.getvalue(), "408\n")

    def test_server_processes_pipelined_keep_alive_requests_and_honors_close(self) -> None:
        source = '''async fn serveForTest() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(error) => println("ERROR " + error)
        Ok(listener) => {
            let local = listener.localAddress()
            match local {
                Err(error) => println("ERROR " + error)
                Ok(address) => {
                    let openedServer = httpServer(move listener, httpRouter(), httpDefaultLimits(), milliseconds(2000))
                    match move openedServer {
                        Err(error) => println("ERROR " + error)
                        Ok(server) => {
                            let serving = server.start()
                            println("READY " + address.toString())
                            await sleepFor(milliseconds(1500))
                            server.shutdown()
                            let stopped = await serving
                            server.close()
                        }
                    }
                }
            }
        }
    }
}
fn main() { let task = serveForTest(); runTasks() }'''
        # Add one static route through a source substitution to keep the server fixture small.
        source = source.replace(
            'let openedServer = httpServer(move listener, httpRouter(), httpDefaultLimits(), milliseconds(2000))',
            '''let router = httpRouter()
                    let get = httpMethod("GET")
                    match get {
                        Err(error) => println("ERROR " + error)
                        Ok(method) => {
                            let response = httpTextResponse(200, "keep-alive")
                            match response {
                                Err(error) => println("ERROR " + error)
                                Ok(value) => router.add(method, "/keep", value)
                            }
                        }
                    }
                    let openedServer = httpServer(move listener, router, httpDefaultLimits(), milliseconds(2000))''',
        )
        program, _ = compile_source(source, "http-keep-alive.tyv")

        class ReadyOutput(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.ready = threading.Event()

            def write(self, value: str) -> int:
                written = super().write(value)
                if "READY " in value or "ERROR " in value:
                    self.ready.set()
                return written

        output = ReadyOutput()
        failures: list[BaseException] = []

        def run_program() -> None:
            try:
                Interpreter(program, output).run()
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=run_program, daemon=True)
        thread.start()
        client: socket.socket | None = None
        try:
            self.assertTrue(output.ready.wait(3), output.getvalue())
            ready_line = next(line for line in output.getvalue().splitlines() if line.startswith("READY "))
            _, endpoint = ready_line.split(" ", 1)
            host, raw_port = endpoint.rsplit(":", 1)
            client = socket.create_connection((host, int(raw_port)), timeout=3)
            client.settimeout(3)
            client.sendall(
                b"GET /keep HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"
                b"GET /keep HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
            self.assertEqual(bytes(response).count(b"HTTP/1.1 200 OK"), 2)
            self.assertEqual(bytes(response).lower().count(b"content-length: 10"), 2)
            self.assertIn(b"connection: close", response.lower())
        finally:
            if client is not None:
                client.close()
            thread.join(4)
        self.assertFalse(thread.is_alive(), "Tyvera HTTP server runtime did not shut down")
        self.assertEqual(failures, [])

    def test_closing_server_cancels_accept_and_releases_listener(self) -> None:
        source = '''async fn startAndClose() {
    let opened = await tcpListen("127.0.0.1", 0, 8)
    match move opened {
        Err(error) => println(error)
        Ok(listener) => {
            let addressResult = listener.localAddress()
            match addressResult {
                Err(error) => { println(error); listener.close() }
                Ok(address) => {
                    let configured = httpServer(move listener, httpRouter(), httpDefaultLimits(), milliseconds(1000))
                    match move configured {
                        Err(error) => println(error)
                        Ok(server) => {
                            let serving = server.start()
                            await sleepFor(milliseconds(20))
                            server.close()
                            println(serving.isCancelled())
                            let reopened = await tcpListen("127.0.0.1", address.port(), 8)
                            match move reopened {
                                Err(error) => println(error)
                                Ok(reopenedListener) => {
                                    println("listener released")
                                    reopenedListener.close()
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
fn main() { let task = startAndClose(); runTasks() }'''
        program, _ = compile_source(source, "http-close.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        self.assertEqual(output.getvalue(), "true\nlistener released\n")

    def test_http_values_and_router_are_available_in_typed_tyvera(self) -> None:
        source = '''fn main() {
    let methodResult = httpMethod("GET")
    match methodResult {
        Err(error) => println(error)
        Ok(method) => {
            let headers = httpHeaders()
            headers.set("Host", "localhost")
            let requestResult = httpRequest(method, "/items/blue?q=one&q=two", headers, bytesFromString(""))
            match requestResult {
                Err(error) => println(error)
                Ok(request) => {
                    println(request.path())
                    match request.query("q") {
                        None => println("no query")
                        Some(value) => println(value)
                    }
                    println(request.queryValues("q").length)
                    match request.remoteAddress() {
                        None => println("no remote address")
                        Some(address) => println(address.toString())
                    }
                    let router = httpRouter()
                    let responseResult = httpTextResponse(200, "found")
                    match responseResult {
                        Err(error) => println(error)
                        Ok(response) => {
                            router.add(method, "/items/:name", response)
                            let routed = router.dispatch(request)
                            println(routed.status())
                            match request.routeParameter("name") {
                                None => println("no route parameter")
                                Some(value) => println(value)
                            }
                        }
                    }
                }
            }
        }
    }
}'''
        program, _ = compile_source(source, "http-api.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        self.assertEqual(output.getvalue(), "/items/blue\none\n2\nno remote address\n200\nblue\n")

    def test_json_request_and_response_reuse_the_typed_json_core(self) -> None:
        source = '''fn main() {
    let method = httpMethod("POST")
    match method {
        Err(error) => println(error)
        Ok(value) => {
            let headers = httpHeaders()
            headers.set("Host", "localhost")
            let incoming = httpRequest(value, "/", headers, bytesFromString("{\\"ok\\":true}"))
            match incoming {
                Err(error) => println(error)
                Ok(request) => match request.json() {
                    Err(error) => println(error)
                    Ok(json) => {
                        let encoded = httpJsonResponse(201, json)
                        match encoded {
                            Err(error) => println(error)
                            Ok(response) => {
                                println(response.status())
                                let body = response.body().toString()
                                match body {
                                    Err(error) => println(error)
                                    Ok(text) => println(text)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}'''
        program, _ = compile_source(source, "http-json.tyv")
        output = io.StringIO()
        Interpreter(program, output).run()
        self.assertEqual(output.getvalue(), '201\n{"ok":true}\n')

    def test_http_client_timeout_cancels_the_pending_response_read(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        request_received = threading.Event()
        release_response = threading.Event()
        server_errors: list[BaseException] = []

        def hold_response() -> None:
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    connection.recv(4096)
                    request_received.set()
                    release_response.wait(1)
            except BaseException as error:
                server_errors.append(error)

        thread = threading.Thread(target=hold_response, daemon=True)
        thread.start()
        source = f'''async fn fetch() {{
    let parsed = httpUrl("http://127.0.0.1:{port}/slow")
    match parsed {{
        Err(error) => println(error)
        Ok(url) => {{
            let response = await httpGet(url, milliseconds(300))
            match response {{
                Err(error) => println(error)
                Ok(_) => println("unexpected response")
            }}
        }}
    }}
}}
fn main() {{ let task = fetch(); runTasks() }}'''
        try:
            program, _ = compile_source(source, "http-client-timeout.tyv")
            output = io.StringIO()
            Interpreter(program, output).run()
            self.assertEqual(output.getvalue(), "HTTP timeout\n")
            self.assertTrue(request_received.is_set())
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(server_errors, [])
        finally:
            listener.close()

    def test_http_server_requires_explicit_listener_move(self) -> None:
        source = '''async fn build() {
    let opened = await tcpListen("127.0.0.1", 0, 4)
    match move opened {
        Err(_) => {}
        Ok(listener) => {
            let server = httpServer(listener, httpRouter(), httpDefaultLimits(), milliseconds(100))
        }
    }
}
fn main() { let task = build(); runTasks() }'''
        with self.assertRaisesRegex(Exception, "move listener"):
            compile_source(source, "http-ownership.tyv")


if __name__ == "__main__":
    unittest.main()
