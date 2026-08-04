"""Offline tests for Home Assistant config-entry lifecycle cleanup."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest

_COMPONENT = Path(__file__).parents[1] / "custom_components" / "imou_direct"


class _ConfigEntry:
    def __class_getitem__(cls, _item):
        return cls


class _ConfigEntryError(Exception):
    pass


def _load_integration():
    homeassistant = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = _ConfigEntry
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryError = _ConfigEntryError
    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.config_entries": config_entries,
            "homeassistant.core": core,
            "homeassistant.exceptions": exceptions,
        }
    )

    package = types.ModuleType("imou_direct_init_test")
    package.__path__ = [str(_COMPONENT)]
    sys.modules[package.__name__] = package

    const_spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.const", _COMPONENT / "const.py"
    )
    assert const_spec is not None and const_spec.loader is not None
    const = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const
    const_spec.loader.exec_module(const)

    manager = types.ModuleType(f"{package.__name__}.manager")
    manager.DirectStreamManager = object
    manager.validate_bootstrap = lambda config: {"output": {}}
    manager.validate_bootstrap_file = lambda path: {"output": {}}
    sys.modules[manager.__name__] = manager

    spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.integration", _COMPONENT / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    integration = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = integration
    spec.loader.exec_module(integration)
    return const, manager, integration


_CONST, _MANAGER, _INTEGRATION = _load_integration()


class SetupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_manager_startup_exception_stops_manager(self) -> None:
        class FailingManager:
            instances: list[FailingManager] = []

            def __init__(self, _config, _ffmpeg_bin) -> None:
                self.stopped = False
                self.instances.append(self)

            def start(self) -> None:
                raise RuntimeError("synthetic startup failure")

            def stop(self) -> None:
                self.stopped = True

        class Hass:
            async def async_add_executor_job(self, function, *args):
                return function(*args)

        entry = types.SimpleNamespace(
            data={
                _CONST.CONF_BOOTSTRAP: {},
                _CONST.CONF_WIDTH: 960,
            }
        )
        original_manager = _INTEGRATION.DirectStreamManager
        original_validate = _INTEGRATION.validate_bootstrap
        _INTEGRATION.DirectStreamManager = FailingManager
        _INTEGRATION.validate_bootstrap = lambda _config: {"output": {}}
        try:
            with self.assertRaisesRegex(
                _ConfigEntryError, "Unable to start"
            ) as raised:
                await _INTEGRATION.async_setup_entry(Hass(), entry)
        finally:
            _INTEGRATION.DirectStreamManager = original_manager
            _INTEGRATION.validate_bootstrap = original_validate

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertTrue(FailingManager.instances[0].stopped)


if __name__ == "__main__":
    unittest.main()
