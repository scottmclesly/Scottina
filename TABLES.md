# TABLES.md — the decode-table store contract

Like [`PROTOCOL.md`](To-DoLists/PROTOCOL.md) between the CanTick firmware and kilodash,
this file is the **only** coupling between the pieces that touch decode
tables:

| Party | Role |
|---|---|
| **Converter service** (`kilodash/tableconv.py`, Tables tile → web app) | **the writer** — ingest, validate, install, remove |
| **NMEA2K screen** (`kilodash/screens/n2k.py`) | reader — enabled PGN tables drive live decode |
| **DBC screen** (future) | reader — `tables/dbc/` |
| **Scottina Light** | reader — flat JSON on SD/USB for decode-only, or the Light index (§5b) + per-PGN detail (§5c) to browse-then-decode |
| **Tables tile** (`kilodash/screens/tables.py`) | mirror — reads the store; its only write is the atomic manifest `enabled` flip |

Spec first, consumers second: anything not written here is not part of the
contract, and no consumer may rely on it.

## 1. Directory layout

Repo `tables/` — runtime `/opt/kilodash/tables/` (override for tests with
`KILODASH_TABLES`):

```
tables/
├── pgn/                    # NMEA2000 PGN tables (Canboat-style JSON)
│   ├── <name>.json         # the table (§2)
│   └── <name>.meta.json    # its manifest sidecar (§3)
├── dbc/                    # raw .dbc signal databases (future DBC screen)
├── uploads/                # converter scratch (uploaded PDFs); never read
│                           # by any consumer, purged by the converter
└── *                       # loose files in the root are the INBOX (§6)
```

`<name>` is `[a-z0-9_-]{1,64}` — derived from the source document, unique
within `pgn/`.

## 2. The Canboat-JSON subset we consume

A table file is a JSON object with a `PGNs` array. Consumers use **only** the
keys below; anything else (extra Canboat keys, vendor annotations) is
**ignored, never fatal**. Canonical Canboat spellings are accepted where
noted.

```jsonc
{
  "PGNs": [
    {
      "PGN": 127508,                  // required, int
      "Name": "Battery Status",       // or Canboat "Description"
      "FastPacket": false,            // or Canboat "Type": "Fast"/"Single"
      "TransmissionInterval": 1500,   // optional, ms — the PGN's nominal
                                      //   send interval; or `interval_ms`.
                                      //   Absent = unknown (§5b rate budget)
      "Fields": [
        {
          "Name": "Voltage",          // required, string
          "BitOffset": 8,             // required, int ≥0 (LSB-first packing)
          "BitLength": 16,            // required, int 1..64
          "Resolution": 0.01,         // default 1
          "Offset": 0,                // engineering offset, default 0
          "Signed": false,            // default false
          "Units": "V",               // default ""
          "Lookup": {"0": "Off"}      // or Canboat "EnumValues":
                                      //   [{"name": "Off", "value": "0"}]
        }
      ]
    }
  ]
}
```

Decode semantics (what the NMEA2K screen does with this):

- Fields are extracted from the **assembled** payload (fast-packet
  reassembly happens *before* table lookup), LSB-first:
  `raw = (payload_as_little_endian_int >> BitOffset) & ((1<<BitLength)-1)`.
- `Signed` fields are two's-complement over `BitLength`.
- The all-ones raw value of an unsigned field (and max-positive of a signed
  one, per N2K convention) means **not available** → rendered `—`, never fed
  to alerts. Applies to fields wider than 1 bit — a 1-bit flag's `1` is a
  real value.
- Display value = `raw * Resolution + Offset`; `Lookup` (keyed by the *raw*
  value as a decimal string) wins over numeric rendering when it matches.

`interval_ms` (accepted also as Canboat's `TransmissionInterval`, both in
milliseconds) is an **optional** per-PGN key: the nominal transmission
interval, the input to Light's rate budget (§5b). **Absent means unknown**,
never fatal — a selection containing unknowns is legal, and the consumer
presents its rate total as a lower bound. A non-integer or out-of-range
value (≤0 or above the sane upper bound) is dropped to unknown with a
warning, same two-tier discipline as the rest of §2.

Validation is **two-tier**: a malformed *file* (not JSON, no usable `PGNs`)
is rejected outright; a malformed *entry* (bad field, missing `PGN`) is
skipped with a warning while the rest of the file loads. A skipped entry
must never take the file — or the screen — down with it.

## 3. The manifest sidecar — `<name>.meta.json`

Written atomically by the converter next to every installed table:

```jsonc
{
  "name": "victron_battery",          // == file stem
  "source_doc": "VE.Can-registers.pdf", // what it was converted from
  "converted": "2026-07-12T14:03:00Z", // ISO-8601 UTC conversion time
  "converter_version": "1.0",          // kilodash.tableconv.VERSION
  "enabled": true,                     // the ONLY key any non-converter
                                       // party may flip (tile, atomically)
  "pgn_count": 12,                     // valid entries at ingest — so
                                       // manifest-only readers (the tile)
                                       // never parse table files
  "sha256": "…"                        // hex digest of <name>.json as written
}
```

- A table with **no manifest** or a **stale `sha256`** is shown in
  inventories as *unverified* and is **not** loaded for decode until the
  converter re-ingests it.
- `enabled: false` removes the table from decode without deleting anything.

## 4. Who reads, who writes

**Consumers only read; the converter only writes.**

- The NMEA2K screen (and every future reader) never mutates the store — not
  even to "fix" a file. The converter never decodes live traffic.
- The Tables tile's enable/disable toggle is the single sanctioned
  exception: it rewrites only the manifest, tmp-file + `os.replace()`
  atomic, never the table itself.
