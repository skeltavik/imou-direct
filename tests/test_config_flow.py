"""Tests for the password-free Home Assistant config entry."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

_COMPONENT = Path(__file__).parents[1] / "custom_components" / "imou_direct"


class _Selector:
    def __init__(self, config=None) -> None:
        self.config = config

    def __call__(self, value):
        return value


class _SelectorConfig(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


class _ConfigFlow:
    def __init_subclass__(cls, **_kwargs) -> None:
        super().__init_subclass__()

    def __init__(self) -> None:
        self.base_initialized = True

    async def async_set_unique_id(self, value: str) -> None:
        self.unique_id = value

    def _abort_if_unique_id_configured(self) -> None:
        return None

    def async_create_entry(self, *, title: str, data: dict) -> dict:
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, **kwargs) -> dict:
        return {"type": "form", **kwargs}

    def _get_reconfigure_entry(self):
        return self.reconfigure_entry

    def async_update_reload_and_abort(self, entry, **kwargs) -> dict:
        return {"type": "abort", "entry": entry, **kwargs}


class _Hass:
    async def async_add_executor_job(self, function, *args):
        return function(*args)


def _install_home_assistant_stubs() -> None:
    homeassistant = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlow = _ConfigFlow
    config_entries.ConfigFlowResult = dict
    constants = types.ModuleType("homeassistant.const")
    constants.CONF_NAME = "name"
    constants.CONF_PASSWORD = "password"
    helpers = types.ModuleType("homeassistant.helpers")
    selector = types.ModuleType("homeassistant.helpers.selector")
    selector.TextSelector = _Selector
    selector.TextSelectorConfig = _SelectorConfig
    selector.TextSelectorType = types.SimpleNamespace(PASSWORD="password")
    selector.SelectSelector = _Selector
    selector.SelectSelectorConfig = _SelectorConfig
    selector.SelectSelectorMode = types.SimpleNamespace(DROPDOWN="dropdown")
    selector.SelectOptionDict = lambda **kwargs: kwargs
    helpers.selector = selector
    homeassistant.config_entries = config_entries
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.config_entries": config_entries,
            "homeassistant.const": constants,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.selector": selector,
        }
    )


def _load_component() -> tuple[types.ModuleType, types.ModuleType]:
    package = types.ModuleType("imou_direct_flow_test")
    package.__path__ = [str(_COMPONENT)]
    sys.modules[package.__name__] = package

    def load(name: str) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location(
            f"{package.__name__}.{name}", _COMPONENT / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    cloud = load("cloud")
    load("bootstrap")
    load("core")
    load("manager")
    load("const")
    return cloud, load("config_flow")


_install_home_assistant_stubs()
_CLOUD, _FLOW = _load_component()


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_form_uses_serializable_country_selector(self) -> None:
        flow = _FLOW.ImouDirectConfigFlow()

        result = await flow.async_step_user()

        validators = {
            key.schema: value for key, value in result["data_schema"].schema.items()
        }
        self.assertIsInstance(validators["country"], _Selector)
        self.assertIsNot(validators["country"], _FLOW._country)
        self.assertEqual(
            validators["transport_mode"].config["translation_key"],
            "transport_mode",
        )
        self.assertNotIn("ffmpeg_bin", validators)

    async def test_invalid_country_is_reported_on_the_field(self) -> None:
        flow = _FLOW.ImouDirectConfigFlow()
        flow.hass = _Hass()

        result = await flow.async_step_user(
            {
                "name": "Front door",
                "account": "owner@example.test",
                "password": "account-password",
                "country": "Belgium",
                "transport_mode": "local_first",
                "width": 960,
            }
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"country": "invalid_country"})

    async def test_reconfigure_hides_but_preserves_legacy_ffmpeg_path(self) -> None:
        bootstrap = {
            "rest": {"host": "https://entry.example"},
            "request": {"deviceId": "DEVICE123"},
            "stream": {"password": "device-password"},
        }
        device = _CLOUD.ImouDevice(
            device_id="DEVICE123",
            product_id="PRODUCT",
            name="Doorbell",
            catalog="Doorbell",
            status="online",
            raw={},
        )
        flow = _FLOW.ImouDirectConfigFlow()
        flow.hass = _Hass()
        flow.reconfigure_entry = types.SimpleNamespace(
            data={
                "name": "Front door",
                "country": "BE",
                "ffmpeg_bin": "/legacy/ffmpeg",
                "width": 960,
                "bootstrap": bootstrap,
            },
            title="Front door",
            unique_id="DEVICE123",
        )

        form = await flow.async_step_reconfigure()
        validators = {
            key.schema: value for key, value in form["data_schema"].schema.items()
        }
        self.assertNotIn("ffmpeg_bin", validators)

        validated_ffmpeg = []
        old_discover = _FLOW._discover_devices
        old_ffmpeg = _FLOW._validate_ffmpeg
        _FLOW._discover_devices = lambda *_args: [(device, bootstrap)]
        _FLOW._validate_ffmpeg = validated_ffmpeg.append
        try:
            result = await flow.async_step_reconfigure(
                {
                    "name": "Front door",
                    "account": "owner@example.test",
                    "password": "account-password",
                    "country": "BE",
                    "transport_mode": "local_first",
                    "width": 960,
                }
            )
        finally:
            _FLOW._discover_devices = old_discover
            _FLOW._validate_ffmpeg = old_ffmpeg

        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["data"]["ffmpeg_bin"], "/legacy/ffmpeg")
        self.assertEqual(validated_ffmpeg, ["/legacy/ffmpeg"])

    async def test_account_password_is_not_stored(self) -> None:
        bootstrap = {
            "rest": {"host": "https://entry.example"},
            "request": {"deviceId": "DEVICE123"},
            "stream": {"password": "device-password"},
        }
        device = _CLOUD.ImouDevice(
            device_id="DEVICE123",
            product_id="PRODUCT",
            name="Doorbell",
            catalog="Doorbell",
            status="online",
            raw={},
        )
        old_discover = _FLOW._discover_devices
        old_ffmpeg = _FLOW._validate_ffmpeg
        discovered_with = []
        validated_ffmpeg = []

        def discover(*args):
            discovered_with.append(args)
            return [(device, bootstrap)]

        _FLOW._discover_devices = discover
        _FLOW._validate_ffmpeg = validated_ffmpeg.append
        try:
            flow = _FLOW.ImouDirectConfigFlow()
            self.assertTrue(flow.base_initialized)
            flow.hass = _Hass()
            result = await flow.async_step_user(
                {
                    "name": "Front door",
                    "account": "owner@example.test",
                    "password": "account-password",
                    "country": " be ",
                    "transport_mode": "local_first",
                    "width": 960,
                }
            )
        finally:
            _FLOW._discover_devices = old_discover
            _FLOW._validate_ffmpeg = old_ffmpeg

        self.assertEqual(result["type"], "create_entry")
        self.assertNotIn("account", result["data"])
        self.assertNotIn("password", result["data"])
        serialized = json.dumps(result["data"])
        self.assertNotIn("owner@example.test", serialized)
        self.assertNotIn("account-password", serialized)
        self.assertEqual(result["data"]["bootstrap"], bootstrap)
        self.assertEqual(result["data"]["country"], "BE")
        self.assertEqual(result["data"]["transport_mode"], "local_first")
        self.assertNotIn("ffmpeg_bin", result["data"])
        self.assertEqual(discovered_with[0][2], "BE")
        self.assertEqual(validated_ffmpeg, ["ffmpeg"])


if __name__ == "__main__":
    unittest.main()
