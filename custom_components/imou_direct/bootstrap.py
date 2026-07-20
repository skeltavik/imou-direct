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


def _optional_object(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    try:
        return _object(value, "related device")
    except ImouProtocolError:
        return None


def _credentials(
    media: dict[str, Any], raw: dict[str, Any], device_id: str
) -> tuple[str, str, str]:
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
    return username, password, wsse_key


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


def bootstrap_from_device(
    session: ImouSession,
    device: ImouDevice,
    devices: list[ImouDevice] | None = None,
) -> dict[str, Any]:
    """Build the minimal stream configuration without retaining account password."""
    raw = device.raw
    channel, media = _media_source(raw)
    device_id = str(channel.get("deviceId") or device.device_id)
    product_id = str(channel.get("productId") or device.product_id)
    channel_id = str(channel.get("channelId") or "0")
    if not product_id:
        raise ImouProtocolError("The selected device has no product identifier")

    username, password, wsse_key = _credentials(media, raw, device_id)

    related = _optional_object(raw.get("relatedDeviceInfo"))
    main_raw = raw
    main_media = media
    main_device_id = device_id
    main_product_id = product_id
    if related is not None and related.get("deviceId"):
        main_device_id = str(related["deviceId"])
        main_product_id = str(related.get("productId") or product_id)
        related_media = _optional_object(related.get("mediaConfig"))
        if related_media is not None:
            main_media = related_media
        match = next(
            (
                candidate
                for candidate in devices or []
                if candidate.device_id == main_device_id
            ),
            None,
        )
        if match is not None:
            main_raw = match.raw
            main_product_id = str(related.get("productId") or match.product_id)
            try:
                _main_channel, main_media = _media_source(main_raw)
            except ImouProtocolError:
                if related_media is None:
                    raise
    lan_username, lan_password, lan_wsse_key = _credentials(
        main_media, main_raw, main_device_id
    )

    p2p = main_raw.get("p2pConfig")
    if not isinstance(p2p, dict):
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
            "device_id": main_device_id,
            "product_id": main_product_id,
            "bind_device_id": device_id,
            "bind_product_id": product_id,
            "bind_channel_id": str(
                int(channel_id) + (1 if main_device_id != device_id else 0)
            ),
            "p2p_type": int(p2p.get("type") or 0),
            "p2p_port": int(p2p.get("port") or 8086),
            "device_username": lan_username,
            "device_password": lan_password,
            "wsse_key": lan_wsse_key,
            "dev_p2p_ak": linked_ak,
            "dev_p2p_sk": p2p_secret,
            "encrypt": int(main_media.get("streamEncryModel", 3)),
            "image_size": _image_size(main_media),
            "audio_type": 1,
            "subtype": 0,
            "shared_link": bool(main_media.get("isSupportShareLink", True)),
        },
    }
