"""Config flow for Imou Direct."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONFIG_PATH,
    CONF_FFMPEG_BIN,
    CONF_WIDTH,
    DEFAULT_CONFIG_PATH,
    DEFAULT_FFMPEG_BIN,
    DEFAULT_NAME,
    DEFAULT_WIDTH,
    DOMAIN,
    MAX_WIDTH,
    MIN_WIDTH,
)
from .manager import validate_bootstrap_file


class FfmpegNotFoundError(Exception):
    """Raised when the configured FFmpeg executable is unavailable."""


def _validate_user_input(hass: HomeAssistant, user_input: dict[str, Any]) -> dict:
    """Validate local paths without retaining bootstrap secrets."""
    path = str(Path(user_input[CONF_CONFIG_PATH]).expanduser().resolve())
    validate_bootstrap_file(path)
    ffmpeg_bin = user_input[CONF_FFMPEG_BIN]
    if shutil.which(ffmpeg_bin) is None:
        raise FfmpegNotFoundError
    return {**user_input, CONF_CONFIG_PATH: path}


class ImouDirectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an Imou Direct config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                validated = await self.hass.async_add_executor_job(
                    _validate_user_input, self.hass, user_input
                )
            except FfmpegNotFoundError:
                errors["base"] = "ffmpeg_missing"
            except FileNotFoundError:
                errors["base"] = "cannot_read"
            except (OSError, ValueError, KeyError):
                errors["base"] = "invalid_config"
            else:
                unique_id = hashlib.sha256(
                    validated[CONF_CONFIG_PATH].encode()
                ).hexdigest()
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=validated[CONF_NAME], data=validated
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_CONFIG_PATH, default=DEFAULT_CONFIG_PATH): str,
                vol.Required(CONF_FFMPEG_BIN, default=DEFAULT_FFMPEG_BIN): str,
                vol.Required(CONF_WIDTH, default=DEFAULT_WIDTH): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_WIDTH, max=MAX_WIDTH)
                ),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )
