"""Constants for the Imou Direct integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "imou_direct"
PLATFORMS: Final = ["camera"]

CONF_CONFIG_PATH: Final = "config_path"
CONF_ACCOUNT: Final = "account"
CONF_BOOTSTRAP: Final = "bootstrap"
CONF_COUNTRY: Final = "country"
CONF_DEVICE_ID: Final = "device_id"
CONF_FFMPEG_BIN: Final = "ffmpeg_bin"
CONF_WIDTH: Final = "width"

DEFAULT_CONFIG_PATH: Final = "/config/imou_direct.json"
DEFAULT_COUNTRY: Final = "BE"
DEFAULT_FFMPEG_BIN: Final = "ffmpeg"
DEFAULT_NAME: Final = "Imou Doorbell"
DEFAULT_WIDTH: Final = 960

MIN_WIDTH: Final = 320
MAX_WIDTH: Final = 1920
