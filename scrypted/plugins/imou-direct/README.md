# Imou Direct for Scrypted

Experimental, evidence-gated Scrypted plugin architecture for an authorized Imou Doorbell 3 and DS3 Chime.

This is a first-class runtime target, not a Home Assistant wrapper. The intended
finished device must work directly in Scrypted as a doorbell with live video,
snapshots, inbound audio, physical ring events, and two-way audio. Home
Assistant is neither a runtime dependency nor a transport relay for this
plugin.

> [!IMPORTANT]
> This is currently an **offline-tested architecture baseline**, not a working local camera plugin. The local video, inbound-audio, doorbell-event, and talkback transports have not yet passed their separate live evidence gates. The plugin therefore does not advertise those Scrypted interfaces by default.

## Current behavior

The provider creates an Imou Doorbell device with coarse `Settings` and `Online` state only. These capabilities are independent and immutable at build time:

| Capability | Scrypted interface | Current gate |
|---|---|---:|
| Local video | `VideoCamera` | closed |
| Local inbound audio | media audio track | closed |
| Local button press | `BinarySensor` | closed |
| Local talkback | `Intercom` | closed |

The product-level completion gate requires all four capability gates above plus
snapshot delivery, reconnect/restart recovery, and accurate online state. A
working video stream alone is not sufficient to call the Scrypted doorbell
integration complete.

Users cannot override evidence gates in settings. Vendor-assisted or public media sessions are rejected at the media boundary and are never labelled local.

## Implemented contracts

- `CapabilityGate`: defaults every unverified capability to disabled.
- `RingEventController`: maps one validated local press to a one-second pulse, suppresses duplicates, and cleans up idempotently.
- `LocalMediaController`: accepts only validated LAN sessions, requires video, gates inbound audio independently, and closes rejected sessions.
- `IntercomController`: permits one talk session, handles stop-during-start, applies a maximum duration, and attempts transport and encoder cleanup.
- `FfmpegTalkbackEncoder`: defines the Scrypted microphone conversion boundary
  for G.711 A-law (`PCMA`), mono, 8 kHz. Those parameters match the recovered
  app-side talker setup, but their local wire framing and timing still require
  a live talkback test.
- Thin Scrypted provider/device adapter with evidence-derived interface advertisement.

No Android app, Frida agent, emulator, packet capture, cloud transfer endpoint, or Home Assistant component is included in the plugin runtime.

## Build and test

Requirements: Node.js 22 and npm.

```bash
npm ci --ignore-scripts
npm test
npm run typecheck
npm run build
```

A successful build produces `out/plugin.zip`. Build success proves only that the plugin packages correctly; it does not prove device connectivity or streaming.

## Evidence required before enabling interfaces

### Video

- fresh local session acquisition;
- non-empty current media bytes;
- continuous decodeable frames;
- clean reconnect and shutdown;
- no vendor relay or public endpoint.

### Inbound audio

- current local audio packets;
- proven framing, codec, sample rate, and channels;
- decodeable or audible output synchronized with video.

### Doorbell event

- current local button-press source;
- one physical press maps to one debounced pulse;
- reconnect/restart behavior proven without cloud push.

### Intercom

- proven local talk-session startup and authentication;
- accepted, correctly timed audio packets;
- audible Doorbell output from an explicitly authorized bounded test;
- repeated start/stop, timeout, disconnect, and cleanup tests;
- no synthetic tone or microphone payload may be sent without explicit authorization.

### Cross-runtime acceptance

- Scrypted connects to the local transport directly; it does not proxy through
  Home Assistant or require the HACS integration.
- Each advertised Scrypted interface has its own live-device evidence and
  lifecycle tests.
- Doorbell presses remain one-shot debounced events even across reconnects.
- Starting and stopping two-way audio cannot leak an encoder, transport,
  microphone source, or active talk session.
- A failure of inbound audio or intercom does not mislabel video-only media as
  a fully supported doorbell session.
- No cloud-assisted fallback may be presented as local operation.

A full-local claim additionally requires accountless bootstrap/runtime plus restart, reboot, and LAN-preserved IPv4 and IPv6 WAN-block tests.

## Security and privacy

- Do not log media descriptors, FFmpeg input arguments, URLs, headers, identifiers, credentials, tokens, packets, or decoded media.
- Keep evidence gates immutable and non-user-configurable.
- Never silently fall back to vendor-assisted transport while claiming local operation.
- Keep research artifacts and private fixtures outside this plugin.
- Do not enable a capability from static SDK strings or SDP hints alone.

## Status

```text
local_video=false
local_inbound_audio=false
local_button_event=false
local_talkback=false
full_local=false
```
