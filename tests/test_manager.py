"""Offline lifecycle tests for local/cloud stream selection."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest

_COMPONENT = Path(__file__).parents[1] / "custom_components" / "imou_direct"
_PACKAGE = types.ModuleType("imou_direct_manager_test")
_PACKAGE.__path__ = [str(_COMPONENT)]
sys.modules[_PACKAGE.__name__] = _PACKAGE


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE.__name__}.{name}", _COMPONENT / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("const")
_load("core")
_load("lan")
_MANAGER = _load("manager")


class _FakeInput:
    def __init__(self, stop: threading.Event, writes: list[bytes]) -> None:
        self._stop = stop
        self._writes = writes

    def write(self, value: bytes) -> None:
        self._writes.append(value)
        self._stop.set()

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, stop: threading.Event, writes: list[bytes]) -> None:
        self.stdin = _FakeInput(stop, writes)
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> int:
        return 0


class ManagerTransportTests(unittest.TestCase):
    def _run_worker(
        self,
        mode: str,
        *,
        local_chunks: list[bytes] | None = None,
        local_error: bool = False,
        lan_config: bool = True,
        local_decodes: bool = True,
    ) -> tuple[list[str], list[bytes]]:
        calls: list[str] = []
        writes: list[bytes] = []
        stop = threading.Event()

        class FakeLocal:
            def __init__(self, _config) -> None:
                calls.append("local_init")

            @property
            def frame_key(self) -> bytes:
                return b"local-key"

            def stream(self, stop=None):
                calls.append("local_stream")
                if local_error:
                    raise _MANAGER.LanP2PError("synthetic local failure")
                yield from local_chunks or []

        class FakeExtractor:
            def __init__(self, frame_key: bytes) -> None:
                self.frame_key = frame_key

            def feed(self, chunk: bytes) -> list[bytes]:
                if self.frame_key == b"local-key" and not local_decodes:
                    return []
                return [chunk]

        originals = {
            "LanP2PTransport": _MANAGER.LanP2PTransport,
            "has_lan_config": _MANAGER.has_lan_config,
            "HevcExtractor": _MANAGER.HevcExtractor,
            "derive_frame_key": _MANAGER.derive_frame_key,
            "fetch_transfer_url": _MANAGER.fetch_transfer_url,
            "tls_play_bytes": _MANAGER.tls_play_bytes,
            "Popen": _MANAGER.subprocess.Popen,
        }
        _MANAGER.LanP2PTransport = FakeLocal
        _MANAGER.has_lan_config = lambda _config: lan_config
        _MANAGER.HevcExtractor = FakeExtractor
        _MANAGER.derive_frame_key = lambda _config: b"cloud-key"

        def fetch(_config):
            calls.append("cloud_fetch")
            return "synthetic-transfer"

        def cloud_stream(_config, _url, stop=None):
            calls.append("cloud_stream")
            yield b"cloud-frame"

        _MANAGER.fetch_transfer_url = fetch
        _MANAGER.tls_play_bytes = cloud_stream
        _MANAGER.subprocess.Popen = lambda *_args, **_kwargs: _FakeProcess(
            stop, writes
        )
        config = {
            "output": {
                "transport_mode": mode,
                "local_frame_timeout": 0,
                "reconnect_delay": 0,
            }
        }
        try:
            with tempfile.TemporaryDirectory() as directory:
                _MANAGER._stream_worker(
                    config,
                    Path(directory),
                    "ffmpeg",
                    _MANAGER.StreamState(),
                    stop,
                )
        finally:
            _MANAGER.LanP2PTransport = originals["LanP2PTransport"]
            _MANAGER.has_lan_config = originals["has_lan_config"]
            _MANAGER.HevcExtractor = originals["HevcExtractor"]
            _MANAGER.derive_frame_key = originals["derive_frame_key"]
            _MANAGER.fetch_transfer_url = originals["fetch_transfer_url"]
            _MANAGER.tls_play_bytes = originals["tls_play_bytes"]
            _MANAGER.subprocess.Popen = originals["Popen"]
        return calls, writes

    def test_local_only_never_calls_cloud(self) -> None:
        calls, writes = self._run_worker("local_only", local_chunks=[b"local-frame"])

        self.assertEqual(writes, [b"local-frame"])
        self.assertEqual(calls, ["local_init", "local_stream"])

    def test_local_first_falls_back_only_after_local_failure(self) -> None:
        calls, writes = self._run_worker("local_first", local_error=True)

        self.assertEqual(writes, [b"cloud-frame"])
        self.assertEqual(
            calls,
            ["local_init", "local_stream", "cloud_fetch", "cloud_stream"],
        )

    def test_local_first_falls_back_when_local_bytes_do_not_decode(self) -> None:
        calls, writes = self._run_worker(
            "local_first",
            local_chunks=[b"non-video-local-data"],
            local_decodes=False,
        )

        self.assertEqual(writes, [b"cloud-frame"])
        self.assertEqual(
            calls,
            ["local_init", "local_stream", "cloud_fetch", "cloud_stream"],
        )

    def test_local_first_legacy_config_uses_cloud(self) -> None:
        calls, writes = self._run_worker("local_first", lan_config=False)

        self.assertEqual(writes, [b"cloud-frame"])
        self.assertEqual(calls, ["cloud_fetch", "cloud_stream"])

    def test_cloud_only_does_not_construct_lan_transport(self) -> None:
        calls, writes = self._run_worker("cloud_only")

        self.assertEqual(writes, [b"cloud-frame"])
        self.assertEqual(calls, ["cloud_fetch", "cloud_stream"])


if __name__ == "__main__":
    unittest.main()
