"""Tests for deriving a private bootstrap from Imou device metadata."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_COMPONENT = Path(__file__).parents[1] / "custom_components" / "imou_direct"
_PACKAGE = types.ModuleType("imou_direct_test")
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


_CLOUD = _load("cloud")
_BOOTSTRAP = _load("bootstrap")


def _encrypted(value: str, device_id: str) -> str:
    key = hashlib.sha256((device_id + "ENCRYPTKEY").encode()).digest()
    nonce = bytes(range(12))
    encrypted = AESGCM(key).encrypt(nonce, value.encode(), None)
    return base64.b64encode(nonce + encrypted).decode()


class BootstrapTests(unittest.TestCase):
    def test_builds_stream_config_from_encrypted_device_fields(self) -> None:
        device_id = "DEVICE123456789"
        device = _CLOUD.ImouDevice(
            device_id=device_id,
            product_id="PRODUCT",
            name="Doorbell",
            catalog="Doorbell",
            status="online",
            raw={
                "deviceId": device_id,
                "productId": "PRODUCT",
                "p2pConfig": {
                    "ak": "device-ak",
                    "p2pToken": "11" * 32,
                    "port": 37777,
                    "type": 1,
                },
                "channelList": [
                    {
                        "channelId": "0",
                        "deviceId": device_id,
                        "productId": "PRODUCT",
                        "mediaConfig": {
                            "deviceAccountNew": _encrypted("admin", device_id),
                            "devicePasswordNew": _encrypted("local-pass", device_id),
                            "wssekeyNew": _encrypted("wsse-key", device_id),
                            "streamEncryModel": 3,
                            "streamClarity": [
                                {
                                    "imageSize": 89,
                                    "isDefault": True,
                                    "streamChannel": 0,
                                }
                            ],
                        },
                    }
                ],
            },
        )
        session = _CLOUD.ImouSession(
            host="https://entry.example:443",
            username="token/123",
            token="session-token",
            session_id="session-id",
            user_id=123,
            client_ua="client-ua",
            terminal_id="terminal-id",
            country="BE",
            timezone_offset=3600,
        )

        config = _BOOTSTRAP.bootstrap_from_device(session, device)

        self.assertEqual(config["stream"]["username"], "admin")
        self.assertEqual(config["stream"]["password"], "local-pass")
        self.assertEqual(config["stream"]["wsse_key"], "wsse-key")
        self.assertNotIn("transfer_hmac_key_hex", config["stream"])
        self.assertEqual(config["request"]["imageSize"], 89)
        self.assertEqual(
            config["lan"]["dev_p2p_ak"],
            "Link\\v2\\das.easy4ipcloud.com\\phone\\easy4ipbaseapp\\123\\device-ak",
        )
        self.assertEqual(config["lan"]["dev_p2p_sk"], "11" * 32)
        serialized = json.dumps(config)
        self.assertNotIn("owner@example.test", serialized)
        self.assertNotIn("account-password", serialized)


if __name__ == "__main__":
    unittest.main()
