"""Turn an authenticated Imou device response into a private local bootstrap."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .cloud import API_REVISION, CONTENT_TYPE, ImouDevice, ImouProtocolError, ImouSession

TRANSFER_API = "things.media.GetRealTransferStreamUrl"
TRANSFER_URI = "/pcs/v1/" + TRANSFER_API
P2P_LINK_HOST = "das.easy4ipcloud.com"


def _object(value: Any, name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ImouProtocolError(f"Imou returned an invalid {name}") from error
        if isinstance(parsed, dict):
            return parsed
    raise ImouProtocolError(f"Imou returned an invalid {name}")


def _decrypt_device_value(value: str, device_id: str) -> str:
    """Decrypt a `*New` media field using Imou's per-device AES-GCM key."""
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ImouProtocolError("Imou returned an invalid device credential") from error
    if len(payload) <= 28:
        raise ImouProtocolError("Imou returned an invalid device credential")
    key = hashlib.sha256((device_id + "ENCRYPTKEY").encode()).digest()
    try:
        return AESGCM(key).decrypt(payload[:12], payload[12:], None).decode()
    except (InvalidTag, UnicodeDecodeError) as error:
        raise ImouProtocolError("Unable to decrypt an Imou device credential") from error


def _media_source(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    channels = raw.get("channelList")
    if isinstance(channels, list):
        for channel in channels:
            if isinstance(channel, dict) and channel.get("mediaConfig"):
                return channel, _object(channel["mediaConfig"], "media configuration")
    if raw.get("mediaConfig"):
        return raw, _object(raw["mediaConfig"], "media configuration")
    raise ImouProtocolError("The selected device has no media configuration")


def _p2p_secret(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ImouProtocolError("The selected device has no P2P token")
    return value


def _image_size(media: dict[str, Any]) -> int:
    clarities = media.get("streamClarity")
    if isinstance(clarities, list):
        candidates = [item for item in clarities if isinstance(item, dict)]
        for item in candidates:
            if item.get("isDefault") and isinstance(item.get("imageSize"), int):
                return item["imageSize"]
        for item in candidates:
            if item.get("streamChannel") == 0 and isinstance(
                item.get("imageSize"), int
            ):
                return item["imageSize"]
    return 89


def bootstrap_from_device(session: ImouSession, device: ImouDevice) -> dict[str, Any]:
    """Build the minimal stream configuration without retaining account password."""
    raw = device.raw
    channel, media = _media_source(raw)
    device_id = str(channel.get("deviceId") or device.device_id)
    product_id = str(channel.get("productId") or device.product_id)
    channel_id = str(channel.get("channelId") or "0")
    if not product_id:
        raise ImouProtocolError("The selected device has no product identifier")

    username_value = media.get("deviceAccountNew")
    password_value = media.get("devicePasswordNew")
    username = (
        _decrypt_device_value(username_value, device_id)
        if isinstance(username_value, str) and username_value
        else str(raw.get("deviceUsername") or "")
    )
    password = (
        _decrypt_device_value(password_value, device_id)
        if isinstance(password_value, str) and password_value
        else str(raw.get("devicePassword") or "")
    )
    wsse_value = media.get("wssekeyNew")
    wsse_key = (
        _decrypt_device_value(wsse_value, device_id)
        if isinstance(wsse_value, str) and wsse_value
        else ""
    )
    if not username or not password:
        raise ImouProtocolError("The selected device has no stream credentials")

    p2p = raw.get("p2pConfig")
    if not isinstance(p2p, dict):
        raise ImouProtocolError("The selected device has no P2P configuration")
    p2p_token = p2p.get("p2pToken")
    p2p_secret = _p2p_secret(p2p_token)
    p2p_ak = str(p2p.get("ak") or "")
    linked_ak = (
        f"Link\\v2\\{P2P_LINK_HOST}\\phone\\easy4ipbaseapp\\"
        f"{session.user_id}\\{p2p_ak}"
        if p2p_ak
        else ""
    )

    return {
        "rest": {
            "host": session.host,
            "uri": TRANSFER_URI,
            "method": "POST",
            "content_type": CONTENT_TYPE,
            "revision": API_REVISION,
            "client_ua": session.client_ua,
            "username": session.username,
            "md5_key": session.token,
            "sha256_key": session.token,
            "session_id": session.session_id,
            "request_id": secrets.token_hex(32),
            "region": "",
            "file_crypt_version": "",
        },
        "request": {
            "assistStream": "false",
            "audioType": 1,
            "channelId": channel_id,
            "design": "second",
            "deviceId": device_id,
            "encrypt": str(media.get("streamEncryModel", 3)),
            "imageSize": _image_size(media),
            "productId": product_id,
            "skipAuth": "",
            "streamId": "0",
            "timeLimit": False,
            "videoLimit": 0,
            "owner": "",
            "ownerType": "base",
            "type": "RTSV1",
        },
        "stream": {
            "username": username,
            "password": password,
            "wsse_key": wsse_key,
            "device_sn": device_id,
        },
        "lan": {
            "device_id": device_id,
            "product_id": product_id,
            "channel_id": channel_id,
            "p2p_type": int(p2p.get("type") or 0),
            "p2p_port": int(p2p.get("port") or 0),
            "device_username": username,
            "device_password": password,
            "dev_p2p_ak": linked_ak,
            "dev_p2p_sk": p2p_secret,
        },
    }
