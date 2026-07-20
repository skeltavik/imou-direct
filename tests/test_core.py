"""Tests for a capture-free Imou PLAY request."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
from pathlib import Path
import re
import unittest

_MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "imou_direct" / "core.py"
_SPEC = importlib.util.spec_from_file_location("imou_direct_core", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CORE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CORE)


class CoreTests(unittest.TestCase):
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

    def test_play_response_accepts_both_length_headers(self) -> None:
        self.assertEqual(
            _CORE._play_response_length(b"HTTP/1.1 200 OK\r\nPrivate-Length: 717"),
            717,
        )
        self.assertEqual(
            _CORE._play_response_length(b"HTTP/1.1 200 OK\r\nContent-Length: 644"),
            644,
        )

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