- **No third writer, ever.** The Files screen's USB import drops files in
  the root inbox (§6); it does not write `pgn/`.
- All converter writes are tmp-file + atomic rename in the same directory,
  so a killed service (idle timeout, power pull) never leaves a
  half-written table or manifest.

## 5. SD-export shape (Scottina Light)

One conversion effort feeds both devices. The export is **flat** (no
subdirectories) and serves **two consumption modes** from the same
directory:

- **decode-only** — the consumer already knows which PGNs it wants and
  decodes what it is handed. It reads the flat table files, the same JSON,
  applying §2 verbatim (ignore-unknown-keys, skip-bad-entries).
- **browse-then-decode** — the consumer presents a *picker* first (Light's
  CAN gauge on a 192 KB device), needing an index and a rate budget the
  flat table alone doesn't carry. It reads the Light index (§5b), then the
  per-PGN detail file (§5c) for each PGN the user selects.

```
<media>/scottina/tables/
├── victron_battery.json            # the table (§2) — decode-only
├── victron_battery.meta.json       # its manifest (§3)
├── victron_battery.index.json      # the Light index (§5b) — browse
├── pgn-127508.json                 # per-PGN detail (§5c) — one per offered PGN
└── …
```

Producers: the Files screen's *Tables → USB* export and the web app's
per-table *download* / *index*. Both call **one shared writer**
(`tables/lightindex.py`) so the two paths emit byte-identical artifacts.

**Decode-only readers skip** `*.meta.json` and any `*.json` that has no
top-level `PGNs` array (which is what the index and detail files are, so
they are never mistaken for tables — TABLES.md gotcha). This is the same
content-based skip the §2 two-tier rule already gives.

### 5b. The Light index — `<name>.index.json`

A new emitted artifact *alongside* the flat table, **never a replacement**.
Keys are abbreviated deliberately (parsed on a 192 KB device; budget
~120 B/PGN, so 100 PGNs ≈ 12 KB) and it carries **names only** — no
bit-field definitions:

```json
{"v": 1,
 "src": "<store name>",
 "sha256": "<of the source table file>",
 "generated": "<ISO8601>",
 "pgns": [
   {"pgn": 127508, "n": "Battery Status", "ms": 1500,
    "sig": [{"i": 0, "n": "Voltage", "u": "V"},
            {"i": 1, "n": "Current", "u": "A"}]}
 ]}
```

- `src` / `sha256` bind the index to the exact bytes of its source table so
  a consumer can **refuse a mismatched pair**. `generated` mirrors the
  source manifest's `converted` time, so re-exporting an unchanged store is
  byte-stable. The index is **derived, never hand-maintained** — an index
  that drifts from its tables decodes plausible garbage.
- `ms` is omitted when the interval is unknown; `sig[].i` indexes the
  detail file's `fields` array (§5c); `sig[].u` is omitted when the signal
  has no units.
- **Fast-packet PGNs are excluded at export time.** Light cannot reassemble
  them safely — one dropped frame yields a plausible wrong value — so it
  must never be offered them. Enforced by the writer; Light keeps an
  independent reject on read (two gates, §6).
- **PDU1 PGNs (PF < 240) are excluded at export time.** Their ids carry a
  destination address that would have to be masked around in the consumer's
  hardware filters — out of scope for v1.

### 5c. Per-PGN detail — `pgn-<num>.json`

The full §2 field definitions for one PGN, emitted for each PGN the index
offers. The consumer loads only the handful it selected, one at a time:

```json
{"v": 1, "pgn": 127508, "name": "Battery Status", "fast": false,
 "interval_ms": 1500,
 "fields": [{"name": "Voltage", "bit_offset": 8, "bit_length": 16,
             "resolution": 0.01, "offset": 0, "signed": false,
             "units": "V", "lookup": null}]}
```

Field keys are the normalized §2 decode form (LSB-first `bit_offset` /
`bit_length`, `resolution`, `offset`, `signed`, `units`, `lookup`), so a
detail file decodes an assembled payload exactly as the NMEA2K screen does.
`fast` is carried so the consumer's read-side check can reject an
ineligible PGN independently. `interval_ms` is omitted when unknown. The
index and its detail files agree on the exact PGN set; a desynced pair is
rejected by the validator (`tables/validate.py::check_pair`).

## 6. Validation & the inbox

`tables/validate.py` is the **shared schema validator** — the same module
runs in the converter (on ingest) and in the NMEA2K screen (on every load).
Defense in depth: a hand-copied file that skips the converter still gets
validated before it can drive decode. It also validates the Light index
(`validate_index`, `check_pair`): used on **write** by the shared writer and
available for the consumer's own read-side check, so an index carrying a
fast-packet or PDU1 entry — or a hand-desynced index/detail pair — is
rejected even though the writer should never produce one.

Loose files in the `tables/` root (USB imports from the Files screen,
`scp`-ed files) are the **inbox**: inert until the converter's *Installed*
tab ingests them (validate → move into `pgn/` → write manifest). Consumers
never read the inbox.

## 7. Consumers of this contract

- Prime's NMEA2K screen — [`kilodash/screens/n2k.py`](kilodash/screens/n2k.py)
- kilodash Tables tile — [`kilodash/screens/tables.py`](kilodash/screens/tables.py)
- Converter service — [`kilodash/tableconv.py`](kilodash/tableconv.py)
- Shared export writer — [`tables/lightindex.py`](tables/lightindex.py)
- Scottina Light — SD reader (flat tables and/or the §5b/§5c browse
  artifacts; see that repo's spec, it links back here)
- Future DBC screen — `tables/dbc/`, same manifest scheme, format TBD there

Changing anything in §1–§6 means updating **all** of the above in one
change, or not making the change.
