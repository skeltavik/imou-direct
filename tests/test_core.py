"""Tests for a capture-free Imou PLAY request."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import re
import threading
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "imou_direct" / "core.py"
_SPEC = importlib.util.spec_from_file_location("imou_direct_core", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CORE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CORE)


class CoreTests(unittest.TestCase):
    def test_runtime_api_host_rejects_legacy_unsafe_destinations(self) -> None:
        self.assertEqual(
            _CORE._normalise_api_host("https://entry.easy4ipcloud.com:443"),
            "https://entry.easy4ipcloud.com:443",
        )
        for value in (
            "http://entry.easy4ipcloud.com",
            "https://attacker.example",
            "https://169.254.169.254",
        ):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                _CORE._normalise_api_host(value)

    def test_extractor_accepts_local_four_byte_interleaved_lengths(self) -> None:
        payload = b"\x00\x00\x00\x01\x40\x01synthetic-hevc"
        frame_length = 24 + len(payload) + 8
        header = bytearray(24)
        header[:4] = b"DHAV"
        header[4] = 0xFD
        header[12:16] = frame_length.to_bytes(4, "little")
        frame = (
            bytes(header)
            + payload
            + b"dhav"
            + frame_length.to_bytes(4, "little")
        )
        packet = b"$\x02" + len(frame).to_bytes(4, "big") + frame
        extractor = _CORE.HevcExtractor(bytes(32))

        output = []
        for offset in range(0, len(packet), 7):
            output.extend(extractor.feed(packet[offset : offset + 7]))

        self.assertEqual(output, [payload])

    def test_extractor_accepts_short_rtp_when_sequence_and_timestamp_start_dhav(
        self,
    ) -> None:
        payload = b"\x00\x00\x00\x01\x40\x01synthetic-short-rtp"
        frame_length = 24 + len(payload) + 8
        header = bytearray(24)
        header[:4] = b"DHAV"
        header[4] = 0xFD
        header[12:16] = frame_length.to_bytes(4, "little")
        frame = (
            bytes(header)
            + payload
            + b"dhav"
            + frame_length.to_bytes(4, "little")
        )
        rtp = b"\x80\x62DHAV" + bytes(6) + frame
        packet = b"$\x02" + len(rtp).to_bytes(2, "big") + rtp
        extractor = _CORE.HevcExtractor(bytes(32))

        self.assertEqual(extractor.feed(packet), [payload])

    def test_extractor_accepts_extended_dhav_length_that_looks_like_rtp(self) -> None:
        frame_length = 0x00018062
        payload = b"\x00\x00\x00\x01\x40\x01" + bytes(frame_length - 38)
        header = bytearray(24)
        header[:4] = b"DHAV"
        header[4] = 0xFD
        header[12:16] = frame_length.to_bytes(4, "little")
        frame = (
            bytes(header)
            + payload
            + b"dhav"
            + frame_length.to_bytes(4, "little")
        )
        packet = b"$\x02" + len(frame).to_bytes(4, "big") + frame
        extractor = _CORE.HevcExtractor(bytes(32))

        self.assertEqual(extractor.feed(packet[:21]), [])
        self.assertEqual(extractor.feed(packet[21:]), [payload])

    def test_extractor_waits_for_large_extended_interleaved_length(self) -> None:
        payload = b"\x00\x00\x00\x01\x40\x01" + bytes(65_498)
        frame_length = 24 + len(payload) + 8
        header = bytearray(24)
        header[:4] = b"DHAV"
        header[4] = 0xFD
        header[12:16] = frame_length.to_bytes(4, "little")
        frame = (
            bytes(header)
            + payload
            + b"dhav"
            + frame_length.to_bytes(4, "little")
        )
        packet = b"$\x02" + len(frame).to_bytes(4, "big") + frame
        extractor = _CORE.HevcExtractor(bytes(32))

        self.assertEqual(extractor.feed(packet[:5]), [])
        self.assertEqual(extractor.feed(packet[5:]), [payload])

    def test_play_response_accepts_both_length_headers(self) -> None:
        self.assertEqual(
            _CORE._play_response_length(b"HTTP/1.1 200 OK\r\nPrivate-Length: 717"),
            717,
        )
        self.assertEqual(
            _CORE._play_response_length(b"HTTP/1.1 200 OK\r\nContent-Length: 644"),
            644,
        )

    def test_play_response_rejects_invalid_or_oversized_lengths(self) -> None:
        for value in (b"-1", b"invalid", str(_CORE.MAX_PLAY_BODY + 1).encode()):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                _CORE._play_response_length(
                    b"HTTP/1.1 200 OK\r\nPrivate-Length: " + value
                )

    def test_play_response_rejects_oversized_header(self) -> None:
        class FakeTls:
            def recv(self, _size: int) -> bytes:
                return b"X" * (_CORE.MAX_PLAY_HEADER + 1)

        with self.assertRaisesRegex(RuntimeError, "header is too large"):
            _CORE._read_play_response(FakeTls(), timeout=10, stop=None)

    def test_play_response_preserves_media_after_bounded_sdp(self) -> None:
        class FakeTls:
            def __init__(self) -> None:
                self.chunks = [
                    b"HTTP/1.1 200 OK\r\nPrivate-Length: 4\r\n\r\nSDP!media"
                ]

            def recv(self, _size: int) -> bytes:
                return self.chunks.pop(0)

        self.assertEqual(
            _CORE._read_play_response(FakeTls(), timeout=10, stop=None), b"media"
        )

    def test_play_response_honours_stop_before_receiving(self) -> None:
        stop = threading.Event()
        stop.set()

        class FakeTls:
            def recv(self, _size: int) -> bytes:
                raise AssertionError("recv must not run after stop")

        with self.assertRaisesRegex(RuntimeError, "stopped"):
            _CORE._read_play_response(FakeTls(), timeout=10, stop=stop)

    def test_play_response_enforces_absolute_deadline(self) -> None:
        class FakeTls:
            def recv(self, _size: int) -> bytes:
                raise AssertionError("recv must not run after the deadline")

        original = _CORE.time.monotonic
        moments = iter((100.0, 111.0))
        _CORE.time.monotonic = lambda: next(moments)
        try:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                _CORE._read_play_response(FakeTls(), timeout=10, stop=None)
        finally:
            _CORE.time.monotonic = original

    def test_generic_play_request_has_fresh_valid_digests(self) -> None:
        transfer_url = "stream.example:443/live.rtpxav?token=abc"
        request = _CORE.build_play_request(
            {
                "stream": {
                    "username": "admin",
                    "password": "local-pass",
                    "device_sn": "DEVICE123456789",
                    "wsse_key": "wsse-key",
                }
            },
            transfer_url,
        )
        head, separator, body = request.partition(b"\r\n\r\n")
        self.assertTrue(separator)
        text = head.decode()
        self.assertIn(
            "PLAY /live.rtpxav?token=abc&trackID=31&method=0 HTTP/1.1", text
        )
        self.assertIn("Host: stream.example:443", text)
        self.assertIn(f"Private-Length: {len(body)}", text)
        self.assertNotIn("local-pass", text)

        values = dict(
            re.findall(
                r'(PasswordDigest|LightweightDigest|Nonce|Created)="([^"]+)"',
                text,
            )
        )
        material = b"admin:Login to wsse-key:local-pass"
        md5_token = hashlib.md5(material, usedforsecurity=False).hexdigest().upper()
        sha256_token = hashlib.sha256(material).hexdigest().upper()
        expected_password = base64.b64encode(
            hashlib.sha1(
                (values["Nonce"] + values["Created"] + md5_token).encode(),
                usedforsecurity=False,
            ).digest()
        ).decode()
        expected_lightweight = base64.b64encode(
            hashlib.sha256(
                (values["Nonce"] + values["Created"] + sha256_token).encode()
            ).digest()
        ).decode()
        self.assertEqual(values["PasswordDigest"], expected_password)
        self.assertEqual(values["LightweightDigest"], expected_lightweight)


if __name__ == "__main__":
    unittest.main()
