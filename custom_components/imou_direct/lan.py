"""Capture-free Imou LAN P2P and DHHTTP transport."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
import secrets
import socket
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from .core import build_play_request, derive_frame_key

DISCOVERY_ADDRESS = "255.255.255.255"
DISCOVERY_PORT = 28591
DEFAULT_DEVICE_PORT = 8086
MAX_DATAGRAM = 65_535
MAX_PLAY_HEADER = 64 * 1024
MAX_PLAY_BODY = 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")
_REQUIRED_LAN_FIELDS = (
    "device_id",
    "product_id",
    "bind_device_id",
    "device_username",
    "device_password",
    "dev_p2p_ak",
    "dev_p2p_sk",
)


class LanP2PError(RuntimeError):
    """Raised when the local Imou transport cannot establish or continue."""


def has_lan_config(config: dict) -> bool:
    """Return whether a bootstrap has the complete v0.3 LAN secret set."""
    lan = config.get("lan")
    return isinstance(lan, dict) and all(
        isinstance(lan.get(key), str) and bool(lan[key])
        for key in _REQUIRED_LAN_FIELDS
    )


def _required_text(values: dict, key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        raise LanP2PError("invalid LAN configuration")
    return value


def _identifier(values: dict, key: str) -> str:
    value = _required_text(values, key)
    if _IDENTIFIER.fullmatch(value) is None:
        raise LanP2PError("invalid LAN device identifier")
    return value


def _header(payload: bytes, name: bytes) -> bytes | None:
    prefix = name.lower() + b":"
    for line in payload.split(b"\r\n")[1:]:
        if line.lower().startswith(prefix):
            return line.split(b":", 1)[1].strip()
    return None


def _xml_body(fields: list[tuple[str, str]]) -> bytes:
    root = ET.Element("body")
    for name, value in fields:
        ET.SubElement(root, name).text = value
    return ET.tostring(root, encoding="utf-8", short_empty_elements=False)


def build_local_channel_request(
    lan: dict,
    *,
    now: dt.datetime | None = None,
    body_nonce: int | None = None,
    wsse_nonce: int | None = None,
    rand_salt: str | None = None,
    request_id: str | None = None,
    cseq: int | None = None,
) -> bytes:
    """Build a fresh authenticated LAN discovery/channel request."""
    product_id = _identifier(lan, "product_id")
    device_id = _identifier(lan, "device_id")
    username = _required_text(lan, "device_username")
    password = _required_text(lan, "device_password")
    p2p_ak = _required_text(lan, "dev_p2p_ak")
    p2p_sk = _required_text(lan, "dev_p2p_sk")
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    created_unix = str(int(moment.timestamp()))
    created_wsse = moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    body_nonce = secrets.randbelow(2**31) if body_nonce is None else body_nonce
    wsse_nonce = secrets.randbelow(2**31) if wsse_nonce is None else wsse_nonce
    rand_salt = secrets.token_hex(8).upper() if rand_salt is None else rand_salt
    request_id = secrets.token_hex(16) if request_id is None else request_id
    cseq = secrets.randbelow(2**31) if cseq is None else cseq

    device_key = hashlib.md5(
        f"{username}:Login to {rand_salt}:{password}".encode(),
        usedforsecurity=False,
    ).hexdigest().upper().encode()
    device_auth = base64.b64encode(
        hmac.new(
            device_key,
            (str(body_nonce) + created_unix).encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    wsse_digest = base64.b64encode(
        hashlib.sha1(
            (str(wsse_nonce) + created_wsse + p2p_sk).encode(),
            usedforsecurity=False,
        ).digest()
    ).decode()
    body = _xml_body(
        [
            ("CreateDate", created_unix),
            ("DevAuth", device_auth),
            ("Nonce", str(body_nonce)),
            ("RandSalt", rand_salt),
            ("UserName", username),
        ]
    )
    wsse = (
        f'UsernameToken Username="{p2p_ak}", '
        f'PasswordDigest="{wsse_digest}", '
        f'Nonce="{wsse_nonce}", Created="{created_wsse}"'
    )
    lines = [
        f"NFPOST /device/{product_id}@{device_id}/local-channel HTTP/1.1",
        "X-Version: 6.7.48",
        f"x-pcs-request-id: {request_id}",
        f"CSeq: {cseq}",
        'Authorization: WSSE profile="UsernameToken"',
        f"X-WSSE: {wsse}",
        "Content-Type: ",
        f"Content-Length: {len(body)}",
    ]
    return "\r\n".join(lines).encode() + b"\r\n\r\n" + body


def build_local_play_url(lan: dict) -> str:
    """Return the child-bound DHHTTP resource used inside the P2P tunnel."""
    bind_device_id = _identifier(lan, "bind_device_id")
    channel = int(lan.get("bind_channel_id", 0)) + 1
    subtype = int(lan.get("subtype", 0))
    encrypt = int(lan.get("encrypt", 3))
    image_size = int(lan.get("image_size", 41))
    audio_type = int(lan.get("audio_type", 1))
    path = (
        "/live/visualtalk.xav"
        if bool(lan.get("shared_link", True))
        else "/live/realmonitor.xav"
    )
    query = urllib.parse.urlencode(
        [
            ("channel", channel),
            ("subtype", subtype),
            ("encrypt", encrypt),
            ("imagesize", image_size),
            ("device", bind_device_id),
            ("audioType", audio_type),
        ]
    )
    port = int(lan.get("host_port") or lan.get("p2p_port") or DEFAULT_DEVICE_PORT)
    if not 1 <= port <= 65_535:
        raise LanP2PError("invalid LAN media port")
    return f"127.0.0.1:{port}{path}?{query}"


def _local_stream_config(lan: dict) -> dict:
    return {
        "stream": {
            "username": _required_text(lan, "device_username"),
            "password": _required_text(lan, "device_password"),
            "device_sn": _identifier(lan, "device_id"),
            "wsse_key": str(lan.get("wsse_key") or ""),
        }
    }


def build_local_play_request(lan: dict) -> bytes:
    """Build the fresh WSSE PLAY request for the local child stream."""
    return build_play_request(_local_stream_config(lan), build_local_play_url(lan))


def local_frame_key(lan: dict) -> bytes:
    """Derive the media frame key from the LAN stream credentials."""
    return derive_frame_key(_local_stream_config(lan))

RTSP_PORT = 554
RTSP_CHANNEL_MAIN = "/live/ch00_0"
RTSP_CHANNEL_SUB = "/live/ch00_1"
_RTSP_DIGEST_PARAM = re.compile(r'(\w+)=(?:\"([^\"]*)\"|([^,\\s]+))')


def has_rtsps_config(config: dict) -> bool:
    """Return whether a bootstrap has the minimal RTSPS stream secret set."""
    lan = config.get("lan")
    if not isinstance(lan, dict):
        return False
    if not isinstance(lan.get("discovery_address"), str) or not lan["discovery_address"]:
        return False
    return all(
        isinstance(lan.get(key), str) and bool(lan[key])
        for key in ("device_username", "device_password")
    )


def _rtsp_digest_fields(challenge: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, quoted, bare in _RTSP_DIGEST_PARAM.findall(challenge):
        fields[key.lower()] = quoted or bare
    return fields


def rtsp_authorization(
    challenge: str,
    username: str,
    password: str,
    method: str,
    uri: str,
) -> str | None:
    """Build a Digest Authorization header for the local RTSPS listener."""
    if not challenge.lower().startswith("digest"):
        return None
    fields = _rtsp_digest_fields(challenge)
    realm, nonce = fields.get("realm"), fields.get("nonce")
    if not realm or not nonce:
        return None
    md5 = lambda value: hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: E731
    cnonce = secrets.token_hex(8)
    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    if "auth" in fields.get("qop", "").lower():
        response = md5(f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}")
        tail = f', qop=auth, nc=00000001, cnonce="{cnonce}"'
    else:
        response = md5(f"{ha1}:{nonce}:{ha2}")
        tail = ""
    opaque = f', opaque="{fields["opaque"]}"' if fields.get("opaque") else ""
    return (
        f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
        f'uri="{uri}", response="{response}"{opaque}{tail}'
    )


def _rtsp_request(
    tls: ssl.SSLSocket,
    method: str,
    uri: str,
    cseq: int,
    authorization: str | None = None,
    extra: str = "",
) -> tuple[str, dict[str, str], bytes]:
    """Send one RTSP request and return its status line, headers, and body."""
    lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}"]
    if method == "DESCRIBE":
        lines.append("Accept: application/sdp")
    if authorization:
        lines.append(f"Authorization: {authorization}")
    if extra:
        lines.append(extra)
    tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = tls.recv(8192)
        if not chunk:
            raise LanP2PError("RTSPS response ended unexpectedly")
        data.extend(chunk)
        if len(data) > MAX_PLAY_HEADER + MAX_PLAY_BODY:
            raise LanP2PError("RTSPS response is too large")
    head, _, body = bytes(data).partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.strip().lower().decode("ascii", "replace")] = value.strip().decode(
                "latin-1", "replace"
            )
    length = 0
    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError as error:
            raise LanP2PError("invalid RTSPS response length") from error
        if not 0 <= length <= MAX_PLAY_BODY:
            raise LanP2PError("invalid RTSPS response length")
    while len(body) < length:
        chunk = tls.recv(8192)
        if not chunk:
            raise LanP2PError("RTSPS response ended unexpectedly")
        body += chunk
    return status, headers, body


def _rtsp_challenge(headers: dict[str, str]) -> str:
    challenge = headers.get("www-authenticate", "")
    return challenge.split(":", 1)[1].strip() if ":" in challenge else challenge


class RtspsTransport:
    """Stream local HEVC video over RTSPS from the LAN device listener."""

    def __init__(self, config: dict, *, timeout: float = 15.0) -> None:
        lan = config.get("lan")
        if not isinstance(lan, dict):
            raise LanP2PError("LAN configuration is missing")
        self._lan = dict(lan)
        self._timeout = timeout

    @property
    def _target(self) -> tuple[str, int]:
        address = str(self._lan.get("discovery_address") or "")
        if not address:
            raise LanP2PError("LAN discovery address is missing")
        port = int(self._lan.get("rtsp_port") or RTSP_PORT)
        if not 1 <= port <= 65_535:
            raise LanP2PError("invalid LAN media port")
        return address, port

    def _credentials(self) -> tuple[str, str]:
        return (
            _required_text(self._lan, "device_username"),
            _required_text(self._lan, "device_password"),
        )

    def _channel(self) -> str:
        channel = str(self._lan.get("rtsp_channel") or RTSP_CHANNEL_MAIN)
        if not channel.startswith("/") or " " in channel or "\r" in channel or "\n" in channel:
            raise LanP2PError("invalid LAN media channel")
        return channel

    def _connect(self) -> ssl.SSLSocket:
        address, port = self._target
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("ALL:@SECLEVEL=0")
        try:
            raw = socket.create_connection((address, port), timeout=self._timeout)
            raw.settimeout(self._timeout)
            tls = context.wrap_socket(raw, server_hostname=address)
        except (OSError, ssl.SSLError) as error:
            raise LanP2PError("LAN RTSPS connection failed") from error
        tls.settimeout(self._timeout)
        return tls

    def stream(self, stop=None) -> Iterator[bytes]:
        """Yield Annex-B HEVC NAL units from the local RTSPS video track."""
        username, password = self._credentials()
        channel = self._channel()
        address, port = self._target
        base = f"rtsp://{address}:{port}{channel}"
        tls = self._connect()
        try:
            status, headers, _body = _rtsp_request(tls, "OPTIONS", base, 1)
            if " 401 " not in status or "www-authenticate" not in headers:
                raise LanP2PError("RTSPS challenge was not offered")
            challenge = _rtsp_challenge(headers)
            describe_auth = rtsp_authorization(challenge, username, password, "DESCRIBE", base)
            if describe_auth is None:
                raise LanP2PError("RTSPS challenge is unsupported")
            status, _headers, _body = _rtsp_request(
                tls, "DESCRIBE", base, 2, describe_auth, "Accept: application/sdp"
            )
            if " 200 " not in status:
                raise LanP2PError("RTSPS media description was rejected")
            session = ""
            for track in (0, 1):
                track_uri = f"{base}/trackID={track}"
                setup_auth = rtsp_authorization(challenge, username, password, "SETUP", track_uri)
                if setup_auth is None:
                    raise LanP2PError("RTSPS challenge is unsupported")
                status, setup_headers, _body = _rtsp_request(
                    tls,
                    "SETUP",
                    track_uri,
                    3 + track,
                    setup_auth,
                    f"Transport: RTP/AVP/TCP;unicast;interleaved={2 * track}-{2 * track + 1}",
                )
                if " 200 " not in status:
                    raise LanP2PError("RTSPS transport setup was rejected")
                if track == 0:
                    session = setup_headers.get("session", "").split(";")[0].strip()
            extra = f"Session: {session}\r\nRange: npt=0.000-" if session else ""
            status, _headers, _body = _rtsp_request(tls, "PLAY", base, 5, None, extra)
            if " 200 " not in status:
                raise LanP2PError("RTSPS playback was rejected")
            yield from self._annex_b(tls, stop)
        except (OSError, ssl.SSLError) as error:
            raise LanP2PError("LAN RTSPS transport failed") from error
        finally:
            tls.close()

    def _annex_b(self, tls: ssl.SSLSocket, stop) -> Iterator[bytes]:
        """Reassemble FU fragmentation units into Annex-B NAL units."""
        pending = bytearray()
        fragment: bytearray | None = None
        param_sets: list[bytes] = []
        param_seen: set[int] = set()
        while stop is None or not stop.is_set():
            try:
                chunk = tls.recv(MAX_DATAGRAM)
            except socket.timeout as error:
                raise LanP2PError("LAN stream timed out") from error
            if not chunk:
                raise LanP2PError("LAN stream ended")
            pending.extend(chunk)
            for unit in self._drain(pending):
                if unit is None:
                    continue
                nal_type = unit[0] >> 1
                if nal_type in (32, 33, 34):
                    if nal_type not in param_seen:
                        param_seen.add(nal_type)
                        param_sets.append(unit)
                        yield b"\x00\x00\x00\x01" + unit
                elif nal_type == 49:
                    assembled = self._fragment(unit, fragment)
                    fragment = assembled[1]
                    if assembled[0] is not None:
                        yield b"\x00\x00\x00\x01" + assembled[0]
                else:
                    yield b"\x00\x00\x00\x01" + unit

    @staticmethod
    def _fragment(
        unit: bytes, fragment: bytearray | None
    ) -> tuple[bytes | None, bytearray | None]:
        """Fold one FU payload into its reassembly buffer."""
        if len(unit) < 3:
            return None, fragment
        start, end, fragment_type = (
            bool(unit[2] & 0x80),
            bool(unit[2] & 0x40),
            unit[2] & 0x3F,
        )
        if start:
            fragment = bytearray(bytes([(unit[0] & 0x81) | (fragment_type << 1), unit[1]]))
        if fragment is None:
            return None, None
        fragment.extend(unit[3:])
        if end:
            return bytes(fragment), None
        return None, fragment

    def _drain(self, pending: bytearray) -> Iterator[bytes | None]:
        """Yield complete video RTP payloads from the interleaved buffer."""
        while len(pending) >= 4:
            if pending[0] != 0x24:
                marker = pending.find(b"$")
                if marker < 0:
                    pending.clear()
                    return
                del pending[:marker]
                if len(pending) < 4:
                    return
            size = int.from_bytes(pending[2:4], "big")
            if size <= 0 or size > MAX_DATAGRAM:
                del pending[0]
                continue
            if len(pending) < 4 + size:
                return
            channel = pending[1]
            packet = bytes(pending[4 : 4 + size])
            del pending[: 4 + size]
            if channel != 0:
                continue
            parsed = self._rtp_payload(packet)
            yield parsed

    @staticmethod
    def _rtp_payload(packet: bytes) -> bytes | None:
        """Strip the RTP header and return video payload type 98 bytes."""
        if len(packet) < 12 or packet[0] >> 6 != 2 or packet[1] & 0x7F != 98:
            return None
        offset = 12 + 4 * (packet[0] & 0x0F)
        if packet[0] & 0x10:
            if len(packet) < offset + 4:
                return None
            offset += 4 + 4 * int.from_bytes(packet[offset + 2 : offset + 4], "big")
        end = len(packet)
        if packet[0] & 0x20:
            padding = packet[-1]
            if not padding or padding > end - offset:
                return None
            end -= padding
        if offset >= end:
            return None
        return packet[offset:end]


def _ptcp_payload(realm: int, data: bytes) -> bytes:
    return (
        (0x10000000 | len(data)).to_bytes(4, "big")
        + realm.to_bytes(4, "big")
        + b"\x00" * 4
        + data
    )


def _ptcp_bind(realm: int, port: int) -> bytes:
    return (
        b"\x11\x00\x00\x00"
        + realm.to_bytes(4, "big")
        + b"\x00" * 4
        + port.to_bytes(4, "big")
        + b"\x7f\x00\x00\x01"
    )


def _ptcp_status(realm: int, status: bytes) -> bytes:
    return (
        b"\x12\x00\x00\x00"
        + realm.to_bytes(4, "big")
        + b"\x00" * 4
        + status
    )


class _PTCPSession:
    """Small PTCP framing state for one reliable local UDP tunnel."""

    def __init__(self) -> None:
        self.sent = 0
        self.received = 0
        self.count = 0
        self.local_id = 0
        self.remote_id = 0
        self._seen: set[tuple[int, int, int, int, bytes]] = set()

    def packet(self, body: bytes = b"", *, sync: bool = False) -> bytes:
        packet_id = 0x0002FFFF if sync else 0x0000FFFF - self.count
        result = (
            b"PTCP"
            + self.sent.to_bytes(4, "big")
            + self.received.to_bytes(4, "big")
            + packet_id.to_bytes(4, "big")
            + self.local_id.to_bytes(4, "big")
            + self.remote_id.to_bytes(4, "big")
            + body
        )
        self.sent += len(body)
        self.local_id = (self.local_id + 1) & 0xFFFFFFFF
        if body and not sync:
            self.count += 1
        return result

    def accept(self, packet: bytes) -> tuple[bytes, bool]:
        if len(packet) < 24 or not packet.startswith(b"PTCP"):
            raise LanP2PError("invalid PTCP packet")
        peer_sent = int.from_bytes(packet[4:8], "big")
        packet_id = int.from_bytes(packet[12:16], "big")
        local_id = int.from_bytes(packet[16:20], "big")
        body = packet[24:]
        key = (
            peer_sent,
            packet_id,
            local_id,
            len(body),
            hashlib.blake2s(body, digest_size=8).digest(),
        )
        duplicate = key in self._seen
        self.remote_id = local_id
        if not duplicate:
            self.received += len(body)
            self._seen.add(key)
            if len(self._seen) > 4096:
                self._seen.clear()
                self._seen.add(key)
        return body, duplicate


class _PTCPMessageFramer:
    """Incrementally split realm-bound PTCP status and data messages."""

    _STATUS_SIZE = 16

    def __init__(self, realm: int) -> None:
        self._realm = realm
        self._buffer = bytearray()
        self._status_buffer = bytearray()

    @property
    def pending(self) -> bool:
        """Return whether a fragmented inner message is being buffered."""
        return bool(self._buffer or self._status_buffer)

    def _status_header(self) -> bytes:
        return b"\x12\x00\x00\x00" + self._realm.to_bytes(4, "big") + b"\x00" * 4

    def _take_interleaved_statuses(
        self, data: bytes, messages: list[tuple[int, bytes]]
    ) -> bytes:
        """Remove status frames arriving while a data frame is incomplete."""
        remaining = data
        expected = self._status_header()
        while self._buffer or self._status_buffer:
            if self._status_buffer:
                needed = self._STATUS_SIZE - len(self._status_buffer)
                self._status_buffer.extend(remaining[:needed])
                remaining = remaining[needed:]
                if not expected.startswith(self._status_buffer[:12]):
                    remaining = bytes(self._status_buffer) + remaining
                    self._status_buffer.clear()
                    break
                if len(self._status_buffer) < self._STATUS_SIZE:
                    return b""
                messages.append((0x12, bytes(self._status_buffer[12:])))
                self._status_buffer.clear()
                continue
            if not remaining or remaining[0] != 0x12:
                break
            prefix_length = min(len(remaining), 12)
            if not expected.startswith(remaining[:prefix_length]):
                break
            if len(remaining) < self._STATUS_SIZE:
                self._status_buffer.extend(remaining)
                return b""
            messages.append((0x12, remaining[12:self._STATUS_SIZE]))
            remaining = remaining[self._STATUS_SIZE :]
        return remaining

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Consume PTCP inner bytes and return every complete typed message."""
        if len(data) > MAX_DATAGRAM:
            raise LanP2PError("invalid LAN PTCP payload")
        messages: list[tuple[int, bytes]] = []
        data = self._take_interleaved_statuses(data, messages)
        self._buffer.extend(data)
        if (
            len(self._buffer) + len(self._status_buffer)
            > MAX_PLAY_HEADER + MAX_PLAY_BODY + 12 + MAX_DATAGRAM
        ):
            raise LanP2PError("invalid LAN PTCP payload")
        while len(self._buffer) >= 12:
            descriptor = int.from_bytes(self._buffer[:4], "big")
            kind = descriptor >> 24
            if int.from_bytes(self._buffer[4:8], "big") != self._realm:
                raise LanP2PError("invalid LAN PTCP payload")
            if kind == 0x10:
                length = descriptor & 0xFFFFFF
                if length > MAX_PLAY_HEADER + MAX_PLAY_BODY:
                    raise LanP2PError("invalid LAN PTCP payload")
                message_length = 12 + length
            elif kind == 0x12:
                if descriptor != 0x12000000:
                    raise LanP2PError("invalid LAN PTCP payload")
                message_length = self._STATUS_SIZE
            else:
                raise LanP2PError("invalid LAN PTCP payload")
            if len(self._buffer) < message_length:
                break
            messages.append((kind, bytes(self._buffer[12:message_length])))
            del self._buffer[:message_length]
        return messages


