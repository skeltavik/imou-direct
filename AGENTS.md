# AGENTS.md

## Purpose

This repository contains **Imou Direct**, an experimental Home Assistant custom
integration for authorized Imou Doorbell 3 hardware. It replaces the Android
application at runtime, but it does not yet replace Imou's cloud transfer
service: Home Assistant logs in directly to Imou during setup, requests a
short-lived media transfer address at runtime, decodes the proprietary stream
locally, and exposes it as a normal `camera` entity.

Use this file as the working contract for changes in this repository. Preserve
the verified protocol behavior and privacy properties unless a task explicitly
changes them and includes evidence and tests for the new behavior.

## Start here

Before editing:

1. Run `git status --short` and preserve unrelated user changes.
2. Read `README.md` for the public product contract.
3. Read the module and its tests together; the protocol code contains several
   wire-compatibility details that look like mistakes but are intentional.
4. Decide whether the task affects the public HACS repository, the local
   reverse-engineering workspace, or both.
5. Never use live credentials or packet captures as unit-test fixtures.

The authoritative public implementation is
`custom_components/imou_direct/`. The top-level ignored `imou_direct/` folder
is an earlier research/runtime implementation; do not patch or publish it as a
substitute for the Home Assistant component.

## Repository boundary

### Public, tracked HACS repository

These files are intended for GitHub and releases:

- `custom_components/imou_direct/`: integration runtime and UI.
- `tests/`: offline regression tests with synthetic data and Home Assistant
  stubs.
- `.github/workflows/validate.yml`: Hassfest and HACS validation.
- `README.md`, `LICENSE`, `hacs.json`, `renovate.json`, and `.gitignore`.
- This `AGENTS.md`.

### Local, intentionally ignored research workspace

The following paths can contain proprietary binaries, decompiled sources,
device identifiers, local addresses, credentials, decrypted media, captures,
or one-off instrumentation:

- `analysis/`, `artifacts/`, `evidence/`, `runtime/`, `tmp/`, and `vendor/`.
- `tools/` and top-level `*_probe.py` / `test_imou*.py` scripts.
- `FINDINGS.md` and `REVERSE_ENGINEERING.md`.
- Top-level `imou_direct/` and `imou_direct_addon/` research implementations.
- `*.private.json` and `imou_direct.json`.

Use these local sources as evidence when appropriate, but do not stage them,
weaken `.gitignore` to publish them, or copy identifying/private values into
tracked code, tests, issues, commit messages, logs, or release notes. Before
every commit, inspect `git diff --cached --name-only` and
`git diff --cached --check`.

## Current architecture

The normal flow is:

```text
Home Assistant config flow
  -> ImouCloudClient login and device discovery
  -> bootstrap_from_device (password-free account session + device secrets)
  -> DirectStreamManager background worker
  -> signed transfer-URL request
  -> proprietary TLS/WSSE PLAY request
  -> RTP/DHAV assembly and encrypted HEVC frame recovery
  -> FFmpeg HEVC-to-H.264 HLS + JPEG snapshot
  -> loopback-only HTTP server
  -> Home Assistant camera/stream proxy
```

Module responsibilities:

- `config_flow.py`: user and reconfigure forms, FFmpeg validation, device
  selection, config-entry creation, and error mapping.
- `cloud.py`: synchronous one-time account bootstrap and device discovery using
  Imou's signed PCS API.
- `bootstrap.py`: decrypt device media fields and build the minimal validated
  runtime configuration without retaining the Imou account password.
- `core.py`: media transfer signing, PLAY construction, TLS transport, RTP and
  DHAV parsing, key derivation, and HEVC extraction.
- `manager.py`: reconnect loop, FFmpeg lifecycle, temporary files, loopback HLS
  server, health state, and cleanup.
- `camera.py`: Home Assistant entity surface only; it must not expose private
  configuration.
- `__init__.py`: config-entry setup/unload and platform forwarding.
- `diagnostics.py`: deliberately narrow, non-sensitive runtime health.
- `const.py`: config keys, defaults, and supported width range.

Keep these layers separated. In particular, do not move blocking network,
socket, filesystem, or subprocess work onto Home Assistant's event loop.

## Non-negotiable security and privacy invariants

1. The Imou account identifier and account password are transient config-flow
   inputs. They must not be stored in a config entry, object retained after
   setup, diagnostics, state attributes, logs, fixtures, or exceptions.
