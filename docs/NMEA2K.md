# NMEA2K & Tables — user guide

Two tiles, one contract. The **NMEA2K** screen decodes live bus traffic
into named fields with units; the **Tables** tile manages the decode tables
it reads and the on-device **converter web app** that produces them. The
contract between all the moving parts is [`TABLES.md`](../TABLES.md) at the
repo root — converter writes, everything else reads.

The split in one line: **CAN** ([CANBUS.md](CANBUS.md)) is for traffic you
*don't* understand yet; **NMEA2K** is for traffic you do — because a table
says so.

Both CAN screens are diagnostics-only and RX-only (see the scope section in
the [README](../README.md#can-scope-the-one-tx-exception-stated-explicitly)).

---

## NMEA2K screen

Appears while a CAN interface is present (same gate as the CAN tile). It
needs at least one **enabled** PGN table; without one it points you at the
Tables tile.

| Element | What it does |
|---|---|
| **Status card** | Loaded PGN count + interface, and the ⚠ alert badge. |
| **PGNs / Unknown chips** | Toggle the decoded list vs. the unknown-PGN list. |
| **Decoded rows** | One per (PGN, source): name, rate, source address, first decoded values. Alerting rows flash. Tap → field breakdown. |
| **Field breakdown** | Every field with value + units (`—` = not available on the wire). **Tap a field → range alert** (`min,max`, either side blank = open; empty = off). **Alert on sight** arms an appearance alert for the whole PGN — for alarm/fault PGNs that should never show up. |
| **Unknown rows** | PGNs heard but not in any table — counted, never silently dropped (undecoded traffic is signal). **Tap → jumps to the CAN screen** pre-filtered to a sample arbitration id, ready for raw forensics. |
| **Save log** | Exports the bounded decoded log as JSON lines to `/opt/kilodash/captures/n2k_*.jsonl` (filtered to the open PGN when a breakdown is showing). |

Alerts are the same non-modal grammar as the CAN screen: status badge + row
flash, never a dialog over a live view. Range alerts fire on the
*transition* out of range, so a stuck-bad value alerts once, not at 10 Hz.

Fast-packet PGNs are reassembled before decode (sequence/frame counters,
per-source state); an `fp-drop` counter appears if sequences are arriving
broken. **Bench note:** validate reassembly against captured multi-frame
PGNs from the real bus before trusting a new table — synthetic frames are
not enough.

## Tables tile

Always visible — tables are software, no dongle needed. It is deliberately
thin: a **remote control + mirror**, no conversion or parsing on the panel.

- **Service pane** — converter status (`stopped` / `starting` / `running ·
  idle-N:MM` countdown), Start/Stop, and while running the URL **plus a QR
  code** for the advertised address (eth0 preferred, else wlan0).
- **Inventory pane** — installed tables straight from the store manifests:
  name, PGN count, state. **Tap a row** to enable/disable (an atomic flip
  of the manifest's `enabled` flag — the file itself is never touched);
  tap **✕** then confirm to remove. `unverified` means the table file
  doesn't match its manifest hash — re-ingest it via the converter; it
  will not decode until then.

Opening the tile starts the service. **Leaving does not stop it** — the
converter shuts itself down after 15 minutes without HTTP activity (an
in-flight conversion counts as activity), so you can navigate Scottina
freely while working from a laptop.

## Converter web app

`http://<pi>:8735/` (scan the QR). Same user model as Node-RED / Signal K:
runs on the Pi, reviewed from a big screen. Install once with
[`setup/install-tables.sh`](../setup/install-tables.sh).

- **PGN tab** — upload a vendor PDF. Text is extracted in a crash-isolated
  subprocess and PGN candidates become a JSON skeleton; then the
  **side-by-side review**: source text left, editable Canboat-subset JSON
  right. *Extraction is assistive, approval is human* — a silently wrong
  bit-field offset is worse than no table, so nothing is ever auto- or
  batch-approved. Approving runs the validator; only then does the table +
  manifest land in the store (atomically).
- **Installed tab** — the same inventory as the tile (they read the same
  store, so they can never disagree) plus **download** (the flat SD-export
  table for decode-only consumers) and **index** (Scottina Light's
  browse-then-decode index, TABLES.md §5b/§5c) and **inbox ingest**: files
  dropped
  into `tables/` by hand or by the [Files](FILES.md) screen's USB import
  are inert until validated and ingested here.
- **DBC tab** — stub; the same ingest→validate→store flow into
  `tables/dbc/` comes with the future DBC screen.

Uploads are size-capped and magic-checked; every store write is tmp-file +
atomic rename, so the idle timeout (or a power pull) can never leave a
half-written table.

## Getting tables on and off the box

| Path | Direction | Shape |
|---|---|---|
| Converter web app | PDF → store | validated + manifested |
| Web *download* / Files **Tables → USB** | store → laptop / SD | flat dir: `<name>.json` + `<name>.meta.json`, plus Light's `<name>.index.json` + `pgn-<num>.json` browse artifacts (TABLES.md §5/§5b/§5c — feeds Scottina Light) |
| Files **Tables ← USB** / `scp` | stick → `tables/` inbox | inert until ingested on the Installed tab |

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No PGN tables — open the Tables tile" | Nothing enabled: convert/ingest a table, or enable one on the tile. |
| Table shows `unverified` | File edited behind the converter's back (hash mismatch). Re-ingest via the converter; it won't decode until then. |
| Every value of a PGN reads `—` | Wrong field offsets in the table, or the payload really is all not-available. Check the breakdown against the vendor doc. |
| `fp-drop` climbing | Fast-packet sequences arriving broken — bus overrun (see the CanTick DROP warning) or a mis-flagged single-frame PGN in the table. |
| `11-bit!` in the bottom bar | The bus is carrying standard-frame traffic — that's not NMEA2000; sniff it on the CAN screen. |
| Converter URL unreachable from the Mac | You're on the other interface — the URL advertises eth0 first. The dual-NIC routing quirk is a known open item; use the advertised address. |
