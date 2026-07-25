# Changelog

All notable changes to Scottina are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Scottina Light table index** (`TABLES.md` §5b/§5c) — a browse-then-decode
  export alongside the existing flat decode-only tables, so Light's CAN gauge
  can present a PGN picker on a 192 KB device before decoding:
  - a compact **index** (`<name>.index.json`: abbreviated keys, names + rate
    budget only) and **per-PGN detail** files (`pgn-<num>.json`: full §2
    field definitions, loaded one at a time). Fast-packet and PDU1 PGNs are
    excluded at export time and rejected again on read (two gates).
  - one **shared writer** (`tables/lightindex.py`) called from both export
    paths — the web *Installed → index* download and the Files *Tables → USB*
    push — so they emit byte-identical artifacts; derived from the store on
    every export, `sha256`-bound to the source table, never hand-maintained.
  - `TABLES.md` §2 gains an optional `interval_ms` (Canboat
    `TransmissionInterval`); `tables/validate.py` range-checks it and
    validates the index shape (`validate_index` / `check_pair`).
- **GPS integration** (contract in `GPS.md`; Adafruit Ultimate GPS PA1616S
  on the PL2303 dongle udev-pinned to USB port 1-1 → `/dev/gps0` — the
  dongle has no serial number, so the physical port IS the identity):
  - **Phase 0 bring-up** — `setup/99-kilodash-serial.rules` (the port→device
    registry lives in the rule-file header) and `gps/pa1616s.py`: two-baud
    probe (115200 then factory 9600 — no backup battery today means every
    boot is a cold start; the probe also handles the battery-installed
    future), checksummed + `PMTK001`-ack-checked config to 115200 / 10 Hz /
    RMC+GGA+GSA every fix with GSV every 5th (the MTK divisor caps at 5,
    not the hoped-for 10). Runs as gpsd's `ExecStartPre`; a udev-triggered
    replug hook restarts gpsd (fresh module = factory defaults again).
  - **Phase 1 plumbing** — gpsd bound to `/dev/gps0` only (`USBAUTO=false`;
    autodiscovery would grab CanTick's console or Light's dock port),
    chrony SHM refclock with root-distance-based fallback ordering (NTP
    wins while the network exists, GPS when disconnected — no PPS, serial
    honesty only), and `gps/snapshotd.py` writing the **position snapshot
    contract** `/run/kilodash/gps/position.json` at 1 Hz (atomic rename,
    crash-only; GPS.md §3 staleness turns any death into "no fix").
    Consumers share ONE reader, `gps/snapshot.py::read_position()`;
    candump/N2K exports now gain `.meta.json` geotag sidecars (§4).
    Light Dock clock pushes gain the honest `gps` quality flag
    (DOCK-PROTOCOL.md v1.1 amendment + two new conformance vectors:
    `ntp ≥ gps > rtc > unsynced`, underclaim-on-reject for v1 Lights).
  - **GPS tile** (`kilodash/screens/gps.py`, hotplug key `gps`) — phosphor
    sky plot (az/el polar, dots sized/shaded by SNR, used vs visible) over
    fix/sats/HDOP/position/SOG/COG/UTC and the chrony "am I the time
    authority right now" line; sky repaints on SKY cadence, status only on
    change. Reads gpsd directly (GPS.md §5) via `gps/gpsdio.py`.
  - **N2K GNSS source node** (`n2k/node.py` + `n2k/fastpacket_tx.py`) — the
    box becomes a *real bus participant* only on the NMEA2K tile's
    **Source GNSS → bus** button: full ISO 11783-5 address claim (NAME:
    Marine / class 60 / function 145, arbitrary-address-capable; preferred
    SA persisted in `state/n2k_sa.json`), claim defense (lower NAME wins —
    defend or move; exhaustion → cannot-claim, surfaced, silent), ISO
    requests for 60928 answered any time, and five PGNs from LIVE gpsd
    (never the snapshot — no double-staleness): 126992 @1 Hz, 129025 +
    129026 @10 Hz, 129029 @1 Hz (the ecosystem's first outbound
    fast-packet, round-trip-tested against our own RX reassembly), 126993
    @60 s. Auto-stop on fix loss (address kept for quick resume; explicit
    stop = full re-claim next time). Own-source rows are ▸-tagged in the
    decode view — self-echo verifies TX but is never mistaken for the
    boat's GPS.
  - **TX allow-list carve-out** (`tests/test_txscan.py`) — the AST scan
    evolved from "no TX anywhere" to a tree-wide positive allow-list with
    exactly one CAN-TX module (`n2k/node.py`); any send-shaped socket call
    elsewhere fails the build, self-tested with synthetic offenders. The
    per-module RX-only scans stay as the independent reject pass.
  - **GPS-vs-bus comparison** (Phase 4 payoff) — decoded position PGNs
    from *other* sources get a threshold badge in the field breakdown
    (green < 10 m, amber < 50 m, red beyond; SOG/COG deltas too,
    unit-normalized), computed against the local snapshot and excluding
    our own claimed SA. Installer: `setup/install-gps.sh`.

