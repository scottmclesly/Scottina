# Scottina

**A pocket-sized front panel for the diagnostic tools you already use.**

A fingertip control panel for a Raspberry Pi 5 running Kali Linux, driving a
3.5" ILI9486 SPI touchscreen (480×320, ADS7846 resistive touch). It boots
straight to a tap-driven dashboard — no keyboard, no mouse, no X server — and
fronts your network, radio, bus, and web-app tooling as finger-sized tiles.

<img width="1600" height="1200" alt="Scottina-frames-1" src="assets/scottina-frames.gif" />

## The gap it closes

Most bench diagnostics live at one of two extremes. Either you're on a headless
box squinting at a terminal over SSH, or you're hauling a laptop to the bench to
open a full-size GUI. Headless is a bit too little. The full interface is way too
much.

Here's the thing: for any single diagnostic question, you're usually pressing two
buttons of that tool and watching two numbers come back. The other 90% of the
interface is just in the way. You don't need the ribbon menus, the config panels,
the twelve tabs — you need to plug a thing in, glance at the 3.5" screen, and get
your answer.

Scottina is that middle ground. It's a simplified front panel for everyone's most
familiar diagnostic tools: the couple of controls you actually press and the
couple of datapoints you're actually watching, and nothing else. It doesn't
invent new tools — it gives the ones you already trust a front panel sized for the
one question you're asking.

It is **not** another Flipper Zero, a Marauder deployment, or a wardriving toy. It
shares some radios and some Kali tools with those, but the point isn't offense —
it's a shop multitool that makes well-known software glanceable.

## What it feels like in practice

- **Pulling a Pi off the rack without the usual dance.** No hooking up a screen
  and keyboard just to join Wi-Fi, no hunting for an Ethernet cable, no arp-scan
  through a forest of "Raspberry Pi Foundation" MACs to SSH in blind. Scottina
  joins networks headless from the touchscreen, and the moment it's connected the
  IP is right there in the header to SSH straight to.
- **A Node-RED screen as a manual CAN troubleshooting bench.** Wire one button to
  a keep-alive heartbeat; a teeter-totter feedback field shows the *origin* of the
  last heartbeat — you or the target — so when a link goes quiet you know who
  dropped first. Two fields show DBC-normalized throttle % and RPM (Node-RED runs
  cantools under the hood), and the other buttons fire the messages you're
  testing. Step through a complex CAN sequence by hand with snappy feedback.
- **A five-second "is my wireless alive?" check with the SDR.** Even receive-only,
  a preloaded table of expected signals plus the Pi 5's headroom turns "what's
  transmitting nearby, across protocols?" into a single button press.
- **New-board bring-up before writing driver code.** Tap the I2C scanner to
  confirm the sensor is actually acking at the address you expect — "is it the
  wiring or my code?" answered in five seconds.
- **Proving your own transmitter works.** Built a 433/915 MHz sensor? Point
  Scottina at the band and watch for your packets to show up and decode.

Two small touches that tie it together: **Scottina's own IP is always in the
header**, and every **web-app screen shows the exact `URL:port`** to open the full
interface — no guessing which port Node-RED or Kismet landed on today.

