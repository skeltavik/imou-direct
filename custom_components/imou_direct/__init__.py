"""Imou Direct Home Assistant integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import (
    CONF_BOOTSTRAP,
    CONF_CONFIG_PATH,
    CONF_FFMPEG_BIN,
    CONF_TRANSPORT_MODE,
    CONF_WIDTH,
    DEFAULT_FFMPEG_BIN,
    DEFAULT_TRANSPORT_MODE,
    PLATFORMS,
)
from .manager import DirectStreamManager, validate_bootstrap, validate_bootstrap_file

type ImouDirectConfigEntry = ConfigEntry[DirectStreamManager]


async def async_setup_entry(
    hass: HomeAssistant, entry: ImouDirectConfigEntry
) -> bool:
    """Set up Imou Direct from a config entry."""
    manager: DirectStreamManager | None = None
    try:
        if CONF_BOOTSTRAP in entry.data:
            config = validate_bootstrap(entry.data[CONF_BOOTSTRAP])
        else:
            config = await hass.async_add_executor_job(
                validate_bootstrap_file, entry.data[CONF_CONFIG_PATH]
            )
        config.setdefault("output", {})["width"] = entry.data[CONF_WIDTH]
        config["output"]["transport_mode"] = entry.data.get(
            CONF_TRANSPORT_MODE, DEFAULT_TRANSPORT_MODE
        )
        manager = DirectStreamManager(
            config, entry.data.get(CONF_FFMPEG_BIN, DEFAULT_FFMPEG_BIN)
        )
        await hass.async_add_executor_job(manager.start)
    except (OSError, ValueError, KeyError) as error:
        if manager is not None:
            await hass.async_add_executor_job(manager.stop)
        raise ConfigEntryError("Unable to start Imou Direct") from error

    entry.runtime_data = manager
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await hass.async_add_executor_job(manager.stop)
        raise
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ImouDirectConfigEntry
) -> bool:
    """Unload an Imou Direct config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await hass.async_add_executor_job(entry.runtime_data.stop)
    return True