2. A config entry necessarily contains a session token and device stream
   credentials. Treat the complete `bootstrap` object as secret, even if some
   individual fields appear harmless.
3. Never log request headers, signed canonical strings, transfer URLs, PLAY
   requests, config-entry data, device serials, P2P tokens, device passwords,
   session IDs, or decrypted frames.
4. Public diagnostics may expose only coarse health data: connection state,
   frame age, and reconnect count. Adding identifiers, URLs, paths, headers, or
   exception messages requires a privacy review.
5. The generated HTTP server must remain bound to `127.0.0.1` on an ephemeral
   port. Browser access must continue through Home Assistant's authenticated
   camera proxy. Do not bind to `0.0.0.0`, add an unauthenticated add-on port,
   or advertise the intermediate HLS URL.
6. Temporary HLS segments and snapshots must be removed on unload. Every new
   process, thread, socket, server, and temporary directory needs a bounded and
   idempotent cleanup path.
7. Do not send project telemetry and do not introduce a project-operated
   backend. Current external traffic goes directly from Home Assistant to
   Imou's services and the returned media endpoint.
8. Work only with hardware/accounts the user owns or is authorized to test.
   Do not broaden probes to unrelated hosts or accounts.

Do not “improve debugging” by printing secret-bearing payloads. Prefer stable
error categories, exception type names, counters, and synthetic reproduction.

## Protocol invariants

This code implements an undocumented protocol recovered through controlled
interoperability research. Seemingly cosmetic changes can break authentication
or playback.

### PCS REST signing

- Preserve JSON serialization choices used for signing: UTF-8,
  `ensure_ascii=False`, and compact separators.
- Both MD5 and SHA-256 content digests/signatures are required. MD5 here is a
  protocol compatibility primitive, not a security recommendation; retain
  `usedforsecurity=False` where applicable.
- Canonical header names, order, conditional fields, casing, newline placement,
  and the final trailing newline are wire-significant.
- Generate fresh dates, nonces, request IDs, and signatures per request.
- Keep the single HTTP 412 server-date retry bounded; never create an unlimited
  retry loop.
- Validate response shape and convert transport, authentication, and protocol
  failures into the existing typed exceptions without including secrets.

### PLAY and transfer transport

- The capture-free PLAY builder is the default. `play_template_hex` and
  `transfer_hmac_key_hex` exist only for legacy v0.1 bootstrap compatibility.
- Preserve the server-derived `.rtpxav` path/query and append the verified
  `trackID=31` and `method=0` parameters exactly once.
- Preserve verified header spelling and casing, including the unusual
  `Accpet-Sdp` spelling. Do not “correct” it without a live protocol test.
- `Private-Length` describes the SDP request body. PLAY responses have been
  observed with either `Private-Length` or `Content-Length`; keep support for
  both.
- WSSE nonce, timestamp format, credential material, uppercase digest tokens,
  and `LightweightDigest` are protocol-sensitive. Never reuse captured dynamic
  values.
- TLS certificate and hostname verification are deliberately disabled only for
  the short-lived proprietary media endpoint, which presents a device/vendor
  certificate unsuitable for normal public PKI validation. Do not copy this
  TLS policy into cloud API calls or unrelated transports.

### RTP, DHAV, encryption, and FFmpeg

- Preserve incremental parsing: TCP/TLS chunks do not align with RTP or DHAV
  frames.
- Reject malformed sizes and tolerate resynchronization without allowing
  unbounded buffers or allocations.
- Payload type 98 and the DHAV `0xb5` extension currently carry the verified
  encrypted video path. Changes require synthetic boundary tests and a live
  capture-free playback check.
- Frame-key derivation and AES segment boundaries are wire behavior. Never
  replace the PBKDF2, hash, IV, or partial-encryption rules based on intuition.
- FFmpeg currently consumes raw HEVC at 15 fps, drops audio, produces H.264 HLS,
  and refreshes a JPEG snapshot. Document and test intentional changes to that
  media contract.
- Keep reconnects bounded and stoppable. A transfer ending or FFmpeg exiting
  should reconnect without leaking a child process or thread.

## Home Assistant integration invariants

- Minimum supported Home Assistant version is `2025.3.0` as declared in
  `hacs.json`.
- `manifest.json` must remain valid for a HACS custom integration and its
  `version` must match the next published release.
