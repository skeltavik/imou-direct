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
                if body.startswith(b"\x00") and not connected:
                    port = int(self._lan.get("p2p_port") or DEFAULT_DEVICE_PORT)
                    send_body(_ptcp_bind(realm, port))
                elif body.startswith(b"\x12") and len(body) >= 12:
                    if int.from_bytes(body[4:8], "big") != realm:
                        continue
                    status = body[12:]
                    if status == b"CONN":
                        connected = True
                        for chunk in drive_tls():
                            yield chunk
                    elif status == b"DISC":
                        raise LanP2PError("LAN peer disconnected")
                elif body.startswith(b"\x10") and len(body) >= 12:
                    length = int.from_bytes(body[:4], "big") & 0xFFFFFF
                    if int.from_bytes(body[4:8], "big") != realm or length > len(body) - 12:
                        raise LanP2PError("invalid LAN PTCP payload")
                    tls_in.write(body[12 : 12 + length])
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
