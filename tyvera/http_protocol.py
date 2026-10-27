"""Backend-independent HTTP/1.1 values, codecs, and routing primitives.

The module consumes and produces bytes only. It does not own sockets or define
scheduler behavior; the reference interpreter adapts these values to Tyvera's
existing TCP tasks and resource rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re


_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "CONNECT", "TRACE"})
_STATUS_REASONS = {
    100: "Continue", 101: "Switching Protocols", 200: "OK", 201: "Created",
    202: "Accepted", 204: "No Content", 301: "Moved Permanently", 302: "Found",
    304: "Not Modified", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    409: "Conflict", 413: "Content Too Large", 414: "URI Too Long",
    431: "Request Header Fields Too Large",
    415: "Unsupported Media Type", 417: "Expectation Failed", 429: "Too Many Requests",
    500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}
_FORBIDDEN_HEADER_VALUE = frozenset(range(0, 9)) | frozenset({10, 13}) | frozenset(range(14, 32)) | {127}


class HttpProtocolError(ValueError):
    """A controlled malformed-message or configured-limit failure."""


@dataclass(frozen=True, slots=True)
class HttpLimits:
    request_line_bytes: int = 8192
    header_bytes: int = 32768
    header_count: int = 100
    body_bytes: int = 1_048_576
    max_connections: int = 128

    def __post_init__(self) -> None:
        if not 256 <= self.request_line_bytes <= 65_536:
            raise ValueError("request-line limit must be between 256 and 65536 bytes")
        if not 1024 <= self.header_bytes <= 262_144:
            raise ValueError("header limit must be between 1024 and 262144 bytes")
        if not 1 <= self.header_count <= 1024:
            raise ValueError("header count limit must be between 1 and 1024")
        if not 0 <= self.body_bytes <= 16_777_216:
            raise ValueError("body limit must be between 0 and 16777216 bytes")
        if not 1 <= self.max_connections <= 4096:
            raise ValueError("connection limit must be between 1 and 4096")


@dataclass(slots=True)
class HttpHeaders:
    """Ordered, case-insensitive header multimap."""

    entries: list[tuple[str, str]] = field(default_factory=list)

    def __repr__(self) -> str:
        return "<HttpHeaders redacted>"

    def add(self, name: str, value: str) -> None:
        normalized = _validate_header(name, value)
        self.entries.append((normalized, value.strip(" \t")))

    def set(self, name: str, value: str) -> None:
        normalized = _validate_header(name, value)
        self.remove(name)
        self.entries.append((normalized, value.strip(" \t")))

    def remove(self, name: str) -> int:
        normalized = _validate_header(name, "")
        old_count = len(self.entries)
        self.entries[:] = [(key, value) for key, value in self.entries if key != normalized]
        return old_count - len(self.entries)

    def values(self, name: str) -> list[str]:
        normalized = _validate_header(name, "")
        return [value for key, value in self.entries if key == normalized]

    def get(self, name: str) -> str | None:
        found = self.values(name)
        if not found:
            return None
        if name.lower() == "set-cookie":
            return found[0]
        return ", ".join(found)

    def contains(self, name: str) -> bool:
        normalized = _validate_header(name, "")
        return any(key == normalized for key, _ in self.entries)

    def copy(self) -> HttpHeaders:
        return HttpHeaders(self.entries.copy())


@dataclass(frozen=True, slots=True)
class HttpMethod:
    name: str

    @property
    def is_standard(self) -> bool:
        return self.name in _KNOWN_METHODS

    def __repr__(self) -> str:
        return "<HttpMethod>"


@dataclass(frozen=True, slots=True)
class HttpUrl:
    scheme: str
    host: str
    port: int
    target: str

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.scheme == "https" else 80
        return host if self.port == default_port else f"{host}:{self.port}"

    def __repr__(self) -> str:
        return "<HttpUrl redacted>"


@dataclass(slots=True)
class HttpRequest:
    method: HttpMethod
    target: str
    headers: HttpHeaders
    body: bytes
    version: str = "HTTP/1.1"
    route_parameters: dict[str, str] = field(default_factory=dict)
    _path: str = "/"
    _query: list[tuple[str, str]] = field(default_factory=list)
    _remote_address: object | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "<HttpRequest redacted>"

    def __post_init__(self) -> None:
        self._path, self._query = split_request_target(self.target)

    def query_values(self, name: str) -> list[str]:
        return [value for key, value in self._query if key == name]

    def is_keep_alive(self) -> bool:
        connection_values = [
            [token.strip().lower() for token in value.split(",")]
            for value in self.headers.values("connection")
        ]
        if any("close" in tokens for tokens in connection_values):
            return False
        return self.version == "HTTP/1.1" or any(
            "keep-alive" in tokens for tokens in connection_values
        )


@dataclass(slots=True)
class HttpResponse:
    status: int
    headers: HttpHeaders
    body: bytes

    def __repr__(self) -> str:
        return "<HttpResponse redacted>"


@dataclass(slots=True)
class HttpMessageDecoder:
    limits: HttpLimits
    response: bool = False
    request_method: str | None = None
    buffer: bytearray = field(default_factory=bytearray)
    eof: bool = False
    _chunk_cursor: int | None = field(default=None, repr=False)
    _chunk_body: bytearray = field(default_factory=bytearray, repr=False)
    _chunk_overhead: int = field(default=0, repr=False)
    _chunk_trailer_count: int = field(default=0, repr=False)
    _chunk_phase: str = field(default="size", repr=False)
    _chunk_remaining: int = field(default=0, repr=False)

    def __repr__(self) -> str:
        return "<HttpDecoder>"

    def feed(self, payload: bytes) -> None:
        if self.eof:
            raise HttpProtocolError("HTTP decoder is already at end of stream")
        max_buffer = (self.limits.request_line_bytes + self.limits.header_bytes
                      + self.limits.body_bytes + self.limits.header_bytes + 4)
        if len(self.buffer) + len(payload) > max_buffer:
            raise HttpProtocolError("HTTP message exceeds configured size limits")
        self.buffer.extend(payload)
        self._check_incomplete_limits()

    def finish(self) -> None:
        self.eof = True

    def next_message(self) -> HttpRequest | HttpResponse | None:
        header_end = self.buffer.find(b"\r\n\r\n")
        if header_end < 0:
            self._check_incomplete_limits()
            if self.eof and self.buffer:
                raise HttpProtocolError("connection closed during HTTP headers")
            return None
        if header_end + 4 > self.limits.request_line_bytes + self.limits.header_bytes + 4:
            raise HttpProtocolError("HTTP headers exceed configured size limit")
        head = bytes(self.buffer[:header_end])
        lines = head.split(b"\r\n")
        if not lines or len(lines[0]) > self.limits.request_line_bytes:
            raise HttpProtocolError("HTTP start line exceeds configured size limit")
        if any(len(line) > self.limits.header_bytes for line in lines[1:]):
            raise HttpProtocolError("HTTP header exceeds configured size limit")
        if len(lines) - 1 > self.limits.header_count:
            raise HttpProtocolError("HTTP header count exceeds configured limit")
        headers = HttpHeaders()
        for raw in lines[1:]:
            if not raw or raw[:1] in {b" ", b"\t"}:
                raise HttpProtocolError("invalid HTTP header line")
            name, separator, raw_value = raw.partition(b":")
            if not separator:
                raise HttpProtocolError("invalid HTTP header line")
            try:
                header_name = name.decode("ascii")
                header_value = raw_value.decode("latin-1")
                headers.add(header_name, header_value)
            except (UnicodeDecodeError, ValueError) as error:
                raise HttpProtocolError("invalid HTTP header line") from error
        content_length = self._content_length(headers)
        transfer_encoding = headers.values("transfer-encoding")
        if transfer_encoding and content_length is not None:
            raise HttpProtocolError("ambiguous HTTP message framing")
        transfer_codings = [coding.strip().lower() for value in transfer_encoding for coding in value.split(",")]
        if transfer_encoding and transfer_codings != ["chunked"]:
            raise HttpProtocolError("unsupported HTTP transfer encoding")
        chunked = transfer_codings == ["chunked"]

        first = lines[0]
        if self.response:
            start = self._parse_status_line(first)
            status, version = start
            no_body = (self.request_method == "HEAD" or 100 <= status < 200
                       or status in {204, 304})
            if no_body:
                body_length = 0
                message_end = header_end + 4
                body = b""
            elif chunked:
                decoded = self._decode_chunked_body(header_end + 4)
                if decoded is None:
                    return None
                body, message_end = decoded
            elif content_length is None:
                if not self.eof:
                    return None
                body_length = len(self.buffer) - header_end - 4
                if body_length > self.limits.body_bytes:
                    raise HttpProtocolError("HTTP body exceeds configured size limit")
                message_end = len(self.buffer)
                body = bytes(self.buffer[header_end + 4:message_end])
            else:
                body_length = content_length
                if body_length > self.limits.body_bytes:
                    raise HttpProtocolError("HTTP body exceeds configured size limit")
                message_end = header_end + 4 + body_length
                if len(self.buffer) < message_end:
                    if self.eof:
                        raise HttpProtocolError("connection closed during HTTP body")
                    return None
                body = bytes(self.buffer[header_end + 4:message_end])
            del self.buffer[:message_end]
            self._reset_chunk_decoder()
            return HttpResponse(status, headers, body)

        if chunked:
            decoded = self._decode_chunked_body(header_end + 4)
            if decoded is None:
                return None
            body, message_end = decoded
        elif content_length is None:
            body_length = 0
            message_end = header_end + 4
            body = b""
        else:
            body_length = content_length
            if body_length > self.limits.body_bytes:
                raise HttpProtocolError("HTTP body exceeds configured size limit")
            message_end = header_end + 4 + body_length
            if len(self.buffer) < message_end:
                if self.eof:
                    raise HttpProtocolError("connection closed during HTTP body")
                return None
            body = bytes(self.buffer[header_end + 4:message_end])
        request_line = self._parse_request_line(first)
        method, target, version = request_line
        if version == "HTTP/1.1":
            hosts = headers.values("host")
            if len(hosts) != 1 or not hosts[0].strip():
                raise HttpProtocolError("HTTP/1.1 request requires one non-empty Host header")
        del self.buffer[:message_end]
        self._reset_chunk_decoder()
        try:
            return HttpRequest(HttpMethod(method), target, headers, body, version)
        except ValueError as error:
            raise HttpProtocolError(str(error)) from error

    def _check_incomplete_limits(self) -> None:
        if self.buffer.find(b"\r\n\r\n") >= 0:
            return
        first_line_end = self.buffer.find(b"\r\n")
        if first_line_end >= 0 and first_line_end > self.limits.request_line_bytes:
            raise HttpProtocolError("HTTP start line exceeds configured size limit")
        if first_line_end < 0 and len(self.buffer) > self.limits.request_line_bytes:
            raise HttpProtocolError("HTTP start line exceeds configured size limit")
        if len(self.buffer) > self.limits.request_line_bytes + self.limits.header_bytes + 4:
            raise HttpProtocolError("HTTP headers exceed configured size limit")
        for index, byte in enumerate(self.buffer):
            if byte == 10 and (index == 0 or self.buffer[index - 1] != 13):
                raise HttpProtocolError("HTTP lines must end with CRLF")
            if byte == 13 and index + 1 < len(self.buffer) and self.buffer[index + 1] != 10:
                raise HttpProtocolError("HTTP lines must end with CRLF")

    def _decode_chunked_body(self, body_start: int) -> tuple[bytes, int] | None:
        if self._chunk_cursor is None:
            self._chunk_cursor = body_start
            self._chunk_body.clear()
            self._chunk_overhead = 0
            self._chunk_trailer_count = 0
            self._chunk_phase = "size"
            self._chunk_remaining = 0

        while True:
            cursor = self._chunk_cursor
            assert cursor is not None
            if self._chunk_phase in {"size", "trailers"}:
                line_end = self.buffer.find(b"\r\n", cursor)
                if line_end < 0:
                    if self._chunk_overhead + len(self.buffer) - cursor > self.limits.header_bytes:
                        raise HttpProtocolError("HTTP chunk metadata exceeds configured size limit")
                    if self.eof:
                        message = ("connection closed during HTTP chunk metadata"
                                   if self._chunk_phase == "size"
                                   else "connection closed during HTTP chunk trailers")
                        raise HttpProtocolError(message)
                    return None
                line = bytes(self.buffer[cursor:line_end])
                self._chunk_cursor = line_end + 2
                self._chunk_overhead += len(line) + 2
                if self._chunk_overhead > self.limits.header_bytes:
                    raise HttpProtocolError("HTTP chunk metadata exceeds configured size limit")
                if self._chunk_phase == "size":
                    size_text = line.partition(b";")[0]
                    if (any(value < 32 or value > 126 for value in line)
                            or not size_text or len(size_text) > 16
                            or any(value not in b"0123456789abcdefABCDEF" for value in size_text)):
                        raise HttpProtocolError("invalid HTTP chunk size")
                    size = int(size_text, 16)
                    if size > self.limits.body_bytes - len(self._chunk_body):
                        raise HttpProtocolError("HTTP body exceeds configured size limit")
                    if size == 0:
                        self._chunk_phase = "trailers"
                    else:
                        self._chunk_remaining = size
                        self._chunk_phase = "data"
                    continue

                if not line:
                    body = bytes(self._chunk_body)
                    message_end = self._chunk_cursor
                    return body, message_end
                if line[:1] in {b" ", b"\t"} or b":" not in line:
                    raise HttpProtocolError("invalid HTTP trailer line")
                self._chunk_trailer_count += 1
                if self._chunk_trailer_count > self.limits.header_count:
                    raise HttpProtocolError("HTTP trailer count exceeds configured limit")
                name, _, raw_value = line.partition(b":")
                try:
                    header_name = name.decode("ascii")
                    header_value = raw_value.decode("latin-1")
                    normalized = _validate_header(header_name, header_value)
                except (UnicodeDecodeError, ValueError) as error:
                    raise HttpProtocolError("invalid HTTP trailer line") from error
                if normalized in {"content-length", "transfer-encoding", "host"}:
                    raise HttpProtocolError("forbidden HTTP trailer field")
                continue

            if self._chunk_phase == "data":
                data_end = cursor + self._chunk_remaining
                if len(self.buffer) < data_end + 2:
                    if self.eof:
                        raise HttpProtocolError("connection closed during HTTP chunk data")
                    return None
                if self.buffer[data_end:data_end + 2] != b"\r\n":
                    raise HttpProtocolError("HTTP chunk data must end with CRLF")
                self._chunk_body.extend(self.buffer[cursor:data_end])
                self._chunk_overhead += 2
                if self._chunk_overhead > self.limits.header_bytes:
                    raise HttpProtocolError("HTTP chunk metadata exceeds configured size limit")
                self._chunk_cursor = data_end + 2
                self._chunk_remaining = 0
                self._chunk_phase = "size"
                continue

            raise HttpProtocolError("invalid HTTP chunk decoder state")

    def _reset_chunk_decoder(self) -> None:
        self._chunk_cursor = None
        self._chunk_body.clear()
        self._chunk_overhead = 0
        self._chunk_trailer_count = 0
        self._chunk_phase = "size"
        self._chunk_remaining = 0

    @staticmethod
    def _content_length(headers: HttpHeaders) -> int | None:
        values = headers.values("content-length")
        if not values:
            return None
        if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
            raise HttpProtocolError("invalid Content-Length")
        try:
            length = int(values[0], 10)
        except ValueError as error:
            raise HttpProtocolError("invalid Content-Length") from error
        if length > 16_777_216:
            raise HttpProtocolError("HTTP body exceeds configured size limit")
        return length

    @staticmethod
    def _parse_request_line(raw: bytes) -> tuple[str, str, str]:
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise HttpProtocolError("invalid HTTP request line") from error
        parts = line.split(" ")
        if len(parts) != 3 or not all(parts):
            raise HttpProtocolError("invalid HTTP request line")
        method, target, version = parts
        if not _TOKEN.fullmatch(method):
            raise HttpProtocolError("invalid HTTP method")
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise HttpProtocolError("unsupported HTTP version")
        if not target or any(ord(char) < 33 or ord(char) > 126 for char in target):
            raise HttpProtocolError("invalid HTTP request target")
        return method, target, version

    @staticmethod
    def _parse_status_line(raw: bytes) -> tuple[int, str]:
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise HttpProtocolError("invalid HTTP status line") from error
        parts = line.split(" ", 2)
        if len(parts) < 2 or parts[0] not in {"HTTP/1.0", "HTTP/1.1"}:
            raise HttpProtocolError("invalid HTTP status line")
        if len(parts[1]) != 3 or not parts[1].isascii() or not parts[1].isdecimal():
            raise HttpProtocolError("invalid HTTP status code")
        status = int(parts[1])
        if not 100 <= status <= 599:
            raise HttpProtocolError("invalid HTTP status code")
        return status, parts[0]


@dataclass(slots=True)
class HttpRouter:
    routes: list[tuple[HttpMethod, str, HttpResponse]] = field(default_factory=list)

    def __repr__(self) -> str:
        return "<HttpRouter>"

    def add(self, method: HttpMethod, path: str, response: HttpResponse) -> bool:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("route path must be an absolute path without query or fragment")
        split_request_target(path)
        if any(existing_method == method and existing_path == path
               for existing_method, existing_path, _ in self.routes):
            raise ValueError("route is already registered")
        if any(part.startswith(":") and (len(part) == 1 or not _TOKEN.fullmatch(part[1:]))
               for part in path.split("/")):
            raise ValueError("invalid route parameter name")
        self.routes.append((method, path, response))
        return True

    def dispatch(self, request: HttpRequest) -> HttpResponse:
        request.route_parameters.clear()
        path_matches: list[tuple[HttpMethod, HttpResponse, dict[str, str]]] = []
        for method, pattern, response in self.routes:
            parameters = _match_route(pattern, request._path)
            if parameters is not None:
                path_matches.append((method, response, parameters))
        if not path_matches:
            return HttpResponse(404, HttpHeaders(), b"Not Found")
        selected = next((row for row in path_matches if row[0] == request.method), None)
        if selected is None:
            allow = sorted({method.name for method, _, _ in path_matches})
            headers = HttpHeaders()
            headers.add("Allow", ", ".join(allow))
            return HttpResponse(405, headers, b"Method Not Allowed")
        _, response, parameters = selected
        request.route_parameters.update(parameters)
        return response


def parse_url(value: str) -> HttpUrl:
    scheme, separator, remainder = value.partition("://")
    if not separator:
        raise ValueError("HTTP URL must include a scheme and authority")
    normalized_scheme = scheme.lower()
    if normalized_scheme not in {"http", "https"}:
        raise ValueError("HTTP URL scheme must be http or https")
    if "#" in remainder:
        raise ValueError("HTTP request URL must not contain a fragment")
    authority_end = min(
        (position for marker in "/?" if (position := remainder.find(marker)) >= 0),
        default=len(remainder),
    )
    authority = remainder[:authority_end]
    target_suffix = remainder[authority_end:]
    if not authority or "@" in authority:
        raise ValueError("invalid HTTP URL authority")

    port = 443 if normalized_scheme == "https" else 80
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            raise ValueError("invalid IPv6 URL authority")
        host = authority[1:closing]
        if "%" in host:
            raise ValueError("IPv6 URL scope identifiers are not supported")
        try:
            ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError as error:
            raise ValueError("invalid IPv6 URL authority") from error
        remainder_authority = authority[closing + 1:]
        if remainder_authority:
            if not remainder_authority.startswith(":"):
                raise ValueError("invalid HTTP URL authority")
            port = _parse_url_port(remainder_authority[1:])
        host = host.lower()
    else:
        if authority.count(":") > 1:
            raise ValueError("IPv6 URL hosts must be enclosed in brackets")
        host, has_port, raw_port = authority.partition(":")
        if has_port:
            port = _parse_url_port(raw_port)
        _validate_url_hostname(host)
        host = host.lower()

    if not target_suffix:
        target = "/"
    elif target_suffix.startswith("?"):
        target = "/" + target_suffix
    elif target_suffix.startswith("/"):
        target = target_suffix
    else:
        raise ValueError("invalid HTTP URL target")
    split_request_target(target)
    return HttpUrl(normalized_scheme, host, port, target)


def _parse_url_port(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid HTTP URL port")
    port = int(value, 10)
    if not 1 <= port <= 65_535:
        raise ValueError("invalid HTTP URL port")
    return port


def _validate_url_hostname(host: str) -> None:
    if not host or len(host) > 253:
        raise ValueError("invalid HTTP URL hostname")
    labels = (host[:-1] if host.endswith(".") else host).split(".")
    if not labels or any(
        not label or len(label) > 63
        or label[0] == "-" or label[-1] == "-"
        or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
        for label in labels
    ):
        raise ValueError("invalid HTTP URL hostname")


def make_method(value: str) -> HttpMethod:
    if not _TOKEN.fullmatch(value):
        raise ValueError("invalid HTTP method")
    return HttpMethod(value)


def split_request_target(target: str) -> tuple[str, list[tuple[str, str]]]:
    if target == "*":
        return "*", []
    if (not target.startswith("/") or "#" in target
            or any(ord(char) < 33 or ord(char) > 126 for char in target)):
        raise ValueError("HTTP request target must be origin-form without a fragment")
    _validate_percent_escapes(target)
    path, separator, query = target.partition("?")
    for segment in path.split("/"):
        _percent_decode(segment)
    if not separator:
        return path or "/", []
    values: list[tuple[str, str]] = []
    for pair in query.split("&"):
        key, found, value = pair.partition("=")
        values.append((_percent_decode(key), _percent_decode(value if found else "")))
    return path or "/", values


def encode_request(
    method: HttpMethod, target: str, headers: HttpHeaders, body: bytes,
    *, limits: HttpLimits | None = None,
) -> bytes:
    method_name = method.name
    if not _TOKEN.fullmatch(method_name):
        raise HttpProtocolError("invalid HTTP method")
    split_request_target(target)
    if "\r" in target or "\n" in target:
        raise HttpProtocolError("invalid HTTP request target")
    output_headers = headers.copy()
    _prepare_body_headers(output_headers, body)
    limits = limits or HttpLimits()
    if len(body) > limits.body_bytes:
        raise HttpProtocolError("HTTP body exceeds configured size limit")
    if len(target.encode("ascii")) > limits.request_line_bytes:
        raise HttpProtocolError("HTTP request line exceeds configured size limit")
    if len(output_headers.entries) > limits.header_count:
        raise HttpProtocolError("HTTP header count exceeds configured limit")
    if not output_headers.contains("host") or not (output_headers.get("host") or "").strip():
        raise HttpProtocolError("HTTP request requires a non-empty Host header")
    start = f"{method_name} {target} HTTP/1.1\r\n".encode("ascii")
    encoded_headers = _encode_headers(output_headers)
    if len(encoded_headers) > limits.header_bytes:
        raise HttpProtocolError("HTTP headers exceed configured size limit")
    return start + encoded_headers + b"\r\n" + body


def encode_response(
    response: HttpResponse, *, keep_alive: bool = False, request_method: str | None = None,
    limits: HttpLimits | None = None,
) -> bytes:
    if not 100 <= response.status <= 599:
        raise HttpProtocolError("invalid HTTP status code")
    headers = response.headers.copy()
    _prepare_body_headers(headers, response.body)
    headers.set("Connection", "keep-alive" if keep_alive else "close")
    limits = limits or HttpLimits()
    if len(response.body) > limits.body_bytes:
        raise HttpProtocolError("HTTP body exceeds configured size limit")
    if len(headers.entries) > limits.header_count:
        raise HttpProtocolError("HTTP header count exceeds configured limit")
    encoded_headers = _encode_headers(headers)
    if len(encoded_headers) > limits.header_bytes:
        raise HttpProtocolError("HTTP headers exceed configured size limit")
    reason = _STATUS_REASONS.get(response.status, "Response")
    no_body = (request_method == "HEAD" or 100 <= response.status < 200
               or response.status in {204, 304})
    body = b"" if no_body else response.body
    return f"HTTP/1.1 {response.status:03d} {reason}\r\n".encode("ascii") + encoded_headers + b"\r\n" + body


def text_response(status: int, text: str) -> HttpResponse:
    headers = HttpHeaders()
    headers.set("Content-Type", "text/plain; charset=utf-8")
    return HttpResponse(status, headers, text.encode("utf-8"))


def _prepare_body_headers(headers: HttpHeaders, body: bytes) -> None:
    if not headers.contains("content-length"):
        headers.set("Content-Length", str(len(body)))
    values = headers.values("content-length")
    if len(values) != 1 or values[0] != str(len(body)):
        raise HttpProtocolError("Content-Length does not match body size")
    if headers.contains("transfer-encoding"):
        raise HttpProtocolError("transfer-encoding is not supported")


def _encode_headers(headers: HttpHeaders) -> bytes:
    output = bytearray()
    for name, value in headers.entries:
        _validate_header(name, value)
        try:
            output.extend(name.encode("ascii"))
            output.extend(b": ")
            output.extend(value.encode("latin-1"))
            output.extend(b"\r\n")
        except UnicodeEncodeError as error:
            raise HttpProtocolError("HTTP header value must use the ISO-8859-1 range") from error
    return bytes(output)


def _validate_header(name: str, value: str) -> str:
    if not _TOKEN.fullmatch(name):
        raise ValueError("invalid HTTP header name")
    if any(ord(char) in _FORBIDDEN_HEADER_VALUE for char in value):
        raise ValueError("invalid HTTP header value")
    if any(ord(char) > 255 for char in value):
        raise ValueError("HTTP header value must use the ISO-8859-1 range")
    return name.lower()


def _percent_decode(value: str) -> str:
    data = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            if index + 2 >= len(value):
                raise ValueError("invalid percent encoding in query")
            pair = value[index + 1:index + 3]
            try:
                data.append(int(pair, 16))
            except ValueError as error:
                raise ValueError("invalid percent encoding in query") from error
            index += 3
            continue
        try:
            data.extend(char.encode("ascii"))
        except UnicodeEncodeError as error:
            raise ValueError("query must use percent-encoded UTF-8") from error
        index += 1
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("query is not valid UTF-8") from error


def _validate_percent_escapes(value: str) -> None:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value):
            raise ValueError("invalid percent encoding in HTTP request target")
        try:
            int(value[index + 1:index + 3], 16)
        except ValueError as error:
            raise ValueError("invalid percent encoding in HTTP request target") from error
        index += 3


def _match_route(pattern: str, path: str) -> dict[str, str] | None:
    pattern_parts = pattern.strip("/").split("/") if pattern != "/" else []
    path_parts = path.strip("/").split("/") if path != "/" else []
    if len(pattern_parts) != len(path_parts):
        return None
    found: dict[str, str] = {}
    for expected, actual in zip(pattern_parts, path_parts):
        if expected.startswith(":"):
            if not actual:
                return None
            found[expected[1:]] = _percent_decode(actual)
        elif expected != actual:
            return None
    return found
