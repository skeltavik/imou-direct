"""Config flow for Imou Direct."""

from __future__ import annotations

import shutil
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_NAME, CONF_PASSWORD
from homeassistant.helpers import selector

from .bootstrap import bootstrap_from_device
from .cloud import (
    ImouCannotConnect,
    ImouCloudClient,
    ImouCloudError,
    ImouDevice,
    ImouInvalidAuth,
    ImouProtocolError,
)
from .const import (
    CONF_ACCOUNT,
    CONF_BOOTSTRAP,
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_FFMPEG_BIN,
    CONF_WIDTH,
    DEFAULT_COUNTRY,
    DEFAULT_FFMPEG_BIN,
    DEFAULT_NAME,
    DEFAULT_WIDTH,
    DOMAIN,
    MAX_WIDTH,
    MIN_WIDTH,
)
from .manager import validate_bootstrap


class FfmpegNotFoundError(Exception):
    """Raised when the configured FFmpeg executable is unavailable."""


def _discover_devices(
    account: str, password: str, country: str
) -> list[tuple[ImouDevice, dict[str, Any]]]:
    """Log in once and create password-free bootstraps for supported devices."""
    client = ImouCloudClient(country=country)
    session = client.login(account, password)
    supported: list[tuple[ImouDevice, dict[str, Any]]] = []
    for device in client.list_devices(session):
        try:
            bootstrap = validate_bootstrap(bootstrap_from_device(session, device))
        except ImouProtocolError:
            continue
        supported.append((device, bootstrap))
    return supported


def _validate_ffmpeg(ffmpeg_bin: str) -> None:
    if shutil.which(ffmpeg_bin) is None:
        raise FfmpegNotFoundError


def _country(value: str) -> str:
    country = value.strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise vol.Invalid("country must be a two-letter code")
    return country


class ImouDirectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an Imou Direct config flow."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._devices: dict[str, tuple[ImouDevice, dict[str, Any]]] = {}
        self._settings: dict[str, Any] = {}

    async def _create_device_entry(self, device_id: str) -> ConfigFlowResult:
        device, bootstrap = self._devices[device_id]
        await self.async_set_unique_id(device.device_id)
        self._abort_if_unique_id_configured()
        data = {**self._settings, CONF_BOOTSTRAP: bootstrap}
        self._devices = {}
        self._settings = {}
        return self.async_create_entry(title=data[CONF_NAME], data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                country = _country(user_input[CONF_COUNTRY])
                await self.hass.async_add_executor_job(
                    _validate_ffmpeg, DEFAULT_FFMPEG_BIN
                )
                devices = await self.hass.async_add_executor_job(
                    _discover_devices,
                    user_input[CONF_ACCOUNT],
                    user_input[CONF_PASSWORD],
                    country,
                )
            except vol.Invalid:
                errors[CONF_COUNTRY] = "invalid_country"
            except FfmpegNotFoundError:
                errors["base"] = "ffmpeg_missing"
            except ImouInvalidAuth:
                errors["base"] = "invalid_auth"
            except ImouCannotConnect:
                errors["base"] = "cannot_connect"
            except (ImouCloudError, OSError, ValueError, KeyError):
                errors["base"] = "unknown"
            else:
                if not devices:
                    errors["base"] = "no_supported_devices"
                else:
                    self._settings = {
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_COUNTRY: country,
                        CONF_WIDTH: user_input[CONF_WIDTH],
                    }
                    self._devices = {
                        device.device_id: (device, bootstrap)
                        for device, bootstrap in devices
                    }
                    if len(self._devices) == 1:
                        return await self._create_device_entry(next(iter(self._devices)))
                    return await self.async_step_device()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_ACCOUNT): selector.TextSelector(
                    selector.TextSelectorConfig()
                ),
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): selector.TextSelector(
                    selector.TextSelectorConfig()
                ),
                vol.Required(CONF_WIDTH, default=DEFAULT_WIDTH): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_WIDTH, max=MAX_WIDTH)
                ),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user select one supported Imou camera."""
        if not self._devices:
            return self.async_abort(reason="discovery_expired")
        if user_input is not None:
            return await self._create_device_entry(user_input[CONF_DEVICE_ID])

        choices = {
            device_id: (
                device.name
                if not device.catalog
                else f"{device.name} ({device.catalog})"
            )
            for device_id, (device, _bootstrap) in self._devices.items()
        }
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=value, label=label)
                                for value, label in choices.items()
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Refresh the private device session without storing account credentials."""
        entry = self._get_reconfigure_entry()
        ffmpeg_bin = entry.data.get(CONF_FFMPEG_BIN, DEFAULT_FFMPEG_BIN)
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                country = _country(user_input[CONF_COUNTRY])
                await self.hass.async_add_executor_job(
                    _validate_ffmpeg, ffmpeg_bin
                )
                devices = await self.hass.async_add_executor_job(
                    _discover_devices,
                    user_input[CONF_ACCOUNT],
                    user_input[CONF_PASSWORD],
                    country,
                )
            except vol.Invalid:
                errors[CONF_COUNTRY] = "invalid_country"
            except FfmpegNotFoundError:
                errors["base"] = "ffmpeg_missing"
            except ImouInvalidAuth:
                errors["base"] = "invalid_auth"
            except ImouCannotConnect:
                errors["base"] = "cannot_connect"
            except (ImouCloudError, OSError, ValueError, KeyError):
                errors["base"] = "unknown"
            else:
                existing = entry.data.get(CONF_BOOTSTRAP, {})
                request = existing.get("request", {}) if isinstance(existing, dict) else {}
                target_id = str(request.get("deviceId") or entry.unique_id or "")
                selected = next(
                    (
                        (device, bootstrap)
                        for device, bootstrap in devices
                        if device.device_id == target_id
                    ),
                    None,
                )
                if selected is None and len(devices) == 1:
                    selected = devices[0]
                if selected is None:
                    errors["base"] = "device_not_found"
                else:
                    device, bootstrap = selected
                    data = {
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_COUNTRY: country,
                        CONF_WIDTH: user_input[CONF_WIDTH],
                        CONF_BOOTSTRAP: bootstrap,
                    }
                    if CONF_FFMPEG_BIN in entry.data:
                        data[CONF_FFMPEG_BIN] = ffmpeg_bin
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=device.device_id,
                        title=user_input[CONF_NAME],
                        data=data,
                    )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=entry.data.get(CONF_NAME, entry.title)
                ): str,
                vol.Required(CONF_ACCOUNT): selector.TextSelector(
                    selector.TextSelectorConfig()
                ),
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(
                    CONF_COUNTRY, default=entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
                ): selector.TextSelector(selector.TextSelectorConfig()),
                vol.Required(
                    CONF_WIDTH, default=entry.data.get(CONF_WIDTH, DEFAULT_WIDTH)
                ): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_WIDTH, max=MAX_WIDTH)
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )
