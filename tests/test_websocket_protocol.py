from __future__ import annotations

import unittest

from tyvera.http_protocol import HttpLimits, HttpMessageDecoder
from tyvera.websocket_protocol import (
    MAX_WEBSOCKET_FRAME_BYTES,
    MAX_WEBSOCKET_MESSAGE_BYTES,
    WebSocketFrameDecoder,
    WebSocketProtocolError,
    create_client_handshake,
    encode_client_frame,
    make_accept_value,
    parse_websocket_url,
    validate_close_payload,
    validate_server_handshake,
)


def server_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        return bytes((first, length)) + payload
    if length <= 0xFFFF:
        return bytes((first, 126)) + length.to_bytes(2, "big") + payload
    return bytes((first, 127)) + length.to_bytes(8, "big") + payload


class WebSocketProtocolTests(unittest.TestCase):
    def test_url_and_upgrade_handshake_use_scheme_defaults_and_accept_validation(self) -> None:
        ws = parse_websocket_url("ws://localhost/events?shard=0")
        wss = parse_websocket_url("wss://[::1]/gateway")
        self.assertEqual((ws.scheme, ws.transport.port, ws.transport.authority), ("ws", 80, "localhost"))
        self.assertEqual((wss.scheme, wss.transport.port, wss.transport.authority), ("wss", 443, "[::1]"))
        key, request = create_client_handshake(ws, "dGhlIHNhbXBsZSBub25jZQ==")
        self.assertIn(b"GET /events?shard=0 HTTP/1.1\r\n", request)
        self.assertIn(b"Connection: Upgrade\r\n", request)
        self.assertEqual(make_accept_value(key), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

        decoder = HttpMessageDecoder(HttpLimits(), response=True, request_method="GET")
        decoder.feed(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Connection: keep-alive, Upgrade\r\n"
                      "Upgrade: websocket\r\n"
                      f"Sec-WebSocket-Accept: {make_accept_value(key)}\r\n\r\n").encode() +
                     server_frame(1, b"ready"))
        response = validate_server_handshake(decoder, key)
        self.assertEqual(response.status, 101)
        self.assertEqual(bytes(decoder.buffer), server_frame(1, b"ready"))

    def test_upgrade_rejects_bad_status_headers_accept_and_extensions(self) -> None:
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        valid = f"HTTP/1.1 101 Upgrade\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: {make_accept_value(key)}\r\n"
        for header, message in (
            ("HTTP/1.1 200 OK\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: x\r\n", "did not accept"),
            (valid.replace("Upgrade: websocket", "Upgrade: h2"), "Upgrade headers"),
            (valid.replace(make_accept_value(key), "wrong"), "accept value"),
            (valid + "Sec-WebSocket-Extensions: permessage-deflate\r\n", "extensions"),
        ):
            decoder = HttpMessageDecoder(HttpLimits(), response=True, request_method="GET")
            decoder.feed((header + "\r\n").encode())
            with self.subTest(message=message), self.assertRaisesRegex(WebSocketProtocolError, message):
                validate_server_handshake(decoder, key)

    def test_client_frames_are_masked_and_support_all_payload_length_encodings(self) -> None:
        for payload in (b"hi", b"x" * 126, b"y" * 65_536):
            encoded = encode_client_frame(2, payload, mask=b"abcd")
            self.assertTrue(encoded[1] & 0x80)
            length_code = encoded[1] & 0x7F
            if len(payload) == 126:
                self.assertEqual(length_code, 126)
            elif len(payload) == 65_536:
                self.assertEqual(length_code, 127)
            decoder = WebSocketFrameDecoder()
            # Client masking is only valid for outgoing frames; unmask here to
            # independently exercise the server-frame parser.
            header_length = 2 if length_code < 126 else 4 if length_code == 126 else 10
            mask_offset = header_length
            self.assertEqual(encoded[mask_offset:mask_offset + 4], b"abcd")
            masked = encoded[mask_offset + 4:]
            unmasked = bytes(byte ^ b"abcd"[index & 3] for index, byte in enumerate(masked))
            decoder.feed(server_frame(2, unmasked))
            event = decoder.next_event()
            self.assertEqual(event.payload, payload)

    def test_decoder_reassembles_fragmented_utf8_messages_around_ping(self) -> None:
        decoder = WebSocketFrameDecoder()
        decoder.feed(server_frame(1, b"h", fin=False) + server_frame(9, b"probe") + server_frame(0, b"ello ", fin=False))
        self.assertEqual(decoder.next_event().opcode, 9)
        self.assertIsNone(decoder.next_event())
        decoder.feed(server_frame(0, "🌍".encode(), fin=True))
        message = decoder.next_event()
        self.assertEqual((message.opcode, message.payload.decode()), (1, "hello 🌍"))

    def test_decoder_rejects_masking_reserved_bits_lengths_controls_and_utf8(self) -> None:
        malformed = (
            (b"\x81\x80abcd", "must not be masked"),
            (b"\xc1\x00", "reserved bits"),
            (b"\x89\x7e\x00\x7e", "control frame"),
            (b"\x09\x00", "control frame"),
            (b"\x83\x00", "opcode"),
            (b"\x81\x7e\x00\x01x", "canonical"),
            (b"\x81\x7f\x80\x00\x00\x00\x00\x01\x00\x00", "payload length"),
            (server_frame(1, b"\xff"), "UTF-8"),
        )
        for payload, message in malformed:
            decoder = WebSocketFrameDecoder()
            decoder.feed(payload)
            with self.subTest(message=message), self.assertRaisesRegex(WebSocketProtocolError, message):
                decoder.next_event()
        with self.assertRaisesRegex(WebSocketProtocolError, "frame exceeds"):
            encode_client_frame(2, b"x" * (MAX_WEBSOCKET_FRAME_BYTES + 1))
        decoder = WebSocketFrameDecoder(max_frame_bytes=4, max_message_bytes=4)
        decoder.feed(server_frame(2, b"12345"))
        with self.assertRaisesRegex(WebSocketProtocolError, "frame exceeds"):
            decoder.next_event()

    def test_fragment_assembly_and_close_payload_are_bounded_and_validated(self) -> None:
        decoder = WebSocketFrameDecoder(max_frame_bytes=8, max_message_bytes=5)
        decoder.feed(server_frame(2, b"123", fin=False))
        self.assertIsNone(decoder.next_event())
        decoder.feed(server_frame(0, b"456", fin=True))
        with self.assertRaisesRegex(WebSocketProtocolError, "message exceeds"):
            decoder.next_event()
        self.assertEqual(validate_close_payload((1000).to_bytes(2, "big") + b"bye"), (1000, "bye"))
        for payload in (b"x", (1005).to_bytes(2, "big"), (1000).to_bytes(2, "big") + b"\xff"):
            with self.subTest(payload=payload), self.assertRaises(WebSocketProtocolError):
                validate_close_payload(payload)
        with self.assertRaisesRegex(WebSocketProtocolError, "receive buffer exceeds"):
            decoder.feed(b"x" * (MAX_WEBSOCKET_MESSAGE_BYTES + 15))


if __name__ == "__main__":
    unittest.main()
