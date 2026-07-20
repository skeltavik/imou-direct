"""Tests for the password-free Imou account bootstrap."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import sys
import unittest

_MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "imou_direct" / "cloud.py"
)
_SPEC = importlib.util.spec_from_file_location("imou_direct_cloud", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CLOUD = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CLOUD
_SPEC.loader.exec_module(_CLOUD)

CONTENT_TYPE = _CLOUD.CONTENT_TYPE
ImouCloudClient = _CLOUD.ImouCloudClient
_canonical = _CLOUD._canonical
build_client_ua = _CLOUD.build_client_ua


class _Response:
    def __init__(self, payload: dict) -> None:
        self.status = 200
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class CloudTests(unittest.TestCase):
    def test_client_ua_contains_no_account_data(self) -> None:
        encoded = build_client_ua(
            "terminal", country="BE", language="nl_BE", timezone_offset=3600
        )
        descriptor = json.loads(base64.b64decode(encoded))
        self.assertEqual(descriptor["terminalId"], "terminal")
        self.assertEqual(descriptor["appid"], "easy4ipbaseapp")
        self.assertEqual(descriptor["timezoneOffset"], "3600")
        self.assertNotIn("account", descriptor)
        self.assertNotIn("password", descriptor)

    def test_canonical_header_order(self) -> None:
        canonical = _canonical(
            uri="/pcs/v1/example",
            digest="digest",
            revision="191204",
            client_ua="ua",
            date="2026-07-20T12:00:00Z",
            nonce="nonce",
            username="token/user",
            session_id="session",
        )
        self.assertEqual(
            canonical,
            (
                "POST\n/pcs/v1/example\ndigest\n"
                f"{CONTENT_TYPE}\n"
                "x-pcs-apiver:191204\n"
                "x-pcs-client-ua:ua\n"
                "x-pcs-date:2026-07-20T12:00:00Z\n"
                "x-pcs-nonce:nonce\n"
                "x-pcs-session-id:session\n"
                "x-pcs-username:token/user\n"
            ).encode(),
        )

    def test_login_discards_password_and_lists_devices(self) -> None:
        requests = []
        responses = iter(
            [
                {
                    "code": 10000,
                    "desc": "OK",
                    "data": {
                        "username": "123",
                        "token": "session-token",
                        "sessionId": "session-id",
                        "userId": 123,
                        "entryUrlV2": "entry.example:443",
                    },
                },
                {
                    "code": 10000,
                    "desc": "OK",
                    "data": {"country": "BE"},
                },
                {
                    "code": 10000,
                    "desc": "OK",
                    "data": {
                        "deviceList": [
                            {
                                "deviceId": "ABC",
                                "productId": "PID",
                                "name": "Doorbell",
                                "catalog": "Doorbell",
                                "status": "online",
                                "devicePassword": "local-only",
                            }
                        ]
                    },
                },
            ]
        )

        def urlopen(request, *, timeout):
            requests.append((request, timeout))
            return _Response(next(responses))

        client = ImouCloudClient(
            terminal_id="terminal",
            country="BE",
            timezone_offset=3600,
            urlopen=urlopen,
        )
        session = client.login("owner@example.test", "private-password")
        devices = client.list_devices(session)

        self.assertEqual(session.username, "token/123")
        self.assertEqual(session.token, "session-token")
        self.assertEqual(session.user_id, 123)
        self.assertFalse(hasattr(session, "password"))
        self.assertEqual(devices[0].device_id, "ABC")
        self.assertEqual(len(requests), 3)
        self.assertNotIn(b"private-password", requests[0][0].data)
        self.assertEqual(json.loads(requests[2][0].data)["data"]["needNewSecret"], True)


if __name__ == "__main__":
    unittest.main()
