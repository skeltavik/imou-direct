"""Camera entity for Imou Direct."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ImouDirectConfigEntry
from .const import DOMAIN
from .manager import DirectStreamManager

SCAN_INTERVAL = timedelta(seconds=10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ImouDirectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Imou Direct camera entity."""
    async_add_entities([ImouDirectCamera(entry, entry.runtime_data)])


class ImouDirectCamera(Camera):
    """Expose the direct decrypted stream through Home Assistant."""

    _attr_brand = "Imou"
    _attr_has_entity_name = True
    _attr_model = "Direct encrypted transfer stream"
    _attr_name = None
    _attr_should_poll = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self, entry: ImouDirectConfigEntry, manager: DirectStreamManager
    ) -> None:
        super().__init__()
        self._manager = manager
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Imou",
            model="Direct encrypted transfer stream",
            name=entry.data[CONF_NAME],
        )

    @property
    def available(self) -> bool:
        """Return whether the stream transport is currently connected."""
        health = self._manager.health()
        return bool(health["connected"])

    @property
    def is_streaming(self) -> bool:
        """Return whether frames are arriving."""
        health = self._manager.health()
        age = health["last_frame_age_seconds"]
        return bool(health["connected"] and age is not None and age < 15)

    @property
    def extra_state_attributes(self) -> dict[str, float | int | None]:
        """Expose only non-sensitive health metrics."""
        health = self._manager.health()
        return {
            "last_frame_age_seconds": health["last_frame_age_seconds"],
            "reconnects": health["reconnects"],
        }

    async def stream_source(self) -> str | None:
        """Return a loopback HLS URL consumable by Home Assistant FFmpeg."""
        ready = await self.hass.async_add_executor_job(
            self._manager.wait_stream_ready, 9.0
        )
        return self._manager.stream_url if ready else None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest generated JPEG snapshot."""
        return await self.hass.async_add_executor_job(self._manager.snapshot_bytes)
