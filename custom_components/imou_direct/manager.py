"""Background transfer-stream manager for Imou Direct."""

from __future__ import annotations

from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

from .core import HevcExtractor, derive_frame_key, fetch_transfer_url, tls_play_bytes

_LOGGER = logging.getLogger(__name__)


def validate_bootstrap_file(path: str) -> dict:
    """Load a bootstrap file and validate only its public shape."""
    with Path(path).expanduser().open(encoding="utf-8") as source:
        config = json.load(source)
    if not isinstance(config, dict):
        raise ValueError("bootstrap root is not an object")

    required = {
        "rest": (
            "host",
            "uri",
            "content_type",
            "revision",
            "client_ua",
            "username",
            "md5_key",
            "sha256_key",
        ),
        "stream": (
            "username",
            "password",
            "device_sn",
            "transfer_hmac_key_hex",
            "play_template_hex",
        ),
    }
    for section, keys in required.items():
        values = config.get(section)
        if not isinstance(values, dict):
            raise ValueError(f"missing {section} section")
        if any(not isinstance(values.get(key), str) or not values[key] for key in keys):
            raise ValueError(f"missing field in {section} section")
    if not isinstance(config.get("request"), dict):
        raise ValueError("missing request section")
    return config


class StreamState:
    """Thread-safe runtime state that never exposes credentials or URLs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connected = False
        self._last_frame_at = 0.0
        self._reconnects = 0
        self._process: subprocess.Popen[bytes] | None = None

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def frame_received(self) -> None:
        with self._lock:
            self._connected = True
            self._last_frame_at = time.time()

    def reconnecting(self) -> None:
        with self._lock:
            self._connected = False
            self._reconnects += 1

    def set_process(self, process: subprocess.Popen[bytes] | None) -> None:
        with self._lock:
            self._process = process

    def terminate_process(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def public(self) -> dict[str, bool | float | int | None]:
        with self._lock:
            age = time.time() - self._last_frame_at if self._last_frame_at else None
            return {
                "connected": self._connected,
                "last_frame_age_seconds": round(age, 1) if age is not None else None,
                "reconnects": self._reconnects,
            }


def _ffmpeg_command(
    ffmpeg_bin: str, output: Path, width: int, hls_time: int
) -> list[str]:
    """Build the HEVC-to-HLS/snapshot FFmpeg command."""
    playlist = output / "stream.m3u8"
    snapshot = output / "snapshot.jpg"
    segment_pattern = output / "segment-%06d.ts"
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        "hevc",
        "-r",
        "15",
        "-i",
        "pipe:0",
        "-an",
        "-filter_complex",
        f"[0:v]split=2[h][s];[h]scale={width}:-2[hv];[s]fps=1/5,scale={width}:-2[sv]",
        "-map",
        "[hv]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-f",
        "hls",
        "-hls_time",
        str(hls_time),
        "-hls_list_size",
        "4",
        "-hls_segment_filename",
        str(segment_pattern),
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments",
        str(playlist),
        "-map",
        "[sv]",
        "-q:v",
        "3",
        "-update",
        "1",
        str(snapshot),
    ]


def _stream_worker(
    config: dict,
    output: Path,
    ffmpeg_bin: str,
    state: StreamState,
    stop: threading.Event,
) -> None:
    output_config = config.get("output", {})
    width = int(output_config.get("width", 960))
    hls_time = int(output_config.get("hls_time", 2))
    reconnect_delay = float(output_config.get("reconnect_delay", 3))

    while not stop.is_set():
        process: subprocess.Popen[bytes] | None = None
        try:
            for old_segment in output.glob("segment-*.ts"):
                old_segment.unlink(missing_ok=True)
            (output / "stream.m3u8").unlink(missing_ok=True)

            transfer_url = fetch_transfer_url(config)
            extractor = HevcExtractor(derive_frame_key(config))
            process = subprocess.Popen(
                _ffmpeg_command(ffmpeg_bin, output, width, hls_time),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            state.set_process(process)
            if process.stdin is None:
                raise RuntimeError("ffmpeg input unavailable")
            state.set_connected(True)

            for chunk in tls_play_bytes(config, transfer_url, stop=stop):
                if stop.is_set():
                    break
                for hevc in extractor.feed(chunk):
                    process.stdin.write(hevc)
                    process.stdin.flush()
                    state.frame_received()
                if process.poll() is not None:
                    raise RuntimeError("ffmpeg exited")

            if not stop.is_set():
                raise RuntimeError("transfer stream ended")
        except Exception as error:  # noqa: BLE001 - reconnect boundary
            if not stop.is_set():
                state.reconnecting()
                _LOGGER.warning("Imou stream reconnecting: %s", type(error).__name__)
        finally:
            state.set_connected(False)
            state.set_process(None)
            if process is not None:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        stop.wait(reconnect_delay)


def _handler_factory(directory: Path, state: StreamState):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, format: str, *args) -> None:
            _LOGGER.debug("Local stream HTTP: " + format, *args)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            path = urllib.parse.urlsplit(self.path).path
            if path == "/health":
                payload = json.dumps(state.public(), separators=(",", ":")).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/":
                self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                self.send_header("Location", "/stream.m3u8")
                self.end_headers()
                return
            name = path.removeprefix("/")
            if name not in {"stream.m3u8", "snapshot.jpg"} and not (
                name.startswith("segment-") and name.endswith(".ts")
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not (directory / name).is_file():
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "stream starting")
                return
            super().do_GET()

    return Handler


class DirectStreamManager:
    """Own the direct Imou connection, transcoder, and loopback HLS server."""

    def __init__(self, config: dict, ffmpeg_bin: str) -> None:
        self._config = config
        self._ffmpeg_bin = ffmpeg_bin
        self._output = Path(tempfile.mkdtemp(prefix="imou-direct-"))
        self._state = StreamState()
        self._stop = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None

    @property
    def stream_url(self) -> str:
        if self._server is None:
            raise RuntimeError("stream manager is not started")
        return f"http://127.0.0.1:{self._server.server_port}/stream.m3u8"

    def start(self) -> None:
        """Start the loopback server and stream worker."""
        if self._server is not None:
            return
        executable = shutil.which(self._ffmpeg_bin)
        if executable is None:
            raise FileNotFoundError("ffmpeg executable not found")
        self._ffmpeg_bin = executable

        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_factory(self._output, self._state)
        )
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="imou-direct-http",
            daemon=True,
        )
        self._worker_thread = threading.Thread(
            target=_stream_worker,
            args=(
                self._config,
                self._output,
                self._ffmpeg_bin,
                self._state,
                self._stop,
            ),
            name="imou-direct-stream",
            daemon=True,
        )
        self._server_thread.start()
        self._worker_thread.start()

    def stop(self) -> None:
        """Stop all runtime work and remove transient media files."""
        self._stop.set()
        self._state.terminate_process()
        if self._server is not None:
            if self._server_thread is not None and self._server_thread.is_alive():
                self._server.shutdown()
            self._server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=3)
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=7)
        self._server = None
        shutil.rmtree(self._output, ignore_errors=True)

    def health(self) -> dict[str, bool | float | int | None]:
        return self._state.public()

    def wait_stream_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        playlist = self._output / "stream.m3u8"
        while time.monotonic() < deadline:
            if playlist.is_file() and playlist.stat().st_size:
                return True
            if self._stop.wait(0.1):
                return False
        return False

    def snapshot_bytes(self) -> bytes | None:
        snapshot = self._output / "snapshot.jpg"
        try:
            return snapshot.read_bytes()
        except FileNotFoundError:
            return None