def _is_preconnection_control(body: bytes, connected: bool, pending: bool) -> bool:
    """Return whether a PTCP body is pre-connection control traffic."""
    return not pending and not connected and body.startswith(b"\x00")


def _response_length(header: bytes) -> int:
    for line in header.split(b"\r\n"):
        if line.lower().startswith((b"private-length:", b"content-length:")):
            try:
                length = int(line.split(b":", 1)[1])
            except ValueError as error:
                raise LanP2PError("invalid LAN PLAY response length") from error
            if not 0 <= length <= MAX_PLAY_BODY:
                raise LanP2PError("invalid LAN PLAY response length")
            return length
    raise LanP2PError("LAN PLAY response length missing")


class LanP2PTransport:
    """Open the authenticated Imou local channel and yield encrypted media bytes."""

    def __init__(self, config: dict, *, timeout: float = 15.0) -> None:
        lan = config.get("lan")
        if not isinstance(lan, dict):
            raise LanP2PError("LAN configuration is missing")
        self._lan = dict(lan)
        self._lan.setdefault("host_port", 49_152 + secrets.randbelow(16_384))
        self._timeout = timeout

    @property
    def frame_key(self) -> bytes:
        return local_frame_key(self._lan)

    def _discover(self, sock: socket.socket, stop) -> tuple[str, int]:
        request = build_local_channel_request(self._lan)
        cseq = _header(request, b"CSeq")
        if cseq is None:
            raise LanP2PError("LAN request correlation is missing")
        address = str(self._lan.get("discovery_address") or DISCOVERY_ADDRESS)
        port = int(self._lan.get("discovery_port") or DISCOVERY_PORT)
        for _attempt in range(3):
            sock.sendto(request, (address, port))
            deadline = time.monotonic() + min(1.5, self._timeout)
            while time.monotonic() < deadline:
                if stop is not None and stop.is_set():
                    raise LanP2PError("LAN transport stopped")
                try:
                    response, peer = sock.recvfrom(MAX_DATAGRAM)
                except socket.timeout:
                    continue
                if _header(response, b"CSeq") != cseq:
                    continue
                status = response.split(b"\r\n", 1)[0]
                if b" 200 " not in status:
                    raise LanP2PError("LAN channel request was rejected")
                return peer
        raise LanP2PError("LAN device was not discovered")

    def stream(self, stop=None) -> Iterator[bytes]:
        """Yield the local DHHTTP media stream after its private SDP body."""
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", 0))
            sock.settimeout(0.5)
        except OSError as error:
            if sock is not None:
                sock.close()
            raise LanP2PError("LAN socket setup failed") from error
        ptcp = _PTCPSession()
        realm = secrets.randbits(32)
        message_framer = _PTCPMessageFramer(realm)
        peer: tuple[str, int] | None = None
        connected = False
        play_sent = False
        last_send = time.monotonic()
        last_receive = last_send
        connect_deadline = last_send + self._timeout
        response = bytearray()
        response_ready = False
        play_started_at: float | None = None
        tls_in = ssl.MemoryBIO()
        tls_out = ssl.MemoryBIO()
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.set_ciphers("ALL:@SECLEVEL=0")
            tls = context.wrap_bio(tls_in, tls_out, server_side=False)
        except ssl.SSLError as error:
            sock.close()
            raise LanP2PError("LAN TLS setup failed") from error
        tls_ready = False

        def send_body(body: bytes = b"", *, sync: bool = False) -> None:
            nonlocal last_send
            if peer is None:
                raise LanP2PError("LAN peer is missing")
            sock.sendto(ptcp.packet(body, sync=sync), peer)
            last_send = time.monotonic()

        def flush_tls() -> None:
            while tls_out.pending:
                send_body(_ptcp_payload(realm, tls_out.read()))

        def drive_tls() -> list[bytes]:
            nonlocal tls_ready, play_sent, play_started_at, response_ready
            if not tls_ready:
                try:
                    tls.do_handshake()
                    tls_ready = True
                except ssl.SSLWantReadError:
                    pass
                flush_tls()
            if tls_ready and not play_sent:
                tls.write(build_local_play_request(self._lan))
                play_sent = True
                play_started_at = time.monotonic()
                flush_tls()
            plaintext: list[bytes] = []
            if tls_ready:
                while True:
                    try:
                        chunk = tls.read(MAX_DATAGRAM)
                    except ssl.SSLWantReadError:
                        break
                    if not chunk:
                        break
                    plaintext.append(chunk)
                flush_tls()
            output: list[bytes] = []
            for chunk in plaintext:
                if response_ready:
                    output.append(chunk)
                    continue
                response.extend(chunk)
                if len(response) > MAX_PLAY_HEADER + MAX_PLAY_BODY:
                    raise LanP2PError("LAN PLAY response is too large")
                if b"\r\n\r\n" not in response:
                    continue
                header, _separator, body = bytes(response).partition(b"\r\n\r\n")
                if len(header) > MAX_PLAY_HEADER:
                    raise LanP2PError("LAN PLAY response header is too large")
                if b" 200 " not in header.split(b"\r\n", 1)[0]:
                    raise LanP2PError("LAN PLAY request was rejected")
                private_length = _response_length(header)
                if len(body) < private_length:
                    continue
                response_ready = True
                remainder = body[private_length:]
                response.clear()
                if remainder:
                    output.append(remainder)
            return output

        try:
            peer = self._discover(sock, stop)
            send_body(b"\x00\x03\x01\x00", sync=True)
            while stop is None or not stop.is_set():
                now = time.monotonic()
                if not connected and now >= connect_deadline:
                    raise LanP2PError("LAN PTCP connection timed out")
                if (
                    play_started_at is not None
                    and not response_ready
                    and now - play_started_at >= self._timeout
                ):
                    raise LanP2PError("LAN PLAY response timed out")
                if connected and now - last_receive >= self._timeout:
                    raise LanP2PError("LAN stream timed out")
                if now - last_send >= 5:
                    send_body(b"\x13" + b"\x00" * 11)
                try:
                    packet, source = sock.recvfrom(MAX_DATAGRAM)
                except socket.timeout:
                    continue
                if source != peer or not packet.startswith(b"PTCP"):
                    continue
                last_receive = time.monotonic()
                body, duplicate = ptcp.accept(packet)
                if body:
                    send_body()
                if duplicate:
                    continue
                if _is_preconnection_control(
                    body, connected, message_framer.pending
                ):
                    port = int(self._lan.get("p2p_port") or DEFAULT_DEVICE_PORT)
                    send_body(_ptcp_bind(realm, port))
                elif message_framer.pending or body.startswith((b"\x10", b"\x12")):
                    for kind, payload in message_framer.feed(body):
                        if kind == 0x12:
                            if payload == b"CONN":
                                connected = True
                                for chunk in drive_tls():
                                    yield chunk
                            elif payload == b"DISC":
                                raise LanP2PError("LAN peer disconnected")
                            continue
                        tls_in.write(payload)
                        for chunk in drive_tls():
                            yield chunk
            return
        except (OSError, ssl.SSLError) as error:
            raise LanP2PError("LAN transport failed") from error
        finally:
            if peer is not None and connected:
                try:
                    send_body(_ptcp_status(realm, b"DISC"))
                except (OSError, LanP2PError):
                    pass
            sock.close()


