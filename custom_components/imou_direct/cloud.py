"""Minimal Imou account bootstrap client.

The account password is only used while :meth:`ImouCloudClient.login` runs.  A
successful login returns a token-backed session, so callers never need to
persist the password.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
import datetime as dt
import hashlib
import hmac
import json
import secrets
import string
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_REVISION = "191204"
BOOTSTRAP_HOST = "https://app-v3.easy4ipcloud.com:443"
CONTENT_TYPE = "application/json; charset=utf-8"
SAAS_PREFIX = "/pcs/v1/"
SUCCESS_CODE = 10000


class ImouCloudError(Exception):
    """Base error for the one-time Imou account bootstrap."""


class ImouCannotConnect(ImouCloudError):
    """Raised when the Imou service cannot be reached."""


class ImouInvalidAuth(ImouCloudError):
    """Raised when Imou rejects the supplied account credentials."""


class ImouProtocolError(ImouCloudError):
    """Raised when Imou returns an unexpected response."""


@dataclass(frozen=True, slots=True)
class ImouSession:
    """Token-backed Imou session created by a successful account login."""

    host: str
    username: str
    token: str
    session_id: str
    user_id: int
    client_ua: str
    terminal_id: str
    country: str
    timezone_offset: int


@dataclass(frozen=True, slots=True)
class ImouDevice:
    """Device data needed to build a local connection bootstrap."""

    device_id: str
    product_id: str
    name: str
    catalog: str
    status: str
    raw: dict[str, Any]


UrlOpen = Callable[..., Any]


def new_terminal_id() -> str:
    """Return an anonymous, installation-local terminal identifier."""
    return uuid.uuid4().hex


def standard_timezone_offset() -> int:
    """Return the local standard UTC offset in seconds, matching Imou Life."""
    return -time.timezone


def build_client_ua(
    terminal_id: str,
    *,
    country: str = "BE",
    language: str = "nl_BE",
    timezone_offset: int | None = None,
) -> str:
    """Build the base64 JSON client descriptor expected by the Imou API."""
    offset = standard_timezone_offset() if timezone_offset is None else timezone_offset
    descriptor = {
        "clientType": "phone",
        "clientVersion": "V10.1.6",
        "clientOV": "Android 15",
        "clientOS": "Android",
        "terminalModel": "Home Assistant",
        "terminalId": terminal_id,
        "appid": "easy4ipbaseapp",
        "project": "Base",
        "language": language,
        "clientProtocolVersion": "V9.7.4",
        "timezoneOffset": str(offset),
        "terminalBrand": "Home Assistant",
        "terminalName": "Imou Direct",
        "country": country,
    }
    encoded = json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(encoded).decode()


def _normalise_host(value: str) -> str:
    host = value.strip()
    if not host:
        raise ImouProtocolError("Imou returned an empty API host")
    if "://" not in host:
        host = "https://" + host
    parsed = urllib.parse.urlsplit(host)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ImouProtocolError("Imou returned an invalid API host")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _digest(body: bytes, algorithm: str) -> str:
    digest = hashlib.new(
        algorithm, body, usedforsecurity=algorithm.lower() != "md5"
    ).digest()
    return base64.b64encode(digest).decode()


def _nonce() -> str:
    alphabet = string.ascii_letters + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(32))
    return f"{int(time.time() * 1000)}{suffix}"


def _canonical(
    *,
    uri: str,
    digest: str,
    revision: str,
    client_ua: str,
    date: str,
    nonce: str,
    username: str,
    session_id: str | None,
) -> bytes:
    lines = [
        "POST",
        uri,
        digest,
        CONTENT_TYPE,
        f"x-pcs-apiver:{revision}",
        f"x-pcs-client-ua:{client_ua}",
        f"x-pcs-date:{date}",
        f"x-pcs-nonce:{nonce}",
    ]
    if session_id:
        lines.append(f"x-pcs-session-id:{session_id}")
    lines.append(f"x-pcs-username:{username}")
    return ("\n".join(lines) + "\n").encode()


def _sign(key: str, canonical: bytes) -> str:
    signature = hmac.new(key.encode(), canonical, hashlib.sha256).digest()
    return base64.b64encode(signature).decode()


class ImouCloudClient:
    """Small synchronous client used from Home Assistant's executor."""

    def __init__(
        self,
        *,
        terminal_id: str | None = None,
        country: str = "BE",
        language: str = "nl_BE",
        timezone_offset: int | None = None,
        timeout: int = 15,
        urlopen: UrlOpen = urllib.request.urlopen,
    ) -> None:
        self.terminal_id = terminal_id or new_terminal_id()
        self.country = country.upper()
        self.timezone_offset = (
            standard_timezone_offset() if timezone_offset is None else timezone_offset
        )
        self.client_ua = build_client_ua(
            self.terminal_id,
            country=self.country,
            language=language,
            timezone_offset=self.timezone_offset,
        )
        self.timeout = timeout
        self._urlopen = urlopen

    def _call(
        self,
        *,
        host: str,
        api: str,
        data: dict[str, Any],
        username: str,
        md5_key: str,
        sha256_key: str,
        session_id: str | None = None,
        revision: str = API_REVISION,
        date_override: str | None = None,
        allow_date_retry: bool = True,
    ) -> dict[str, Any]:
        uri = SAAS_PREFIX + api
        body = json.dumps(
            {"data": data}, ensure_ascii=False, separators=(",", ":")
        ).encode()
        date = date_override or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = _nonce()
        md5_digest = _digest(body, "md5")
        sha256_digest = _digest(body, "sha256")
        common = {
            "uri": uri,
            "revision": revision,
            "client_ua": self.client_ua,
            "date": date,
            "nonce": nonce,
            "username": username,
            "session_id": session_id,
        }
        headers = {
            "Content-Type": CONTENT_TYPE,
            "Content-MD5": md5_digest,
            "Content-SHA256": sha256_digest,
            "x-pcs-signature": _sign(md5_key, _canonical(digest=md5_digest, **common)),
            "x-pcs-signature-sha256": _sign(
                sha256_key, _canonical(digest=sha256_digest, **common)
            ),
            "x-pcs-username": username,
            "x-pcs-apiver": revision,
            "x-pcs-nonce": nonce,
            "x-pcs-date": date,
            "x-pcs-client-ua": self.client_ua,
            "Accept-Encoding": "identity",
            "timeout": str(self.timeout * 1000),
        }
        if session_id:
            headers["x-pcs-session-id"] = session_id

        endpoint = _normalise_host(host) + uri
        request = urllib.request.Request(
            endpoint, data=body, headers=headers, method="POST"
        )
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                status = response.status
                response_body = response.read()
        except urllib.error.HTTPError as error:
            server_date = error.headers.get("x-pcs-date")
            if error.code == 412 and server_date and allow_date_retry:
                return self._call(
                    host=host,
                    api=api,
                    data=data,
                    username=username,
                    md5_key=md5_key,
                    sha256_key=sha256_key,
                    session_id=session_id,
                    revision=revision,
                    date_override=server_date,
                    allow_date_retry=False,
                )
            status = error.code
            response_body = error.read()
        except (OSError, TimeoutError) as error:
            raise ImouCannotConnect("Unable to reach the Imou service") from error

        try:
            payload = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ImouProtocolError(
                f"Imou returned non-JSON data (HTTP {status})"
            ) from error
        if not isinstance(payload, dict):
            raise ImouProtocolError("Imou returned an invalid response")
        code = payload.get("code")
        if code != SUCCESS_CODE:
            description = str(payload.get("desc") or "request rejected")[:160]
            if code == 22001 or api == "user.account.GetToken":
                raise ImouInvalidAuth(description)
            raise ImouCloudError(f"Imou API error {code}: {description}")
        response_data = payload.get("data")
        if not isinstance(response_data, dict):
            raise ImouProtocolError("Imou response has no data object")
        return response_data

    def login(self, account: str, password: str) -> ImouSession:
        """Authenticate once and return a password-free token session."""
        account = account.strip()
        if not account or not password:
            raise ImouInvalidAuth("Account and password are required")
        password_bytes = password.encode()
        password_md5 = hashlib.md5(
            password_bytes, usedforsecurity=False
        ).hexdigest()
        password_sha256 = hashlib.sha256(password_bytes).hexdigest()
        account_username = "account\\" + account

        token_data = self._call(
            host=BOOTSTRAP_HOST,
            api="user.account.GetToken",
            data={
                "areaCode": self.country,
                "gpsInfo": {"latitude": 0.0, "longitude": 0.0},
            },
            username=account_username,
            md5_key=password_md5,
            sha256_key=password_sha256,
        )
        if token_data.get("failNum") not in (None, "", 0, "0"):
            raise ImouInvalidAuth("Imou rejected the account credentials")
        try:
            token_username = "token/" + str(token_data["username"])
            token = str(token_data["token"])
            session_id = str(token_data["sessionId"])
            host = _normalise_host(str(token_data["entryUrlV2"]))
            user_id = int(token_data["userId"])
        except (KeyError, TypeError) as error:
            raise ImouProtocolError("Imou token response is incomplete") from error
        if not token or not session_id:
            raise ImouProtocolError("Imou token response is incomplete")

        login_data = self._call(
            host=host,
            api="user.account.Login",
            data={
                "avatarDigestType": "SHA256",
                "timezoneOffset": self.timezone_offset,
            },
            username=token_username,
            md5_key=token,
            sha256_key=token,
            session_id=session_id,
        )
        if login_data.get("entryUrlV2"):
            host = _normalise_host(str(login_data["entryUrlV2"]))
        return ImouSession(
            host=host,
            username=token_username,
            token=token,
            session_id=session_id,
            user_id=user_id,
            client_ua=self.client_ua,
            terminal_id=self.terminal_id,
            country=str(login_data.get("country") or self.country),
            timezone_offset=self.timezone_offset,
        )

    def list_devices(self, session: ImouSession) -> list[ImouDevice]:
        """Return devices plus the new local/P2P secret set supplied by Imou."""
        data = self._call(
            host=session.host,
            api="device.list.DeviceBasicInfoQueryV2",
            data={
                "groupId": "0",
                "offset": 0,
                "transferStr": "",
                "limit": 100,
                "needNewSecret": True,
            },
            username=session.username,
            md5_key=session.token,
            sha256_key=session.token,
            session_id=session.session_id,
        )
        raw_devices = data.get("deviceList")
        if not isinstance(raw_devices, list):
            raise ImouProtocolError("Imou device response has no device list")
        devices: list[ImouDevice] = []
        for raw in raw_devices:
            if not isinstance(raw, dict) or not raw.get("deviceId"):
                continue
            devices.append(
                ImouDevice(
                    device_id=str(raw["deviceId"]),
                    product_id=str(raw.get("productId") or ""),
                    name=str(raw.get("name") or raw["deviceId"]),
                    catalog=str(raw.get("catalog") or ""),
                    status=str(raw.get("status") or ""),
                    raw=raw,
                )
            )
        return devices
