# Imou Direct

<p align="center">
  <img src="custom_components/imou_direct/brand/icon@2x.png" alt="Imou Direct icon" width="160">
</p>

Experimental HACS custom integration that exposes an Imou Doorbell 3 as a
normal Home Assistant `camera` entity without Android at runtime.

The recovered stream path directly:

- signs the Imou media REST request and obtains a short-lived transfer URL;
- performs the proprietary TLS/WSSE `PLAY` handshake;
- reconstructs RTP/DHAV frames and decrypts encrypted HEVC keyframes;
- converts HEVC to H.264 HLS and a JPEG snapshot with FFmpeg; and
- hands the result to Home Assistant's authenticated camera/stream proxy.

This is a research prototype for hardware and accounts you own or are
authorized to test. It is currently validated with an Imou Doorbell 3 plus
Chime and is not a general replacement for the Imou Life account login flow.

## Prerequisite: private bootstrap file

The integration needs a private bootstrap JSON recovered from your own
authenticated Imou Life session. That file is intentionally not part of this
repository. It contains account and device credentials and must never be
committed, published, pasted into an issue, or stored in an untrusted backup.

Place it on the Home Assistant host as:

```text
/config/imou_direct.json
```

Restrict the file to the Home Assistant user where the installation permits
that. The integration reads it from disk and does not copy its contents into
the config-entry database, diagnostics, entity attributes, or logs.

The current bootstrap originates from an authenticated app session. Its full
lifetime is not yet known, so a future account-session expiry may require a
fresh export.

## Install with HACS

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/skeltavik/imou-direct` as category
   **Integration**.
3. Install **Imou Direct** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration**.
5. Select **Imou Direct** and keep the default private path above.

For manual installation, copy `custom_components/imou_direct` into
`/config/custom_components/imou_direct` and restart Home Assistant.

Home Assistant OS and Home Assistant Container include FFmpeg. Other install
types must provide an FFmpeg executable; its path can be changed in the setup
form.

## Security and runtime behavior

- The generated HLS service binds only to `127.0.0.1` inside Home Assistant.
- Browser clients use Home Assistant's authenticated camera proxy.
- Diagnostics contain only connection state, frame age, and reconnect count.
- Temporary HLS segments and snapshots are deleted when the integration unloads.
- No Android process, Supervisor add-on, open add-on port, Generic Camera entry,
  RTSP server, or ONVIF profile is required at runtime.

## Compatibility

- Home Assistant 2025.3 or newer
- HACS integration repository layout
- FFmpeg with `libx264`
- Currently tested: Imou Doorbell 3 stream at 1920×1920, transcoded to a
  configurable H.264 width (960 pixels by default)

Home Assistant 2026.3 and newer load the bundled integration icon locally.
Imou is a trademark of its respective owner. Imou Direct is an independent,
unofficial interoperability project and is not affiliated with or endorsed by
Imou.