INTERCEPT_HOST = "127.0.0.1"
INTERCEPT_PORT = 49511
_ANNEX_B_PREFIX = b"\x00\x00\x00\x01"
_MAX_NAL = 8 * 1024 * 1024


def has_intercept_config(config: dict) -> bool:
    """Return whether an intercept daemon endpoint is configured."""
    lan = config.get("lan")
    if not isinstance(lan, dict):
        return False
    if lan.get("intercept") is False:
        return False
    host = lan.get("intercept_host") or INTERCEPT_HOST
    port = lan.get("intercept_port") or INTERCEPT_PORT
    if not isinstance(host, str) or not host or "\r" in host or "\n" in host:
        return False
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return False
    return 1 <= port_number <= 65_535


class InterceptTransport:
    """Yield Annex-B NAL units from a loopback intercept daemon."""

    def __init__(self, config: dict, *, timeout: float = 15.0) -> None:
        lan = config.get("lan")
        if not isinstance(lan, dict):
            raise LanP2PError("LAN configuration is missing")
        host = lan.get("intercept_host")
        host = str(host) if host is not None else INTERCEPT_HOST
        raw_port = lan.get("intercept_port")
        raw_port = raw_port if raw_port is not None else INTERCEPT_PORT
        try:
            port = int(raw_port)
        except (TypeError, ValueError) as error:
            raise LanP2PError("invalid intercept endpoint") from error
        if not host or "\r" in host or "\n" in host or not 1 <= port <= 65_535:
            raise LanP2PError("invalid intercept endpoint")
        self._host = host
        self._port = port
        self._timeout = timeout

    def stream(self, stop=None) -> Iterator[bytes]:
        """Yield one Annex-B NAL unit per length-prefixed daemon frame."""
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            )
            sock.settimeout(self._timeout)
        except OSError as error:
            raise LanP2PError("intercept daemon is unreachable") from error
        pending = bytearray()
        try:
            while stop is None or not stop.is_set():
                try:
                    chunk = sock.recv(MAX_DATAGRAM)
                except socket.timeout as error:
                    raise LanP2PError("LAN stream timed out") from error
                if not chunk:
                    if pending:
                        raise LanP2PError("LAN stream ended")
                    return
                pending.extend(chunk)
                if len(pending) > _MAX_NAL + 4:
                    raise LanP2PError("intercept frame is too large")
                while len(pending) >= 4:
                    size = int.from_bytes(pending[:4], "big")
                    if size <= 0 or size > _MAX_NAL:
                        raise LanP2PError("intercept frame is too large")
                    if len(pending) < 4 + size:
                        break
                    unit = bytes(pending[4 : 4 + size])
                    del pending[: 4 + size]
                    if not unit.startswith(_ANNEX_B_PREFIX):
                        raise LanP2PError("intercept frame is not Annex-B")
                    yield unit
        except OSError as error:
            raise LanP2PError("intercept transport failed") from error
        finally:
            sock.close()
