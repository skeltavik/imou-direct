# Imou Intercept (prototype)

Local tap for the Imou Chime upload stream. No cloud, no app.

## What it does

When the doorbell is awake, the Chime pushes live video upstream to the
vendor server (TCP 9112, custom binary framing). This add-on reads that
stream from an operator-controlled capture path, decrypts it with the
device frame key, and serves Annex-B HEVC NAL units to loopback clients
over a length-prefixed TCP protocol (default port 49511). The Imou Direct
Home Assistant integration consumes it as its first `intercept` candidate.

## Capture modes

- `mirror` (default): passively read mirrored Chime traffic. Configure the
  mirror on your router or hypervisor (e.g. Proxmox/UniFi port mirroring
  toward this host). Fail-open: removing the mirror changes nothing for
  the Chime.
- `arp`: read traffic while an operator-run ARP spoof redirects it. Prefer
  `mirror`. Requires `NET_RAW`; the add-on never spoofs by itself.

The add-on never touches the Chime or the network. If it stops, the
Chime's own cloud path is unaffected.

## Options

| Option | Meaning |
|---|---|
| `chime_ip` | Chime LAN address (capture filter only) |
| `upload_port` | Chime upload port (default 9112) |
| `listen_port` | Annex-B serve port (default 49511) |
| `capture_mode` | `mirror` or `arp` |
| `frame_key_hex` | 32-byte device frame key as hex (secret) |

Get `frame_key_hex` from your Imou Direct bootstrap (`stream.frame_key_hex`
in the private device config). It never appears in logs.

## Repository layout (for future split)

This prototype lives under the ignored `imou_direct_addon/` workspace. To
publish as a standalone add-on repository, move `config.yaml`,
`Dockerfile`, `rootfs/`, and this README to the new repo root unchanged.