- **Light Dock** (contract in `DOCK-PROTOCOL.md`, mirrored verbatim with the
  [Scottina-Light](https://github.com/scottmclesly/Scottina-Light) firmware
  repo; shared conformance asset `To-DoLists/dock-vectors.json`):
  - **Sync engine** (`kilodash/lightdock.py`) — dock Scottina Light on USB
    and it syncs itself: HELLO → clock push (honest quality: `ntp` only
    while NTP is synchronized *right now*, never laundered) → decode-table
    push (TABLES.md §5 export shape, name+sha256 diff, staged PUT + atomic
    verify-then-COMMIT; Prime always wins) → log pull into
    `captures/light-<name>` (checksum-verified, **deleted from Light only
    after proof of receipt**) → BYE. Every request timeout-bounded; §2
    resync scanner never allocates on an unvalidated length; redock just
    reruns the diff — no resume state on either side. `max_payload` is
    negotiated in HELLO, so chunk sizes stay parameterized until the
    Phase-0 throughput number lands.
  - **Light Dock screen** (`kilodash/screens/lightdock.py`, hotplug key
    `scottinalight` — Seeed VID 2886:802d + product string, never the ACM
    index) — two-distance UI: across-the-room phosphor animation (pulses
    riding the cable while syncing, the hug when complete, sad face +
    broken cable on interruption) as the only dirty-rect region between
    log lines, over a session-only log of the engine's lines verbatim,
    including the logging-suspended statement (§6). Controls: **Re-sync**
    and the **auto-pull-logs** toggle (`lightdock_pull_logs`, also in
    Settings). Redock while open restarts the sync automatically.
  - **Fake Light** (`tests/fakelight.py`) — a vector-pinned protocol
    responder on a PTY; the engine's whole test suite
    (`tests/test_lightdock.py`) runs against it with no firmware in the
    loop, per DOCK-PROTOCOL.md §10. User guide `docs/LIGHTDOCK.md`.

- **CAN / NMEA2K split + Tables converter** (contract in `TABLES.md`):
  - **CAN screen refactor** — raw-bus forensics for reverse-engineering:
    seen-IDs table (count/rate/last payload/changed-bytes highlight since
    the previous frame), tap-a-row **byte grid** with per-byte watches in
    two modes (change-detection and value-match; non-modal badge + row
    flash), candump-style live list, bounded 50k-frame ring log with
    ID/watched/changed filters exported as replayable candump `.log`.
    RX-only enforced in code: `tests/test_busmon.py` AST-scans the screen
    and its model (`kilodash/busmon.py`) for TX-shaped calls every run.
    Existing controls (bitrate/autodetect/logging/CanTick hosting) live on
    the Setup tab unchanged.
  - **NMEA2K screen** (`kilodash/screens/n2k.py`, decode core
    `kilodash/n2k.py`) — table-driven semantic decode: fast-packet
    reassembly → PGN lookup → field extraction; per-(PGN, source) rows with
    tap-through field breakdown; **range-exit** (transition-fired) and
    **appearance** alerts; unknown PGNs counted + one-tap handover to the
    CAN screen; bounded decoded log exported as JSON lines.
  - **Table store contract** (`TABLES.md`, `tables/validate.py`,
    `tables/store.py`) — Canboat-JSON subset, per-table manifest sidecar
    (sha256, enabled flag, pgn_count), consumers-read/converter-writes
    discipline, shared validator run on ingest *and* on load, flat
    SD-export shape feeding Scottina Light.
  - **Tables converter web app** (`kilodash/tableconv.py`, Flask, port
    8735) — PDF → side-by-side human review → validate → atomic install;
    Installed tab (enable/disable/remove/download/manifest + inbox
    ingest); DBC tab stubbed. On-demand systemd unit
    (`kilodash-tables.service`) with a 15-min idle self-shutdown that
    counts in-flight conversions as activity; uploads size-capped +
    magic-checked, PDF parsing crash-isolated in a subprocess
    (`kilodash/pdfextract.py`).
  - **Tables tile** (`kilodash/screens/tables.py`) — thin remote control +
    mirror: service status with idle countdown, URL **+ QR code** at the
    advertised address, manifest-only inventory (tap = atomic enabled
    flip, ✕ = remove). `kilodash/net.py::advertise_addr()` (eth0-preferred)
    is the shared address helper the CanTick work reuses.
  - Files screen **Tables → USB** now exports the installed `pgn/` store
    (tables + manifests) flat — the Scottina Light SD shape.
  - Installer `setup/install-tables.sh`; user guide `docs/NMEA2K.md`.

- **CanTick WiFi-CAN bridge** (`kilodash/cantick.py`, contract in
  `PROTOCOL.md`): a CanTick dialing in over WiFi appears as an ordinary
  `slcan0` — supervised `socat`+`slcand` pair on TCP 29536 with automatic
  relaunch/backoff, read-only UDP 29537 heartbeat (freshness badge, live rx/s,
  drop-rising warning, contract-version check), one-time USB provisioning
  push (CTK1 framing, CRC-16/CCITT-FALSE, base64 creds, PSKs never logged),
  and a reversible `hostapd`+`dnsmasq` fallback AP on `wlan0` for
  no-uplink diagnostics (`Scottina-CanTick` @ 192.168.42.1, bench-proven
  full up/restore cycle). CAN screen gains a CanTick source chip, a
  heartbeat health card, and a Provision button when a CanTick is on USB.

### Changed

- **LAN Scan tile refactor** (`kilodash/screens/lan.py`, `kilodash/scan.py`):
  - **Discover** now renders one tappable card per host instead of cramped
    text rows: the card frame is colour-coded up (green) / down (red), line 1
    carries the IP (accent) and MAC (muted) with colour separation, and line 2
    is the identity: reverse-DNS name, else the MAC **vendor** (e.g. "Raspberry
    Pi Foundation"), else "(unknown host)". Tapping a card selects it as the
    port-scan target — its IP prepopulates the target field and the view jumps
    to **Ports**; switching back to **Discover** restores the local subnet CIDR.
    `scan.ScanJob` now exposes structured `hosts` (via `hosts_snapshot()`,
    including `vendor`) alongside the streamed text lines.
  - The "Complete · N host(s)" status line and the host-count badge are gone;
    the found-host count is shown on the **Run** button once a scan completes.
  - **Ports** mode gives the ports field the entire width (no more overlap with
    the host badge), showing just the port list (the field is tappable).
  - **Ports / Services / Identify** results are now structured, not raw text: a
    single-host scan is a flat, un-indented port list (no IP header, no "up"
    line); a multi-host scan reuses the Discover card pattern, one card per host
    with its ports/OS info listed inside. Identify surfaces "OS: no confident
    match" when `-O` can't fingerprint, so it reads as distinct from a bare
    port scan.
- **Services / Identify now actually return on a subnet.** Both were sweeping
  nmap's default top-1000 ports per host, which ground for minutes on a `/24`
  and looked like they did nothing. They are now pinned to `COMMON_PORTS` with
  a per-host `--host-timeout` (`scan.HOST_TIMEOUT`) so each host completes in
  seconds. (Identify still needs root for `-O`; the service runs as root.)

## [1.0.0] — 2026-07-05

First release. **Scottina** — the digital Swiss Army knife for hardware
developers: a tap-driven dashboard for a Raspberry Pi 5 with a 3.5" ILI9486
SPI touchscreen (480×320, ADS7846 resistive touch), rendering directly to
`/dev/fb0` with no X server.

### Added

**Core**
- Framebuffer UI engine (PIL + numpy straight to `/dev/fb0`) with a tile-grid
  Home screen, per-screen Back buttons, and ▲▼ scroll buttons for long lists.
- Touch input read directly from the ADS7846 evdev node, with runtime
  orientation settings (swap/invert axes, 180° flip) and an on-screen
  calibration helper.
- Config-driven Settings screen: every entry in `config.py::DEFAULTS` renders
  automatically as a toggle, stepper, or choice cycler. Includes power actions
  (restart UI / reboot / shutdown) and an About card.
- Themeable look with a boot splash (animated `ScottinaSplash.gif`) shown the
  moment the app owns the framebuffer, plus an idle screensaver dim timeout.
- `kilodash.service` systemd unit and setup scripts for kiosk boot.

**Screens**
- **System health** — CPU, memory, temperature, and disk at a glance.
- **LAN tools** — network status and host discovery.
- **Wi-Fi** — scanning, plus a monitor-mode sniffer that keeps the uplink
  alive while capturing.
- **RTL-SDR** — spectrum capture with signal identification.
- **I²C scanner** — bus probing for attached devices.
- **CAN bus** — traffic monitor.
- **Serial monitor** — live view of serial ports.
- **Pomodoro timer** — because hardware work needs breaks too.

**Web-app launch panels** (Phase 4 framework)
- **Kismet**, **Node-RED**, **AIS-catcher**, and **Signal K** screens that
  start/stop the backing service and front its web UI.

**Project**
- MIT license — created by Scott McLeslie for the benefit of all living
  beings; free to share and contribute.
- README, roadmap, and install scripts for side-loaded apps.

[1.0.0]: https://github.com/scottmclesly/Scottina/releases/tag/v1.0.0
