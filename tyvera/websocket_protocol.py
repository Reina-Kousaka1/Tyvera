"""Bounded WebSocket client framing and HTTP Upgrade validation.

The module is transport independent. The interpreter supplies a TCP or TLS
byte stream; this codec owns the WebSocket wire rules and memory limits.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field

from .http_protocol import HttpLimits, HttpMessageDecoder, HttpProtocolError, HttpResponse, HttpUrl, parse_url


MAX_WEBSOCKET_FRAME_BYTES = 1 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_WEBSOCKET_HANDSHAKE_BYTES = 32 * 1024
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CLOSE_CODES = set(range(1000, 1015)) - {1004, 1005, 1006}


class WebSocketProtocolError(ValueError):
    """A malformed URL, Upgrade response, or WebSocket frame."""


@dataclass(frozen=True, slots=True, repr=False)
class WebSocketUrl:
    scheme: str
    transport: HttpUrl

    def __repr__(self) -> str:
        return "<WebSocketUrl redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class WebSocketEvent:
    opcode: int
    payload: bytes

    def __repr__(self) -> str:
        return "<WebSocketEvent redacted>"


def parse_websocket_url(value: str) -> WebSocketUrl:
    scheme, separator, remainder = value.partition("://")
    normalized = scheme.lower()
    if not separator or normalized not in {"ws", "wss"}:
        raise WebSocketProtocolError("WebSocket URL scheme must be ws or wss")
    transport_scheme = "https" if normalized == "wss" else "http"
    try:
        transport = parse_url(f"{transport_scheme}://{remainder}")
    except ValueError as error:
        raise WebSocketProtocolError(str(error)) from error
    return WebSocketUrl(normalized, transport)


def create_client_handshake(url: WebSocketUrl, key: str | None = None) -> tuple[str, bytes]:
    selected_key = key or base64.b64encode(os.urandom(16)).decode("ascii")
    try:
        decoded = base64.b64decode(selected_key, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise WebSocketProtocolError("invalid WebSocket key") from error
    if len(decoded) != 16:
        raise WebSocketProtocolError("invalid WebSocket key")
    target = url.transport.target
    authority = url.transport.authority
    request = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: {authority}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {selected_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    return selected_key, request


def make_accept_value(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")


def validate_server_handshake(decoder: HttpMessageDecoder, key: str) -> HttpResponse | None:
    if len(decoder.buffer) > MAX_WEBSOCKET_HANDSHAKE_BYTES:
        raise WebSocketProtocolError("WebSocket handshake exceeds size limit")
    header_end = decoder.buffer.find(b"\r\n\r\n")
    if header_end >= 0:
        first_line = bytes(decoder.buffer[:header_end]).split(b"\r\n", 1)[0]
        try:
            status, _ = HttpMessageDecoder._parse_status_line(first_line)
        except (HttpProtocolError, UnicodeDecodeError):
            status = 0
        if status and status != 101:
            raise WebSocketProtocolError("WebSocket server did not accept the Upgrade request")
    try:
        response = decoder.next_message()
    except ValueError as error:
        raise WebSocketProtocolError(f"invalid WebSocket handshake: {error}") from error
    if response is None:
        return None
    if not isinstance(response, HttpResponse) or response.status != 101:
        raise WebSocketProtocolError("WebSocket server did not accept the Upgrade request")
    if response.body:
        raise WebSocketProtocolError("WebSocket Upgrade response must not contain a body")
    upgrades = [token.strip().lower() for item in response.headers.values("upgrade") for token in item.split(",")]
    connections = [token.strip().lower() for item in response.headers.values("connection") for token in item.split(",")]
    accept_values = response.headers.values("sec-websocket-accept")
    if "websocket" not in upgrades or "upgrade" not in connections:
        raise WebSocketProtocolError("invalid WebSocket Upgrade headers")
    if len(accept_values) != 1 or not hmac.compare_digest(accept_values[0], make_accept_value(key)):
        raise WebSocketProtocolError("invalid WebSocket accept value")
    if response.headers.contains("sec-websocket-extensions"):
        raise WebSocketProtocolError("WebSocket extensions are not negotiated")
    return response


def encode_client_frame(opcode: int, payload: bytes = b"", *, mask: bytes | None = None,
                        fin: bool = True) -> bytes:
    if opcode not in {0, 1, 2, 8, 9, 10}:
        raise WebSocketProtocolError("invalid WebSocket opcode")
    control = opcode >= 8
    if control and (not fin or len(payload) > 125):
        raise WebSocketProtocolError("invalid WebSocket control frame")
    if len(payload) > MAX_WEBSOCKET_FRAME_BYTES:
        raise WebSocketProtocolError("WebSocket frame exceeds size limit")
    if opcode == 1:
        _validate_utf8(payload, "WebSocket text message is not valid UTF-8")
    if opcode == 8:
        validate_close_payload(payload)
    key = os.urandom(4) if mask is None else mask
    if len(key) != 4:
        raise WebSocketProtocolError("WebSocket mask must contain four bytes")
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        header = bytes((first, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((first, 0x80 | 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((first, 0x80 | 127)) + length.to_bytes(8, "big")
    masked = bytes(byte ^ key[index & 3] for index, byte in enumerate(payload))
    return header + key + masked


def validate_close_payload(payload: bytes) -> tuple[int | None, str]:
    if not payload:
        return None, ""
    if len(payload) == 1:
        raise WebSocketProtocolError("invalid WebSocket close payload")
    code = int.from_bytes(payload[:2], "big")
    if code not in _CLOSE_CODES and not 3000 <= code <= 4999:
        raise WebSocketProtocolError("invalid WebSocket close code")
    reason = _validate_utf8(payload[2:], "WebSocket close reason is not valid UTF-8")
    return code, reason


@dataclass(slots=True)
class WebSocketFrameDecoder:
    max_frame_bytes: int = MAX_WEBSOCKET_FRAME_BYTES
    max_message_bytes: int = MAX_WEBSOCKET_MESSAGE_BYTES
    buffer: bytearray = field(default_factory=bytearray)
    _fragment_opcode: int | None = None
    _fragment: bytearray = field(default_factory=bytearray)

    def feed(self, payload: bytes) -> None:
        # A single network read may contain complete frames, but never allow an
        # incomplete frame plus queued data to grow without a fixed bound.
        if len(self.buffer) + len(payload) > self.max_message_bytes + 14:
            raise WebSocketProtocolError("WebSocket receive buffer exceeds size limit")
        self.buffer.extend(payload)

    def next_event(self) -> WebSocketEvent | None:
        while True:
            frame = self._next_frame()
            if frame is None:
                return None
            opcode, fin, payload = frame
            if opcode >= 8:
                if opcode == 8:
                    validate_close_payload(payload)
                return WebSocketEvent(opcode, payload)
            if opcode == 0:
                if self._fragment_opcode is None:
                    raise WebSocketProtocolError("unexpected WebSocket continuation frame")
                self._append_fragment(payload)
                if not fin:
                    continue
                assembled = bytes(self._fragment)
                message_opcode = self._fragment_opcode
                self._fragment.clear()
                self._fragment_opcode = None
                return self._message(message_opcode, assembled)
            if self._fragment_opcode is not None:
                raise WebSocketProtocolError("new WebSocket data frame before fragmented message ended")
            if fin:
                return self._message(opcode, payload)
            self._fragment_opcode = opcode
            self._fragment.clear()
            self._append_fragment(payload)

    def _append_fragment(self, payload: bytes) -> None:
        if len(self._fragment) + len(payload) > self.max_message_bytes:
            raise WebSocketProtocolError("WebSocket message exceeds size limit")
        self._fragment.extend(payload)

    @staticmethod
    def _message(opcode: int, payload: bytes) -> WebSocketEvent:
        if opcode == 1:
            _validate_utf8(payload, "WebSocket text message is not valid UTF-8")
        return WebSocketEvent(opcode, payload)

    def _next_frame(self) -> tuple[int, bool, bytes] | None:
        data = self.buffer
        if len(data) < 2:
            return None
        first, second = data[0], data[1]
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketProtocolError("WebSocket reserved bits are not supported")
        opcode = first & 0x0F
        if opcode not in {0, 1, 2, 8, 9, 10}:
            raise WebSocketProtocolError("invalid WebSocket opcode")
        if second & 0x80:
            raise WebSocketProtocolError("server WebSocket frames must not be masked")
        length_code = second & 0x7F
        offset = 2
        if length_code == 126:
            if len(data) < offset + 2:
                return None
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 126:
                raise WebSocketProtocolError("non-canonical WebSocket payload length")
            offset += 2
        elif length_code == 127:
            if len(data) < offset + 8:
                return None
            length = int.from_bytes(data[offset:offset + 8], "big")
            if length < 65_536 or length & (1 << 63):
                raise WebSocketProtocolError("invalid WebSocket payload length")
            offset += 8
        else:
            length = length_code
        if length > self.max_frame_bytes:
            raise WebSocketProtocolError("WebSocket frame exceeds size limit")
        if opcode >= 8 and (not fin or length > 125):
            raise WebSocketProtocolError("invalid WebSocket control frame")
        if len(data) < offset + length:
            return None
        payload = bytes(data[offset:offset + length])
        del data[:offset + length]
        return opcode, fin, payload


def _validate_utf8(payload: bytes, error: str) -> str:
    try:
        return payload.decode("utf-8", "strict")
    except UnicodeDecodeError as failure:
        raise WebSocketProtocolError(error) from failure