> **Name note:** the product is **Scottina**, hosted at
> [github.com/scottmclesly/Scottina](https://github.com/scottmclesly/Scottina).
> The Python package, install path (`/opt/kilodash`), and systemd unit keep the
> historical working name `kilodash` on purpose — renaming them buys nothing
> and risks breaking the service. Everything you *see* says Scottina.
>
> The established names are **Scottina** / **Scottina Prime** (the panel),
> **Scottina Light** (the small companion — earlier working name *Island*),
> **CanTick** (the CAN-over-WiFi box), and **kilodash** (the internal
> package). Nothing else — don't invent new variants.

## Why it works the way it does

- **Renders directly to `/dev/fb0`** (the `ili9486drmfb` framebuffer) with PIL +
  numpy, and **reads touch straight from the ADS7846 evdev node**. No SDL, no
  Xorg, nothing that can lose the DRM device. This is the one approach that was
  reliable on this panel.
- **All navigation is discrete taps.** Resistive touch is too noisy to tell a
  horizontal swipe from a vertical drag, so we don't try: Home is a tile grid,
  every other screen has a Back button, and long lists get real ▲▼ scroll
  buttons.
- **Touch orientation is a runtime setting**, not a compile-time constant, so the
  panel can be re-calibrated from the Settings screen or by editing
  `config.json` over SSH — no reboot, no code edits.
- **Everything tunable lives in `config.py::DEFAULTS`.** The Settings screen
  renders whatever it finds there, so adding a knob is a one-line change.

## Boot splash

On start-up Scottina paints [`ScottinaSplash.png`](ScottinaSplash.png) as a
curtain the instant it owns the framebuffer, so the boot gap reads as an
intentional splash rather than a blank panel. It holds for ~2.5 s (a tap lifts
it early) while the rest of the app initialises, then hands off to Home.

## Hardware / display config

The panel is brought up by a single, clean overlay in
`/boot/firmware/config.txt`:

```
dtparam=spi=on
dtoverlay=piscreen,drm,rotate=90     # rotate=90 == the panel flipped 180° from its old 270°
```

`rotate` is the only display knob that needs a reboot. Touch axes are handled in
software (see below), so you never touch the overlay for calibration. A backup
of the original config is saved next to it as `config.txt.kilodash-bak.*`.

> Note: this panel exposes **no controllable backlight** (`/sys/class/backlight`
> is empty), so "dimming" is a software screensaver that darkens the rendered
> image. If you later wire a PWM backlight, Scottina auto-detects the sysfs node
> and uses it.

## Install

```bash
sudo cp /opt/kilodash/kilodash.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kilodash
```

Runs as root (needs `/dev/fb0`, evdev, `nmcli`, and `arp-scan`'s raw sockets).

Core dependencies (already present on this build): `python3-pil`,
`python3-numpy`, `python3-evdev`, plus `nmcli`, `arp-scan`, and `vcgencmd`.
`python3-pygame` is **not** used. The web-app backends (Node-RED, AIS-catcher,
Signal K) and the SDR/Wi-Fi tools install separately — see
[Web-app launch terminal](#web-app-launch-terminal) and
[`setup/install-phase4.sh`](setup/install-phase4.sh). The logic-analyzer
stack (sigrok-cli + fx2lafw firmware + udev/group setup) installs with
[`setup/install-logic-analyzer.sh`](setup/install-logic-analyzer.sh) —
usage guide in [docs/LOGICANALISER.md](docs/LOGICANALISER.md).

## Using it

- **Tap a tile** on Home to open a tool; **tap Back** (top-left) to return.
- **Tap** buttons, list rows, and toggles. Targets are ≥44 px for fingers.
- **Scroll** long lists with the ▲▼ buttons at the lower-right; the percentage
  between them shows your position.
- **Dimming**: after the idle timeout the screen darkens (software); the next
  tap only wakes it, it doesn't also activate what you touched.
- Quit while testing from a USB keyboard with **ESC** or **q**.

### Home tiles & pictograms

Each tool has its own **Semiotic-Standard-inspired pictogram** (the geometric,
monochrome style of Ron Cobb's *Alien* corridor icons) instead of a plain dot,
so tiles are distinguishable at arm's length. Glyphs live in
[`kilodash/pictograms.py`](kilodash/pictograms.py); a screen names its glyph
with a `glyph = "…"` class attribute and unknown keys fall back to a filled dot.

Fixed tiles are always shown. **Device tiles appear only while their dongle is
plugged in** (hotplug, see `devices.py`) and carry a small green "live" badge.
**Web-app tiles appear only once the app is installed.**

<img width="1600" height="1200" alt="fon" src="assets/home-tiles.gif" />


## Screens

**Built-in (always present):**

| Screen | What it does |
|---|---|
| **LAN Scan** | Intent-based network diagnostics (diagnostics only — no offensive tooling). Pick a **target** (IP / hostname / CIDR) and one of four **modes**, then **Run**: <br>• **Discover** — which devices are alive on the subnet. <br>• **Ports** — is an expected port open on a host (curated common ports by default; enter your own in the Ports field). <br>• **Services** — what service + version each open port runs. <br>• **Identify** — best-effort OS guess (needs root; refuses gracefully otherwise). <br>Results stream into a scrolling pane; a badge counts discovered hosts. There is deliberately **no raw-flag entry** — the mode is the safety boundary. Full user guide: [docs/LANSCAN.md](docs/LANSCAN.md). |
| **Wi-Fi** | Enable/disable, scan SSIDs, tap to connect. Secured networks open the on-screen keyboard for the password; saved/open ones connect immediately. Full user guide: [docs/WIFI.md](docs/WIFI.md). |
| **Pi Health** | Temperature, CPU, memory, disk, uptime, Wi-Fi signal, throttling — each a labelled bar or value card. User guide: [docs/SYSTEM.md](docs/SYSTEM.md#pi-health). |
| **Pomodoro** | Focus/break timer that **keeps counting on a background thread** even when you're on another screen, and toasts each transition app-wide. User guide: [docs/SYSTEM.md](docs/SYSTEM.md#pomodoro). |
| **Settings** | Every tunable as a card (booleans toggle, ints step, choices cycle), plus power actions (Restart UI / reboot / shutdown) and a touch-calibration helper. User guide: [docs/SYSTEM.md](docs/SYSTEM.md#settings). |
| **Micro KVM** | Meshtastic/BLE T3 — **off-grid command plane (armed off-network only)**. Across-the-room **ARMED (off-grid)** / **DORMANT (home)** banner, BLE link state, last-heard node, and the bounded session log (command, sender, accept/reject reason, reply). The tile is a mirror only — executor, arm gate and BLE link live in `microkvm/`. Contract: [MICROKVM-PROTOCOL.md](To-DoLists/MICROKVM-PROTOCOL.md); guide: [docs/MICROKVM.md](docs/MICROKVM.md); install with [`setup/install-microkvm.sh`](setup/install-microkvm.sh). |

**Hotplug device screens (tile shows only while the device is present):**

| Screen | Device | What it does |
|---|---|---|
| **RTL-SDR** | RTL2832U dongle | Frequency **Scan** (`rtl_power` sweep → spectrum + peak), **Identify** (`rtl_433` decodes real ISM packets and names the device), per-band knowledge hints, and IQ **Capture** (RX-only, no replay). Full user guide: [docs/RTLSDR.md](docs/RTLSDR.md). |
| **WiFi Sniff** | ALFA (2nd adapter) | Passive monitor-mode capture with `airodump-ng` — every AP/client it hears (SSID, channel, encryption, signal). A watchdog keeps the Pi's own uplink (`wlan0`) connected the whole time. Passive only, no injection. Full user guide: [docs/WIFISNIFF.md](docs/WIFISNIFF.md). |
| **CAN** | CanTick/slcan0 (or CANable / gs_usb) | **Raw sniff, byte-watch alerts, candump logs.** Raw-bus forensics for proprietary traffic: a **seen-IDs table** (count, rate, last payload, changed-bytes highlight), tap a row → **byte grid** → per-byte watches (change-detection or value-match; badge + row flash, never modal), a bounded ring log with filters exported as replayable candump `.log` (SavvyCAN-loadable), plus the Setup tab: bitrate select/**autodetect**, continuous logging, and the **CanTick** WiFi bridge — see below. Full user guide: [docs/CANBUS.md](docs/CANBUS.md). |
| **NMEA2K** | CanTick/slcan0 + PGN tables | **Live decode, range/appearance alerts.** Semantic decode of known PGNs against tables from the Tables converter ([TABLES.md](TABLES.md)): fast-packet reassembly → PGN lookup → per-field values with units; alerts when a field **exits a range** or a configured PGN **appears at all**; unknown PGNs are counted, listed, and hand over to the CAN screen in one tap. Decoded log exports as JSON lines. Guide: [docs/NMEA2K.md](docs/NMEA2K.md). |
| **GPS** | PA1616S @ `/dev/gps0` (port 1-1) | **Time authority, geotag snapshots, sky plot, N2K GNSS source.** Adafruit Ultimate GPS on the PL2303 dongle udev-pinned to USB port 1-1 (*the GPS jack* — the dongle has no serial number, so the physical port IS the identity). Ecosystem plumbing first, tile second: **gpsd + chrony** make the box a disconnected time authority (serial-only, no PPS — tens-of-ms honesty), the **position snapshot contract** ([GPS.md](GPS.md)) geotags capture artifacts, and the tile shows a phosphor **sky plot** (per-sat az/el/SNR, used vs visible) over fix/HDOP/position/UTC and the chrony *"am I the time authority right now"* line. The **Source GNSS → bus** button lives on the NMEA2K tile (it's a bus action). Install with [`setup/install-gps.sh`](setup/install-gps.sh). |
| **I2C Scan** | onboard i2c-1 | `i2cdetect` on the Pi's bus with best-guess names for responding addresses. User guide: [docs/I2C-SERIAL.md](docs/I2C-SERIAL.md#i2c-scan). |
| **Serial** | FTDI / CP210x / CH340 | Lists USB-serial ports and gives a read-only live view of one at a chosen baud — handy for sniffing UART/debug output. User guide: [docs/I2C-SERIAL.md](docs/I2C-SERIAL.md#serial). |
| **Logic** | FX2LP (CY7C68013A) | Passive multi-channel digital capture + protocol decode (UART/I2C/SPI/CAN) via the packaged `sigrok-cli`/fx2lafw stack: 8 channels, up to 24 MHz, edge trigger, decoded annotations + per-channel activity strips. Every capture persists to `/opt/kilodash/captures/*.sr` for PulseView on a laptop. Install with [`setup/install-logic-analyzer.sh`](setup/install-logic-analyzer.sh); full user guide: [docs/LOGICANALISER.md](docs/LOGICANALISER.md). **3.3 V logic only** — the bare board has no input protection; series resistor / buffer / divider before probing anything near Scottina's 12 V wiring. |
| **Tables** | — | **Table converter service + inventory.** Always-visible remote control for the on-device converter web app (start/stop, URL **+ QR code** at the eth0-preferred address) and a mirror of the installed PGN tables (enable/disable = atomic manifest flip, remove). Conversion itself happens in a browser on a big screen: upload a vendor PDF → side-by-side review → human approval → validation → store. Contract: [TABLES.md](TABLES.md); guide: [docs/NMEA2K.md](docs/NMEA2K.md). Install with [`setup/install-tables.sh`](setup/install-tables.sh). |
| **Files** | USB stick | **Offload logs without a laptop:** plug in any USB stick and copy captures (`candump` logs, `.sr`, IQ, sniffs) from `/opt/kilodash/captures/` onto it — one per tap or all at once — with a sync-then-**Eject** button so it's always safe to pull. Also exchanges **CAN decode tables** (DBC, NMEA2000/canboat) between the stick and `/opt/kilodash/tables/`, where decoding tools read them — the *Tables → USB* push also emits **Scottina Light's browse index** ([TABLES.md](TABLES.md) §5b/§5c) alongside the flat tables, feeding Light's CAN gauge picker, not only Prime's own decode. Copies never delete the originals. Full user guide: [docs/FILES.md](docs/FILES.md). |
| **Light Dock** | Scottina Light (USB) | **Auto-sync on dock:** clock push (with an honest quality label — `ntp` only while NTP is actually synchronized), decode-table push (**Prime always wins**, atomic verify-then-commit), and black-box **log pull** into `captures/light-*` — checksum-verified, deleted from Light only after proof of receipt. Two-distance screen: an across-the-room animation (pulses = syncing, the hug = done, sad face + broken cable = come look) over a session-only log of exactly what landed. Wire contract: `DOCK-PROTOCOL.md`, mirrored verbatim in [Scottina-Light](https://github.com/scottmclesly/Scottina-Light). Full user guide: [docs/LIGHTDOCK.md](docs/LIGHTDOCK.md). |

**Web-app launch terminals** (see below): **Kismet**, **Node-RED**, **AIS**,
**Signal K**.

### CanTick — CAN over WiFi

<img width="1149" height="1098" alt="CanTick" src="assets/cantick.gif" />

CanTick is a small ESP32 box that clamps onto a CAN bus somewhere Scottina
isn't, and tunnels it over WiFi. The model is deliberately boring: **it's just
`slcan0`**. While the CAN screen is open, Scottina listens on TCP 29536; a
CanTick dials in, `slcand` attaches the stream as a normal SocketCAN
interface, and everything downstream — `candump`, the Node-RED SocketCAN node,
Signal K / canboatjs for NMEA2000 — reads `slcan0` with zero
interface-specific changes. The link is supervised: if the CanTick drops off
WiFi, it's torn down and re-armed so the next dial-in reconnects with no
restart. A read-only UDP heartbeat (port 29537, every 2 s) drives the health
card at the bottom of the CAN screen: device name, mode
(normal/listen/closed), RSSI, live rx/s, a fresh/stale badge, and a loud
**DROP** warning when the bus starts out-running the bridge. The full contract
is [`PROTOCOL.md`](PROTOCOL.md); scope is diagnostics + normal CAN
participation only, and **listen-only is enforced on the device itself**.

**Provisioning quick-start:** plug a factory-fresh CanTick into the Pi's USB,
open the CAN screen, tap **Provision**. Scottina pushes its *current* WiFi
credentials (primary slot), a generated fallback-AP credential pair
(fallback slot), the bus bitrate and the listen-only flag over the CDC serial
port, then verifies with `GET_STATUS`. PSKs are never logged. Unplug; on next
boot the CanTick joins the network and dials in.

### Micro KVM — off-grid command plane (scope)

When Prime is out of SSH/web reach, a paired Meshtastic node (your phone's
canned messages) can query it and drive a small set of diagnostic actions
over LoRa — one text frame in, one terse reply back. No video, no shell, no
interactive I/O. Scope, stated plainly: **this plane executes only an
allow-listed verb set** (`microkvm/registry.py`, mirrored in
[MICROKVM-PROTOCOL.md](To-DoLists/MICROKVM-PROTOCOL.md)), **has no shell** —
every subprocess is a fixed `list[str]` argv with domain-enumerated tokens,
re-checked by an independent reject pass (the `scan.py` pattern) — **and is
inert on-network**: the executor arms only when the configured home
gateway is unreachable (debounced), so holding the channel PSK at the bench
commands nothing. The command channel PSK is the crypto boundary; a
sender-node-ID allow-list narrows within it. Radio side: BLE to the Prime
radio T3, never Prime's WiFi (that stays free for the web app). Mesh
provisioning: [docs/LORAMESH.md](docs/LORAMESH.md).

**AP fallback:** if the Pi has *no uplink at all* when the CAN screen opens
(off-grid diagnostics), it raises a WPA2 AP `Scottina-CanTick` on `wlan0`
(gateway `192.168.42.1`, DHCP, `scottina.local`) so provisioned CanTicks can
still reach it via their fallback slot. The AP is fully reversible — closing
the screen (or the uplink returning) tears it down and hands `wlan0` back to
NetworkManager. `wlan1`/ALFA is never touched. Needs `hostapd` + `dnsmasq`
installed; without them the fallback simply reports itself unavailable.

## Live screens & responsiveness

Fast-changing screens are decoupled from the slow ones. The main loop honours a
per-screen `tick_interval` and only wakes each screen at its own cadence; live
screens tick at ~20 Hz while everything else stays at ~1 Hz. To make that
affordable on the SPI panel, `framebuffer.py` supports **dirty-rect blits** —
a screen reports the boxes that actually changed (via `self.report_dirty(...)`
in `tick()`) and only those row bands are written, 2–15× cheaper than a full
frame. Full-frame redraws still cover first frame, transitions, the keyboard
overlay, and dimming wake.

Guardrails keep a wedged data source from spinning the CPU: the **CAN** counter
and **Signal K** poller drop back to a slow tick automatically when no data is
flowing. A perf overlay (FPS / frame-time) is available behind
**Settings → System → "FPS meter"** (default off). See
[`KioskSpeedImprovementToDo.md`](KioskSpeedImprovementToDo.md) for the measured
before/after numbers.

## Touch calibration

If taps land in the wrong place after the 180° flip, open **Settings → Touch**
and flip these one at a time:

| Symptom | Toggle |
|---|---|
| taps mirror left/right | `Touch invert X` |
| taps mirror up/down | `Touch invert Y` |
| taps land on the wrong axis entirely | `Touch swap X/Y` |

Same values live in `config.json` if the panel is so far off you can't hit the
toggles — edit it over SSH and restart the service. Defaults target the
`rotate=90` orientation (`swap=on, invert_x=off, invert_y=off`); the old
`rotate=270` values were `swap=on, invert_x=on, invert_y=on`.

## Web-app launch terminal

Beyond the built-in screens, Scottina fronts **bigger packages that serve their
own browser UI**. Opening one of these screens *launches the app* and fronts it
with **one compact status card**: its border colour is the app state (green
only once the port actually answers — a real "✓ web UI confirmed", not just
"spawned"), its body is the **URL:port** to open the full interface from a
phone or laptop, and **tapping it stops the app (with a confirm) or launches
it again**. Below the card sits a native panel of controls and live feedback.
If the app was already serving (autostarted at boot), it's adopted instead of
duplicated.

Full user guide for all four: [docs/WEBAPPS.md](docs/WEBAPPS.md).

Shipped apps (tiles appear only when the app is installed):

- **Kismet** — launches the server, confirms `:2501`, a Sniff on/off toggle that
  adds the ALFA as an uplink-safe monitor source, and a live peer list
  colour-coded by device type.
- **Node-RED** — confirms the `:1880` editor and gives **6 assignable feedback
  fields + 6 trigger buttons** wired to your own flow. Import
  [`setup/nodered-kilodash-flow.json`](setup/nodered-kilodash-flow.json) and see
  [`setup/NODE-RED.md`](setup/NODE-RED.md) for the wire-up guide.
- **AIS** — AIS-catcher on the RTL-SDR for live vessel/message feedback (RX), plus
  an own-MMSI field and a hardware-gated Transmit-test control for bench-checking
  a robot's AIS receiver (TX needs a HackRF/Pluto + `ais-simulator`).
- **Signal K** — the boat's data hub as a helm glance. Confirms `:3000`, then
  pages through six-value vitals groups (Nav / Engine / Environment / Power,
  tap to cycle) with a live **heartbeat** line pinned to the bottom — freshness
  dot + feed count — so one glance tells you the NMEA2000→SK bridge is actually
  flowing. Never stopped on leave.

Install the backends with [`setup/install-phase4.sh`](setup/install-phase4.sh)
(idempotent; installs Node-RED, AIS-catcher, Signal K + their systemd units).

The framework lives in `kilodash/webapp.py` (a `WebApp` process/port supervisor
+ stdlib HTTP helpers) and `screens/webapp_base.py` (`WebAppScreen`). To add
another web app, subclass `WebAppScreen`, set `app_name`/`port`/`service` (or
`start_cmd`), and override the `draw_app`/`handle_app_tap`/`poll_app` hooks.

## Layout

```
run.py                  entrypoint
ScottinaSplash.png      boot curtain art
kilodash/
  app.py                main loop, taps, splash, dirty-rect render, dimming, keyboard overlay
  framebuffer.py        /dev/fb0 pack + blit (RGB565 / XRGB8888), full-frame + dirty-rect paths
  pictograms.py         Semiotic-Standard tile glyphs
  touch.py              ADS7846 evdev reader + axis mapping
  system.py             network / wifi / lan / health data (+ background Task)
  scan.py               LAN Scan safety core — intent→arg-array builder, reject-list, nmap parse/stream
  webapp.py             launch/confirm/supervise third-party web apps (Phase 4)
  devices.py            USB / bus hotplug detection (drives tile visibility)
  widgets.py            Button, on-screen Keyboard, helpers
  theme.py              palettes + font cache
  config.py             settings schema + JSON persistence
  screens/              one file per screen
microkvm/               off-grid command plane: verb registry, executor, arm gate, BLE link
legacy/                 the fbdash.py / kilo_dash.py prototypes this grew from
setup/                  web-app installer, systemd units, Node-RED flow + guide
tools/                  bench instruments: CanTick provisioning, Meshtastic node provisioning
```

Adding a screen: subclass `screens.base.Screen`, implement `draw_content` and
`handle_tap`, set a `glyph` (see `pictograms.py`), and add it to
`screens/__init__.py::SCREENS`.

### LAN Scan safety model (why the rejected flags stay rejected)

`scan.py` assembles every scan command from a discrete intent (mode + validated
target + validated ports) into an **argument array** — never a shell string, so
there is nothing to inject into. The four modes are the *entire* attack surface;
the UI has no raw-flag input. `scan._enforce_rejects` is defense in depth: even
if a value arrived from elsewhere, the assembled command is refused if it
contains any of these. **Do not "helpfully" re-add them** — they exist to keep
this a diagnostics tool:

| Flag(s) | Why blocked |
|---|---|
| `--script`, `-sC` | NSE — nmap's offensive scripting subsystem (vuln/exploit probes). Top priority to keep unreachable. |
| `-sS`, `-sF`, `-sX`, `-sN` | Stealth / half-open / evasion scans meant to slip past monitoring. |
| `-A` | Aggressive — bundles NSE, OS detection and traceroute. |
| `-D`, `-S`, `--spoof-mac` | Decoys and identity spoofing. |
| `-f`, `--mtu`, `--data-length` | Packet fragmentation / padding for firewall evasion. |
| `-T4`, `-T5` | Evasion-tuned aggressive timing. |

Tests in `tests/` prove each mode's exact arg array and that every flag above is
refused (`python -m unittest discover -s tests`). Full task list:
[LAN-Scan-Refactor-TODO.md](LAN-Scan-Refactor-TODO.md).

### CAN scope (the TX exception, stated explicitly)

The CAN and NMEA2K screens are **diagnostics only. The CAN TX exception now
covers two things, both stated explicitly: (1) heartbeat/reply behavior
required by bus participation — the link layer's job (CanTick firmware, see
[PROTOCOL.md](To-DoLists/PROTOCOL.md)); and (2) the GNSS source node
(`n2k/node.py`) — ISO address claim, claim defense, ISO request responses,
and the five GNSS PGNs (126992, 129025, 129026, 129029, 126993), started
and stopped only by an explicit user action ("Source GNSS → bus" on the
NMEA2K tile). No injection, replay, fuzzing, or arbitrary-frame TX is
expressible anywhere in the UI or command builders.** Enforced in code, not
by convention, same discipline as the LAN Scan flags:
`tests/test_txscan.py` AST-scans the **whole tree** against a positive
allow-list of TX-permitted modules (exactly one: `n2k/node.py`) — any
send-shaped call on a socket in any other module fails the build — while
`tests/test_busmon.py` / `tests/test_n2k.py` remain the independent reject
pass keeping both screens and their bus models RX-only.

## Roadmap

Phase 2/3 (Kali pentest tooling, RTL-SDR, ALFA Wi-Fi adapter) and Phase 4 (the
web-app launch terminal above) are tracked in [ROADMAP.md](ROADMAP.md) and
[PHASE2.md](PHASE2.md).

## License

[MIT](LICENSE). Scottina is created by Scott McLeslie for the benefit of all
living beings — feel free to share and contribute.
