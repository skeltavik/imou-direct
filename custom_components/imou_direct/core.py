from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import socket
import ssl
import string
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _digest(body: bytes, algorithm: str) -> str:
    digest = hashlib.new(
        algorithm, body, usedforsecurity=algorithm.lower() != "md5"
    ).digest()
    return base64.b64encode(digest).decode()


def _new_nonce() -> str:
    alphabet = string.ascii_letters + string.digits
    return str(int(dt.datetime.now().timestamp() * 1000)) + "".join(
        secrets.choice(alphabet) for _ in range(32)
    )


def _canonical_signature(signing: dict, digest: str) -> bytes:
    lines = [
        signing.get("method", "POST"),
        signing["uri"],
        digest,
        signing.get("content_type", "application/json; charset=utf-8"),
        f'x-pcs-apiver:{signing["revision"]}',
        f'x-pcs-client-ua:{signing["client_ua"]}',
        f'x-pcs-date:{signing["date"]}',
    ]
    if signing.get("file_crypt_version"):
        lines.append(f'x-pcs-file-crypt-version:{signing["file_crypt_version"]}')
    lines.append(f'x-pcs-nonce:{signing["nonce"]}')
    if signing.get("session_id"):
        lines.append(f'x-pcs-session-id:{signing["session_id"]}')
    lines.append(f'x-pcs-username:{signing["username"]}')
    return ("\n".join(lines) + "\n").encode()


def _signature(signing: dict, key: str, digest: str) -> str:
    return base64.b64encode(
        hmac.new(key.encode(), _canonical_signature(signing, digest), hashlib.sha256).digest()
    ).decode()


def _find_transfer_url(value) -> str | None:
    if isinstance(value, dict):
        for key in ("tls_resource", "tlsResource", "resource"):
            candidate = value.get(key)
            if isinstance(candidate, str) and ".rtpxav?" in candidate:
                return candidate
        for nested in value.values():
            candidate = _find_transfer_url(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_transfer_url(nested)
            if candidate:
                return candidate
    return None


def fetch_transfer_url(config: dict, timeout: int = 20) -> str:
    rest = config["rest"]
    body = json.dumps(
        {"data": config["request"]}, separators=(",", ":"), ensure_ascii=False
    ).encode()
    date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = _new_nonce()
    signing = {
        **rest,
        "date": date,
        "nonce": nonce,
    }
    md5_value = _digest(body, "md5")
    sha256_value = _digest(body, "sha256")
    headers = {
        "Content-Type": rest["content_type"],
        "Content-MD5": md5_value,
        "Content-SHA256": sha256_value,
        "x-pcs-signature": _signature(signing, rest["md5_key"], md5_value),
        "x-pcs-signature-sha256": _signature(
            signing, rest["sha256_key"], sha256_value
        ),
        "x-pcs-username": rest["username"],
        "x-pcs-apiver": rest["revision"],
        "x-pcs-nonce": nonce,
        "x-pcs-date": date,
        "x-pcs-client-ua": rest["client_ua"],
        "timeout": str(timeout * 1000),
        "Accept-Encoding": "identity",
    }
    if rest.get("session_id"):
        headers["x-pcs-session-id"] = rest["session_id"]
    if rest.get("request_id"):
        headers["x-pcs-request-id"] = rest["request_id"]
    if rest.get("region"):
        headers["x_pcs_region"] = rest["region"]

    endpoint = urllib.parse.urljoin(rest["host"].rstrip("/") + "/", rest["uri"])
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        response_body = error.read()
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"transfer API returned non-JSON HTTP {status}") from error
    transfer_url = _find_transfer_url(parsed)
    if not transfer_url:
        api_code = parsed.get("code") if isinstance(parsed, dict) else None
        raise RuntimeError(
            f"transfer API URL missing (HTTP {status}, API code {api_code})"
        )
    return transfer_url