- If `ImouDirectConfigFlow` defines `__init__`, it must call
  `super().__init__()` before assigning its own fields. Missing this causes the
  config-flow endpoint to fail at runtime even when Hassfest passes.
- Config-flow schemas must use Home Assistant selectors or other values that
  `voluptuous_serialize` supports. Never put a custom Python validator directly
  in a displayed schema; normalize and validate those values after submission.
- Blocking login/discovery and FFmpeg validation must run through
  `hass.async_add_executor_job`. Do not call `urllib`, sockets, `shutil.which`,
  blocking waits, or file reads directly on the event loop.
- Use the device ID as the config-flow unique ID so the same device cannot be
  configured twice. Do not expose it as an entity attribute or diagnostic.
- Reconfigure must refresh secret/session material without storing account
  credentials and should preserve the configured device. Retain the safe
  single-device fallback for legacy entries that lack a stable target ID.
- Preserve v0.1 compatibility: entries with `config_path` load the legacy JSON
  file, while new/reconfigured entries use embedded `bootstrap` data. Do not
  remove this path without an explicit migration and release note.
- Setup failures must stop a partially constructed manager. Platform-forwarding
  failures and unloads must also clean up runtime resources.
- Keep `strings.json`, `translations/en.json`, and `translations/nl.json`
  structurally synchronized whenever config-flow text, fields, errors, or abort
  reasons change.
- Keep camera state and diagnostics free of secrets. Entity availability should
  remain based on manager health, not direct network work in entity properties.

Home Assistant's HACS/Hassfest checks validate metadata and structure, but they
do not prove that a config flow can be instantiated or that a camera can play.
Maintain targeted runtime regression tests for those boundaries.

## Coding conventions

- Follow the existing typed Python style and use `from __future__ import
  annotations` in component modules.
- Prefer standard-library implementations; `cryptography`, `voluptuous`, and
  Home Assistant APIs are already available through the host. Do not add a
  package dependency casually. If one is unavoidable, declare it correctly in
  `manifest.json` and justify its runtime/platform impact.
- Keep the cloud client synchronous and dependency-inject `urlopen` for offline
  testing. Invoke it from an executor at the Home Assistant boundary.
- Use explicit, typed exceptions at external boundaries. Catch broadly only at
  the background reconnect boundary, where failure must not kill the worker.
- Copy caller-supplied bootstrap dictionaries before mutation. Do not retain a
  reference that another layer can mutate unexpectedly.
- Prefer bounded timeouts and stop events over sleeps. All loops must have a
  clear stop/retry condition.
- Keep log messages operational and non-sensitive. Logging an exception class
  is normally safer than logging its vendor-provided message.
- Preserve the small public surface. Put protocol helpers next to the relevant
  transport rather than creating generic utility modules.
- Do not edit generated/decompiled sources under `analysis/` as if they were
  project source code.

## Testing workflow

The tracked test suite is intentionally offline. It imports component modules
directly and uses lightweight Home Assistant stubs, so it can run without a
full Home Assistant installation.

Run the complete local gate after every tracked code change:

```bash
ruff check custom_components tests
python3 -m unittest discover -s tests -v
python3 -m compileall -q custom_components tests
python3 -m json.tool custom_components/imou_direct/manifest.json >/dev/null
python3 -m json.tool custom_components/imou_direct/strings.json >/dev/null
python3 -m json.tool custom_components/imou_direct/translations/en.json >/dev/null
python3 -m json.tool custom_components/imou_direct/translations/nl.json >/dev/null
git diff --check
```

When modifying behavior, add or update a focused regression test:

- `test_cloud.py`: signing canonicalization, login/session behavior, API error
  mapping, date retry, and absence of account secrets.
- `test_bootstrap.py`: device response parsing, AES-GCM field decryption,
  bootstrap validation, and absence of account credentials.
- `test_config_flow.py`: form lifecycle, base-class initialization, executor
  calls, device selection, reconfigure behavior, and stored-data privacy.
- `test_core.py`: PLAY bytes, dynamic digest validation, length headers,
  incremental RTP/DHAV parsing, decryption boundaries, and malformed input.
- Add manager tests for lifecycle, loopback binding, HTTP allowlisting,
  reconnects, process cleanup, and temporary-file removal when changing
  `manager.py`.

For a regression, prefer red-green verification: demonstrate the failure with
a focused test, make the smallest correct change, then run the complete gate.

