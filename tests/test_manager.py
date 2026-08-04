"""Offline lifecycle tests for local/cloud stream selection."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import time
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
        self.stdin._stop.set()

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
        local_decode_error: bool = False,
        local_stall: float = 0,
        local_frame_timeout: float = 0,
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
                if local_stall:
                    deadline = time.monotonic() + local_stall
                    while time.monotonic() < deadline and (
                        stop is None or not stop.is_set()
                    ):
                        time.sleep(0.005)
                yield from local_chunks or []

        class FakeExtractor:
            def __init__(self, frame_key: bytes) -> None:
                self.frame_key = frame_key

            def feed(self, chunk: bytes) -> list[bytes]:
                if self.frame_key == b"local-key" and local_decode_error:
                    raise RuntimeError("synthetic parser failure")
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
                "local_frame_timeout": local_frame_timeout,
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

    def test_local_no_frame_timeout_expires_while_transport_is_silent(self) -> None:
        started = time.monotonic()
        calls, writes = self._run_worker(
            "local_first",
            local_stall=0.25,
            local_frame_timeout=0.02,
        )

        self.assertLess(time.monotonic() - started, 0.15)
        self.assertEqual(writes, [b"cloud-frame"])
        self.assertEqual(
            calls,
            ["local_init", "local_stream", "cloud_fetch", "cloud_stream"],
        )

    def test_local_first_falls_back_when_local_parser_fails(self) -> None:
        calls, writes = self._run_worker(
            "local_first",
            local_chunks=[b"malformed-local-data"],
            local_decode_error=True,
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


class ManagerLifecycleTests(unittest.TestCase):
    def test_stop_retains_output_and_reports_live_worker(self) -> None:
        class LiveThread:
            def is_alive(self) -> bool:
                return True

            def join(self, timeout=None) -> None:
                return None

        manager = _MANAGER.DirectStreamManager({}, "ffmpeg")
        output = manager._output
        (output / "stream.m3u8").write_text("synthetic", encoding="utf-8")
        manager._worker_thread = LiveThread()

        with self.assertRaisesRegex(RuntimeError, "worker did not stop"):
            manager.stop()

        self.assertTrue(output.exists())
        manager._worker_thread = None
        manager.stop()
        self.assertFalse(output.exists())

    def test_start_failure_cleans_partial_runtime(self) -> None:
        events: list[str] = []

        class FakeServer:
            server_port = 1234
            daemon_threads = False

            def __init__(self, *_args, **_kwargs) -> None:
                events.append("server_created")

            def serve_forever(self) -> None:
                return None

            def shutdown(self) -> None:
                events.append("server_shutdown")

            def server_close(self) -> None:
                events.append("server_close")

        class FakeThread:
            count = 0

            def __init__(self, *_args, **_kwargs) -> None:
                self.index = FakeThread.count
                FakeThread.count += 1
                self.started = False
                self.alive = False

            def start(self) -> None:
                if self.index == 1:
                    raise RuntimeError("synthetic thread-start failure")
                self.started = True
                self.alive = True

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout=None) -> None:
                if not self.started:
                    raise RuntimeError("cannot join thread before it is started")
                events.append(f"thread_{self.index}_joined")
                self.alive = False

        originals = (
            _MANAGER.shutil.which,
            _MANAGER.ThreadingHTTPServer,
            _MANAGER.threading.Thread,
        )
        _MANAGER.shutil.which = lambda _name: "/synthetic/ffmpeg"
        _MANAGER.ThreadingHTTPServer = FakeServer
        _MANAGER.threading.Thread = FakeThread
        manager = _MANAGER.DirectStreamManager({}, "ffmpeg")
        output = manager._output
        try:
            with self.assertRaisesRegex(RuntimeError, "thread-start"):
                manager.start()
        finally:
            (
                _MANAGER.shutil.which,
                _MANAGER.ThreadingHTTPServer,
                _MANAGER.threading.Thread,
            ) = originals

        try:
            self.assertIsNone(manager._server)
            self.assertIsNone(manager._server_thread)
            self.assertIsNone(manager._worker_thread)
            self.assertFalse(output.exists())
            self.assertIn("server_shutdown", events)
            self.assertIn("server_close", events)
            self.assertIn("thread_0_joined", events)
        finally:
            manager.stop()

    def test_server_thread_start_failure_cleans_partial_runtime(self) -> None:
        events: list[str] = []

        class FakeServer:
            server_port = 1234
            daemon_threads = False

            def __init__(self, *_args, **_kwargs) -> None:
                events.append("server_created")

            def serve_forever(self) -> None:
                return None

            def shutdown(self) -> None:
                events.append("server_shutdown")

            def server_close(self) -> None:
                events.append("server_close")

        class FakeThread:
            count = 0

            def __init__(self, *_args, **_kwargs) -> None:
                self.index = FakeThread.count
                FakeThread.count += 1
                self.started = False

            def start(self) -> None:
                if self.index == 0:
                    raise RuntimeError("synthetic server-thread-start failure")
                self.started = True

            def is_alive(self) -> bool:
                return self.started

            def join(self, timeout=None) -> None:
                if not self.started:
                    raise RuntimeError("cannot join thread before it is started")
                events.append(f"thread_{self.index}_joined")
                self.started = False

        originals = (
            _MANAGER.shutil.which,
            _MANAGER.ThreadingHTTPServer,
            _MANAGER.threading.Thread,
        )
        _MANAGER.shutil.which = lambda _name: "/synthetic/ffmpeg"
        _MANAGER.ThreadingHTTPServer = FakeServer
        _MANAGER.threading.Thread = FakeThread
        manager = _MANAGER.DirectStreamManager({}, "ffmpeg")
        output = manager._output
        try:
            with self.assertRaisesRegex(RuntimeError, "server-thread-start"):
                manager.start()
        finally:
            (
                _MANAGER.shutil.which,
                _MANAGER.ThreadingHTTPServer,
                _MANAGER.threading.Thread,
            ) = originals

        try:
            self.assertIsNone(manager._server)
            self.assertIsNone(manager._server_thread)
            self.assertIsNone(manager._worker_thread)
            self.assertFalse(output.exists())
            self.assertIn("server_close", events)
            self.assertNotIn("server_shutdown", events)
            self.assertNotIn("thread_0_joined", events)
            self.assertNotIn("thread_1_joined", events)
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main()
