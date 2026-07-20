"""Safe diagnostics for Imou Direct."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from . import ImouDirectConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ImouDirectConfigEntry
) -> dict:
    """Return runtime health without paths, credentials, URLs, or device IDs."""
    return {"stream": entry.runtime_data.health()}