### Runtime verification levels

Use the least invasive level that proves the change:

1. **Offline unit tests**: mandatory for all logic changes.
2. **Home Assistant import/config-flow smoke test**: mandatory for changes to
   HA APIs, config-entry lifecycle, selectors, or platform setup when a suitable
   environment is available.
3. **Synthetic transport/FFmpeg test**: use generated, non-private byte streams
   for parser, manager, HTTP, or subprocess lifecycle changes.
4. **Authorized live-device test**: reserve for protocol, authentication,
   transfer, encryption, reconnect, or end-to-end playback changes. Confirm the
   target device/account, avoid printing secrets, store raw output only in
   ignored paths, and report whether the test was capture-free.

Do not claim “streaming works” from unit tests, Hassfest, or an HTTP 200 alone.
An end-to-end success requires fresh transfer acquisition, successful PLAY,
continuous decoded frames, FFmpeg output, a non-empty HLS playlist/snapshot,
and clean shutdown. If live hardware was not tested, say so explicitly.

## Troubleshooting guide

- **Config flow shows HTTP 500**: inspect the first Home Assistant traceback,
  verify imports against the minimum HA version, instantiate the flow in a
  test, and confirm the parent constructor runs. Hassfest alone is insufficient.
- **`invalid_auth`**: keep this distinct from connectivity and malformed vendor
  responses. Do not log the account, password, token, or vendor response body.
- **Entry setup fails immediately**: validate FFmpeg resolution, bootstrap
  shape, legacy path access, and cleanup after partial manager construction.
- **Camera unavailable**: distinguish transfer API failure, TLS/PLAY failure,
  parser/decryption failure, FFmpeg exit, and missing HLS output using safe
  stage-level diagnostics—not payload dumps.
- **PLAY returns non-200**: compare canonical request structure and dynamic
  values with known-good protocol tests before changing headers or cryptography.
- **PLAY returns 200 but no frames**: test incremental RTP/DHAV boundaries,
  payload type, child device selection, stream encryption mode, frame-key
  inputs, and FFmpeg process health.
- **Works once, then stalls**: exercise short-lived transfer expiry, reconnect
  cleanup, fresh nonce/request ID generation, and stopped-process replacement.
- **HLS works locally but not in HA**: keep the loopback server private and
  verify Home Assistant consumes `stream_source`; do not expose the port as a
  workaround.

## Documentation and product truth

Keep public claims precise:

- The integration is Android-free at runtime.
- Video processing and delivery are local.
- Account login/device discovery and current media transfer are still
  cloud-assisted; version 0.2.x is not offline or LAN-only.
- No usable standard ONVIF media profile or static RTSP route has been verified
  for the tested Doorbell 3/Chime combination.
- The current validated scope is an Imou Doorbell 3 plus Chime, HEVC input, and
  H.264 HLS output. Do not imply broad Imou model support without evidence.
- Imou is a third-party trademark and this project is unofficial.

Update `README.md` when behavior, setup steps, privacy boundaries, compatibility,
or limitations change. Do not copy private research notes verbatim into public
documentation; summarize only what is necessary and remove identifiers.

## Versioning, Git, and releases

- When a task calls for a new branch, use the `codex/` prefix unless the user
  explicitly requests another naming convention.
- Keep commits scoped and inspect staged content for private data.
- Do not push, tag, create a release, or change GitHub state unless the user has
  requested publication.
- For a release, update `custom_components/imou_direct/manifest.json`, run the
  full local gate, push the intended commit, wait for both HACS and Hassfest to
  pass, and only then create a non-draft version tag/release.
- Use semantic versioning: patch for compatible fixes, minor for new compatible
  behavior, and major for deliberate compatibility breaks.
- Release notes must state user-visible behavior and meaningful security or
  migration effects without including secret values or research artifacts.
- A green workflow does not replace local tests or runtime verification.

## Definition of done

A change is complete only when:

- the requested behavior is implemented in the authoritative tracked component;
- privacy, loopback binding, cleanup, async, and legacy invariants still hold;
- a focused regression test covers behavior that can be tested offline;
- the complete local gate passes;
- relevant English and Dutch strings and public documentation are synchronized;
- live-device claims are labeled as tested or untested with concrete evidence;
- `git status` and staged-file inspection show no accidental research artifacts
  or credentials; and
- publication steps, when requested, have completed and their CI/release status
  has been verified.
