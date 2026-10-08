"""Offline tests for the capture-free Imou LAN transport."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import importlib.util
import re
import sys
import types
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

_COMPONENT = Path(__file__).parents[1] / "custom_components" / "imou_direct"
_PACKAGE = types.ModuleType("imou_direct_lan_test")
_PACKAGE.__path__ = [str(_COMPONENT)]
sys.modules[_PACKAGE.__name__] = _PACKAGE


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE.__name__}.{name}", _COMPONENT / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("core")
_LAN = _load("lan")


def _config() -> dict:
    return {
        "device_id": "MAINDEVICE12345",
        "product_id": "MAINPROD",
        "bind_device_id": "CHILDDEVICE1234",
        "bind_product_id": "CHILDPR",
        "bind_channel_id": "0",
        "device_username": "admin",
        "device_password": "device-password",
        "wsse_key": "main-wsse",
        "dev_p2p_ak": "Link\\v2\\example\\phone\\app\\1\\key",
        "dev_p2p_sk": "synthetic-p2p-secret",
        "p2p_port": 8086,
        "encrypt": 3,
        "image_size": 41,
        "shared_link": True,
    }


class LanTests(unittest.TestCase):
    def test_local_channel_request_has_fresh_valid_authentication(self) -> None:
        lan = _config()
        moment = dt.datetime(2026, 7, 20, 12, 34, 56, tzinfo=dt.timezone.utc)
        request = _LAN.build_local_channel_request(
            lan,
            now=moment,
            body_nonce=1234,
            wsse_nonce=5678,
            rand_salt="0011223344556677",
            request_id="a" * 32,
            cseq=42,
        )
        head, separator, body = request.partition(b"\r\n\r\n")
        self.assertTrue(separator)
        text = head.decode()
        self.assertIn(
            "NFPOST /device/MAINPROD@MAINDEVICE12345/local-channel HTTP/1.1",
            text,
        )
        self.assertIn(f"Content-Length: {len(body)}", text)
        self.assertNotIn(lan["device_password"], text)
        fields = {child.tag: child.text or "" for child in ET.fromstring(body)}
        key = hashlib.md5(
            b"admin:Login to 0011223344556677:device-password",
            usedforsecurity=False,
        ).hexdigest().upper().encode()
        expected_device_auth = base64.b64encode(
            hmac.new(key, b"12341784550896", hashlib.sha256).digest()
        ).decode()
        self.assertEqual(fields["DevAuth"], expected_device_auth)

        captured_digest = re.search(r'PasswordDigest="([^"]+)"', text)
        self.assertIsNotNone(captured_digest)
        expected_wsse = base64.b64encode(
            hashlib.sha1(
                b"56782026-07-20T12:34:56+00:00synthetic-p2p-secret",
                usedforsecurity=False,
            ).digest()
        ).decode()
        self.assertEqual(captured_digest.group(1), expected_wsse)

    def test_local_play_request_uses_child_bound_optional_route(self) -> None:
        lan = _config()

        request = _LAN.build_local_play_request(lan)

        first_line = request.split(b"\r\n", 1)[0].decode()
        _method, target, _version = first_line.split(" ")
        parsed = urllib.parse.urlsplit(target)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(parsed.path, "/live/visualtalk.xav")
        self.assertEqual(query["device"], lan["bind_device_id"])
        self.assertEqual(query["channel"], "1")
        self.assertEqual(query["trackID"], "31")
        self.assertEqual(query["method"], "0")
        self.assertNotIn(lan["device_password"].encode(), request)

    def test_ptcp_session_tracks_counters_and_deduplicates(self) -> None:
        session = _LAN._PTCPSession()
        session.local_id = 10

        sync = session.packet(b"\x00\x03\x01\x00", sync=True)
        self.assertEqual(int.from_bytes(sync[12:16], "big"), 0x0002FFFF)
        self.assertEqual(int.from_bytes(sync[16:20], "big"), 10)
        self.assertEqual(session.sent, 4)

        peer = (
            b"PTCP"
            + (0).to_bytes(4, "big")
            + (4).to_bytes(4, "big")
            + (0x0002FFFF).to_bytes(4, "big")
            + (99).to_bytes(4, "big")
            + (10).to_bytes(4, "big")
            + b"\x00\x03\x01\x00"
        )
        body, duplicate = session.accept(peer)
        self.assertEqual(body, b"\x00\x03\x01\x00")
        self.assertFalse(duplicate)
        self.assertEqual(session.received, 4)
        _body, duplicate = session.accept(peer)
        self.assertTrue(duplicate)

    def test_pending_message_precedes_preconnection_control(self) -> None:
        self.assertFalse(
            _LAN._is_preconnection_control(b"\x00\x00\x00", False, True)
        )
        self.assertTrue(
            _LAN._is_preconnection_control(b"\x00\x00\x00", False, False)
        )

    def test_ptcp_message_framer_reassembles_fragmented_data(self) -> None:
        realm = 0x12345678
        frame = _LAN._ptcp_payload(realm, b"fragmented TLS record")
        framer = _LAN._PTCPMessageFramer(realm)

        self.assertEqual(framer.feed(frame[:7]), [])
        self.assertEqual(framer.feed(frame[7:15]), [])
        self.assertEqual(framer.feed(frame[15:]), [(0x10, b"fragmented TLS record")])

    def test_ptcp_message_framer_preserves_status_then_data(self) -> None:
        realm = 0x12345678
        body = _LAN._ptcp_status(realm, b"CONN") + _LAN._ptcp_payload(
            realm, b"TLS record"
        )

        self.assertEqual(
            _LAN._PTCPMessageFramer(realm).feed(body),
            [(0x12, b"CONN"), (0x10, b"TLS record")],
        )

    def test_ptcp_message_framer_preserves_data_then_status(self) -> None:
        realm = 0x12345678
        body = _LAN._ptcp_payload(realm, b"TLS record") + _LAN._ptcp_status(
            realm, b"DISC"
        )

        self.assertEqual(
            _LAN._PTCPMessageFramer(realm).feed(body),
            [(0x10, b"TLS record"), (0x12, b"DISC")],
        )

    def test_ptcp_message_framer_does_not_consume_status_as_pending_data(self) -> None:
        realm = 0x12345678
        data = _LAN._ptcp_payload(realm, b"fragmented")
        framer = _LAN._PTCPMessageFramer(realm)

        self.assertEqual(framer.feed(data[:15]), [])
        status = _LAN._ptcp_status(realm, b"CONN")
        self.assertEqual(framer.feed(status[:7]), [])
        self.assertEqual(framer.feed(status[7:]), [(0x12, b"CONN")])
        self.assertEqual(framer.feed(data[15:]), [(0x10, b"fragmented")])

    def test_ptcp_message_framer_restores_disproved_tentative_status_bytes(self) -> None:
        realm = 0x12345678
        payload = b"A\x12ZBC"
        frame = _LAN._ptcp_payload(realm, payload)
        framer = _LAN._PTCPMessageFramer(realm)

        self.assertEqual(framer.feed(frame[:13]), [])
        self.assertEqual(framer.feed(frame[13:14]), [])
        self.assertEqual(framer.feed(frame[14:]), [(0x10, payload)])

    def test_ptcp_message_framer_restores_full_false_status_prefix(self) -> None:
        realm = 0x12345678
        payload = b"A\x12\x00\x00\x00\x87\x65\x43\x21BC"
        frame = _LAN._ptcp_payload(realm, payload)
        framer = _LAN._PTCPMessageFramer(realm)

        self.assertEqual(framer.feed(frame[:13]), [])
        self.assertEqual(framer.feed(frame[13:21]), [])
        self.assertEqual(framer.feed(frame[21:]), [(0x10, payload)])

    def test_ptcp_message_framer_preserves_coalesced_data(self) -> None:
        realm = 0x12345678
        body = _LAN._ptcp_payload(realm, b"first") + _LAN._ptcp_payload(
            realm, b"second"
        )

        self.assertEqual(
            _LAN._PTCPMessageFramer(realm).feed(body),
            [(0x10, b"first"), (0x10, b"second")],
        )

    def test_ptcp_message_framer_rejects_invalid_realm(self) -> None:
        framer = _LAN._PTCPMessageFramer(0x12345678)

        with self.assertRaisesRegex(_LAN.LanP2PError, "invalid LAN PTCP payload"):
            framer.feed(_LAN._ptcp_payload(0x87654321, b"wrong realm"))

    def test_ptcp_message_framer_allows_bounded_data_larger_than_datagram(self) -> None:
        realm = 0x12345678
        payload = b"x" * (_LAN.MAX_DATAGRAM + 1)
        frame = _LAN._ptcp_payload(realm, payload)
        framer = _LAN._PTCPMessageFramer(realm)

        self.assertEqual(framer.feed(frame[: _LAN.MAX_DATAGRAM]), [])
        self.assertEqual(
            framer.feed(frame[_LAN.MAX_DATAGRAM :]), [(0x10, payload)]
        )

    def test_ptcp_message_framer_rejects_oversize_data(self) -> None:
        header = (
            (
                0x10000000
                | (_LAN.MAX_PLAY_HEADER + _LAN.MAX_PLAY_BODY + 1)
            ).to_bytes(4, "big")
            + (0x12345678).to_bytes(4, "big")
            + b"\x00" * 4
        )

        with self.assertRaisesRegex(_LAN.LanP2PError, "invalid LAN PTCP payload"):
            _LAN._PTCPMessageFramer(0x12345678).feed(header)


if __name__ == "__main__":
    unittest.main()