def build_play_request(config: dict, transfer_url: str) -> bytes:
    stream = config["stream"]
    if not stream.get("play_template_hex"):
        return _build_generic_play_request(stream, transfer_url)

    template = bytes.fromhex(stream["play_template_hex"])
    head, separator, body = template.partition(b"\r\n\r\n")
    if not separator:
        raise RuntimeError("PLAY template has no header separator")
    lines = head.decode("latin1").split("\r\n")
    old_target = lines[0].split(" ", 2)[1]
    old_query = urllib.parse.parse_qsl(
        urllib.parse.urlsplit(old_target).query, keep_blank_values=True
    )
    parsed_url = urllib.parse.urlsplit("//" + transfer_url)
    query = urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
    query.extend((key, value) for key, value in old_query if key in {"trackID", "method"})
    target = urllib.parse.urlunsplit(
        ("", "", parsed_url.path, urllib.parse.urlencode(query), "")
    )
    lines[0] = f"PLAY {target} HTTP/1.1"
    for index, line in enumerate(lines):
        if line.lower().startswith("host:"):
            lines[index] = f"Host: {parsed_url.netloc}"
        elif line.lower().startswith("x-pcs-request-id:"):
            lines[index] = f"x-pcs-request-id: {secrets.token_hex(8)}"

    nonce = secrets.token_hex(16)
    created = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = base64.b64encode(
        hmac.new(
            bytes.fromhex(stream["transfer_hmac_key_hex"]),
            transfer_url.encode() + nonce.encode() + created.encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    for index, line in enumerate(lines):
        if not line.startswith("WSSE:"):
            continue
        replacements = {
            "Username": stream["username"],
            "PasswordDigest": digest,
            "Nonce": nonce,
            "Created": created,
        }
        for key, value in replacements.items():
            line = re.sub(key + r'="[^"]*"', f'{key}="{value}"', line)
        lines[index] = line
    return "\r\n".join(lines).encode() + separator + body


def _parse_transfer_url(transfer_url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(
        transfer_url if "://" in transfer_url else "//" + transfer_url
    )
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("transfer URL has no host and port")
    return parsed


def _build_generic_play_request(stream: dict, transfer_url: str) -> bytes:
    """Build the Imou PLAY request without retaining a captured template."""
    parsed = _parse_transfer_url(transfer_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key not in {"trackID", "method"}]
    query.extend((("trackID", "31"), ("method", "0")))
    target = urllib.parse.urlunsplit(
        ("", "", parsed.path, urllib.parse.urlencode(query), "")
    )

    nonce = secrets.token_hex(16)
    created = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    effective_key = stream.get("wsse_key") or stream["device_sn"]
    credential_material = (
        stream["username"]
        + ":Login to "
        + effective_key
        + ":"
        + stream["password"]
    ).encode()
    md5_token = hashlib.md5(credential_material, usedforsecurity=False).hexdigest().upper()
    sha256_token = hashlib.sha256(credential_material).hexdigest().upper()
    password_digest = base64.b64encode(
        hashlib.sha1(
            (nonce + created + md5_token).encode(), usedforsecurity=False
        ).digest()
    ).decode()
    lightweight_digest = base64.b64encode(
        hashlib.sha256((nonce + created + sha256_token).encode()).digest()
    ).decode()

    session_version = secrets.randbits(32)
    body = (
        "v=0\r\n"
        f"o=- {session_version} {session_version} IN IP4 0.0.0.0\r\n"
        "s=Media Server\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "t=0 0\r\n"
        "a=control:*\r\n"
        "a=packetization-supported:DH\r\n"
        "a=rtppayload-supported:DH\r\n"
        "a=range:npt=now-\r\n"
        "m=video 0 RTP/AVP 0\r\n"
        "a=control:trackID=0\r\n"
        "a=framerate:0\r\n"
        "a=rtpmap:0 disable/90000\r\n"
        "a=fmtp\r\n"
        "a=sendonly\r\n"
        "m=audio 0 RTP/AVP 0\r\n"
        "a=control:trackID=1\r\n"
        "a=rtpmap:0 disable/8000\r\n"
        "a=sendonly\r\n"
        "m=audio 0 RTP/AVP 0\r\n"
        "a=control:trackID=2\r\n"
        "a=rtpmap:0 disable/8000\r\n"
        "a=sendonly\r\n"
        "m=application 0 RTP/AVP 100\r\n"
        "a=control:trackID=3\r\n"
        "a=rtpmap:100 stream-assist-frame/90000\r\n"
        "a=sendonly\r\n"
        "m=application 0 RTP/AVP 107\r\n"
        "a=control:trackID=4\r\n"
        "a=rtpmap:107 vnd.onvif.metadata/90000\r\n"
        "a=sendonly\r\n"
        "m=audio 0 RTP/AVP 8\r\n"
        "a=control:trackID=5\r\n"
        "a=rtpmap:8 PCMA/16000\r\n"
        "a=sendonly\r\n"
    ).encode()
    wsse = (
        f'UsernameToken Username="{stream["username"]}", '
        f'PasswordDigest="{password_digest}", '
        f'LightweightDigest="{lightweight_digest}", '
        f'Nonce="{nonce}", Created="{created}"'
    )
    headers = [
        f"PLAY {target} HTTP/1.1",
        "Accpet-Sdp: Private",
        'Authorization: WSSE profile="UsernameToken"',
        "Connect-Type: P2P",
        "Connection: keep-alive",
        "Cseq: 0",
        f"Host: {parsed.netloc}",
        f"Private-Length: {len(body)}",
        "Private-Type: application/sdp",
        "Speed: 1.000000",
        "User-Agent: Http Stream Client/1.0",
        f"WSSE: {wsse}",
        f"x-pcs-request-id: {secrets.token_hex(8)}",
    ]
    return "\r\n".join(headers).encode() + b"\r\n\r\n" + body


def _header(request: bytes, name: bytes) -> bytes:
    prefix = name.lower() + b":"
    for line in request.split(b"\r\n")[1:]:
        if line.lower().startswith(prefix):
            return line.split(b":", 1)[1].strip()
    raise RuntimeError("PLAY host header missing")


def _play_response_length(header: bytes) -> int:
    for line in header.split(b"\r\n"):
        if line.lower().startswith((b"content-length:", b"private-length:")):
            return int(line.split(b":", 1)[1])
    raise RuntimeError("PLAY SDP length missing")


def tls_play_bytes(
    config: dict, transfer_url: str, timeout: int = 10, stop=None
):
    request = build_play_request(config, transfer_url)
    host_value = _header(request, b"Host").decode("ascii")
    host, port_text = host_value.rsplit(":", 1)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, int(port_text)), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.settimeout(timeout)
            tls.sendall(request)
            pending = bytearray()
            while b"\r\n\r\n" not in pending:
                chunk = tls.recv(65536)
                if not chunk:
                    raise RuntimeError("PLAY response ended before headers")
                pending.extend(chunk)
            header, separator, remainder = bytes(pending).partition(b"\r\n\r\n")
            status_line = header.split(b"\r\n", 1)[0]
            if b" 200 " not in status_line:
                raise RuntimeError("PLAY response was not HTTP 200")
            content_length = _play_response_length(header)
            while len(remainder) < content_length:
                chunk = tls.recv(65536)
                if not chunk:
                    raise RuntimeError("PLAY response ended during SDP")
                remainder += chunk
            yield remainder[content_length:]
            tls.settimeout(2)
            while True:
                try:
                    chunk = tls.recv(65536)
                except socket.timeout:
                    if stop is not None and stop.is_set():
                        return
                    continue
                if not chunk:
                    return
                yield chunk


def derive_frame_key(config: dict) -> bytes:
    stream = config["stream"]
    effective_key = stream.get("wsse_key") or stream["device_sn"]
    material = (
        stream["username"]
        + ":Login to "
        + effective_key
        + ":"
        + stream["password"]
    ).encode()
    login_md5 = (
        hashlib.md5(material, usedforsecurity=False).hexdigest().upper().encode()
    )
    return hashlib.pbkdf2_hmac(
        "sha256", login_md5, effective_key.encode(), 20_000, dklen=32
    )


def _rtp_payload(packet: bytes) -> tuple[int, int, bytes] | None:
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return None
    csrc_count = packet[0] & 0x0F
    offset = 12 + 4 * csrc_count
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
    if offset > end:
        return None
    return packet[1] & 0x7F, int.from_bytes(packet[2:4], "big"), packet[offset:end]


def _b5_info(extension: bytes) -> tuple[int, int, bytes] | None:
    offset = extension.find(b"\xb5")
    if offset < 0 or offset + 43 > len(extension) or extension[offset + 2] != 1:
        return None
    clear = int.from_bytes(extension[offset + 3 : offset + 6], "little")
    encrypted = int.from_bytes(extension[offset + 6 : offset + 9], "little")
    return clear, encrypted, extension[offset + 27 : offset + 43]


class HevcExtractor:
    def __init__(self, frame_key: bytes):
        self.frame_key = frame_key
        self.wire = bytearray()
        self.dhav = bytearray()
        self.started = False

    def feed(self, chunk: bytes) -> list[bytes]:
        self.wire.extend(chunk)
        payloads = []
        while len(self.wire) >= 4:
            if self.wire[0] != 0x24:
                marker = self.wire.find(b"$")
                if marker < 0:
                    self.wire.clear()
                    break
                del self.wire[:marker]
                if len(self.wire) < 4:
                    break
            size = int.from_bytes(self.wire[2:4], "big")
            if len(self.wire) < 4 + size:
                break
            packet = bytes(self.wire[4 : 4 + size])
            del self.wire[: 4 + size]
            parsed = _rtp_payload(packet)
            if parsed is not None and parsed[0] == 98:
                self.dhav.extend(parsed[2])
                payloads.extend(self._drain_dhav())
        return payloads

    def _drain_dhav(self) -> list[bytes]:
        output = []
        while True:
            if not self.dhav.startswith(b"DHAV"):
                marker = self.dhav.find(b"DHAV")
                if marker < 0:
                    if len(self.dhav) > 3:
                        del self.dhav[:-3]
                    break
                del self.dhav[:marker]
            if len(self.dhav) < 24:
                break
            frame_length = int.from_bytes(self.dhav[12:16], "little")
            if frame_length < 32 or frame_length > 8 * 1024 * 1024:
                del self.dhav[:4]
                continue
            if len(self.dhav) < frame_length:
                break
            frame = bytes(self.dhav[:frame_length])
            if frame[-8:-4] != b"dhav" or int.from_bytes(frame[-4:], "little") != frame_length:
                del self.dhav[:4]
                continue
            del self.dhav[:frame_length]
            frame_type = frame[4]
            if frame_type not in (0xFC, 0xFD):
                continue
            if not self.started and frame_type != 0xFD:
                continue
            self.started = True
            extension_size = frame[22]
            payload_offset = 24 + extension_size
            payload_size = frame_length - 8 - payload_offset
            if payload_size < 0:
                continue
            payload = bytearray(frame[payload_offset : payload_offset + payload_size])
            if frame_type == 0xFD:
                info = _b5_info(frame[24:payload_offset])
                if info is not None:
                    clear, encrypted, iv = info
                    end = clear + encrypted
                    if end > len(payload):
                        raise RuntimeError("invalid encrypted keyframe range")
                    decryptor = Cipher(algorithms.AES(self.frame_key), modes.OFB(iv)).decryptor()
                    payload[clear:end] = decryptor.update(bytes(payload[clear:end])) + decryptor.finalize()
            output.append(bytes(payload))
        return output
