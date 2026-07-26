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
          "Resolution": 0.01,         // default 1; MUST NOT be 0 (a 0 scale
                                      //   decodes as raw counts — rejected)
          "Offset": 0,                // engineering offset, default 0
          "Signed": false,            // default false
          "Units": "V",               // default ""
          "Lookup": {"0": "Off"},     // or Canboat "EnumValues":
                                      //   [{"name": "Off", "value": "0"}]
          "Undecodable": "reason"     // optional; §2 can't interpret this
                                      //   field → decoders render it n/a
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

Two field-level rules guard against a confident-wrong reading (both
two-tier: the offending field is skipped with a warning, the rest load):

- **`Resolution` must not be 0.** A `0` scale was once silently coerced to
  `1`; a real-table typo would then decode as raw counts and look entirely
  plausible. It is now a validation error.
- **A single-frame (non-`FastPacket`) PGN's fields must fit in 64 bits** —
  one 8-byte CAN frame. A field whose `BitOffset + BitLength` exceeds 64 can
  never carry data (a mislabelled fast-packet or a broken table) and is
  skipped. Fast-packet PGNs assemble a larger payload and are exempt. Checked
  in `tables/validate.py` (the shared gate) so it protects every consumer —
  Prime's live decode and the Light export alike — not just one of them.

`Undecodable` (optional, string) is the **fail-safe** for a field the current
schema cannot correctly interpret — the common case is a *conditional field*
whose resolution/unit depends on another field's value (see §8). A decoder
**must not compute a number** for such a field: it renders it not-available
(the NMEA2K screen shows `n/d`, distinct from the `—` NA sentinel) and carries
the reason for display. **Wrong-and-confident is worse than absent.** The
field still occupies its bits (its raw value is available) and its
`Undecodable` flag travels into the §5c detail file so Light fail-safes it
too; the §5b index omits its (meaningless) unit but still lists the signal.

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
├── victron_battery.json                 # the table (§2) — decode-only
├── victron_battery.meta.json            # its manifest (§3)
├── victron_battery.index.json           # the Light index (§5b) — browse
├── victron_battery.pgn-127508.json      # per-PGN detail (§5c), namespaced
└── …
```

Producers: the Files screen's *Tables → USB* export and the web app's
per-table *download* / *index*. Both call **one shared writer**
(`tables/lightindex.py`) so the two paths emit byte-identical artifacts.

**Detail and index files are namespaced by store** (`<src>.index.json`,
`<src>.pgn-<num>.json`). Store names are `[a-z0-9_-]` — **no dots** — so two
stores that both define a PGN never collide in one flat dir, and a
decode-only reader tells a table (`<name>.json`, one dot) from an index or
detail (two dots) by name alone.

**Decode-only readers skip** `*.meta.json`, `*.index.json`,
`*.pgn-*.json`, and — belt and braces — any `*.json` with no top-level
`PGNs` array (which the index and detail files are). The same content-based
skip the §2 two-tier rule already gives.

### 5b. The Light index — `<name>.index.json`

A new emitted artifact *alongside* the flat table, **never a replacement**.
Keys are abbreviated deliberately (parsed on a 192 KB device; budget
~190 B/PGN with the per-detail `sha`, so 100 PGNs ≈ 19 KB) and it carries
**names only** — no bit-field definitions:

```json
{"v": 1,
 "src": "<store name>",
 "sha256": "<of the source table file>",
 "generated": "<ISO8601>",
 "warn": "<provenance banner, e.g. source_doc — rendered persistently>",
 "pgns": [
   {"pgn": 127508, "n": "Battery Status", "ms": 1500,
    "sha": "<sha256 of this PGN's detail file>",
    "sig": [{"i": 0, "n": "Voltage", "u": "V"},
            {"i": 1, "n": "Current", "u": "A"}]}
 ]}
```

- `src` / `sha256` bind the index to the exact bytes of its source table so
  a consumer can **refuse a mismatched pair**. `generated` mirrors the
  source manifest's `converted` time, so re-exporting an unchanged store is
  byte-stable. The index is **derived, never hand-maintained** — an index
  that drifts from its tables decodes plausible garbage.
- `pgns[].sha` is the **sha256 of that PGN's detail file** (§5c). A consumer
  hashes the detail it loads and refuses to decode on a mismatch — detection
  on top of the namespacing that makes a cross-store collision structurally
  impossible in the first place (two gates, §6). `validate.detail_sha_ok()`
  is the read-side check.
- `warn` (optional) is a **provenance banner** populated at export from the
  table's explicit `_synthetic` flag if present, else the manifest's
  `source_doc`. Consumers render it **persistently**, so a synthetic fixture
  is structurally incapable of appearing on screen as a real reading.
  Provenance lives here and in the §3 manifest — **never** in the §2 table
  schema (tables travel with manifests; the index header is where the
  consumer looks).
- `ms` is omitted when the interval is unknown; `sha` when there is no
  detail; `sig[].i` indexes the detail file's `fields` array (§5c);
  `sig[].u` is omitted when the signal has no units.
- **Fast-packet PGNs are excluded at export time.** Light cannot reassemble
  them safely — one dropped frame yields a plausible wrong value — so it
  must never be offered them. Enforced by the writer; Light keeps an
  independent reject on read (two gates, §6).
- **PDU1 PGNs (PF < 240) are excluded at export time.** Their ids carry a
  destination address that would have to be masked around in the consumer's
  hardware filters — out of scope for v1.

### 5c. Per-PGN detail — `<src>.pgn-<num>.json`

The full §2 field definitions for one PGN, emitted for each PGN the index
offers, **namespaced by store** so two stores that define the same PGN
export side by side without overwriting. The consumer loads only the handful
it selected, one at a time, and checks each against the index's `pgns[].sha`:

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

## 8. Conditional fields — DECISION: DEFERRED to v2 (not implemented now)

> **Decision (settled — do not re-litigate).** The agreed direction is to
> match **Canboat `LOOKUP_FIELDTYPE`** (a selector field whose value defines
> the following field's type, carrying resolution + unit together). It is
> **not implemented now.** The `Undecodable` field marker (§2) is a sufficient
> fail-safe in the meantime — a conditional field is rendered not-available,
> never a confident wrong value. **§8 lands with the real Canboat `pgns.json`
> import as a v2 contract change**, so the schema gains conditional-field
> support and Canboat interop in one step rather than piecemeal. Until then
> the analysis below stands as the rationale; the choice is made.

**Problem.** Proprietary marine PGNs commonly carry a field whose meaning —
resolution *and* unit, sometimes the field type itself — depends on another
field's value or a single bit (a "mode"/"select"). §2 gives each field exactly
one `Resolution` and one `Units`, so it cannot express this. Today such a field
is marked `Undecodable` (§2) and rendered not-available: correct and safe, but
it means the value is simply unavailable. This section proposes how to actually
decode it. It is a **proposal — not implemented.** `Undecodable` is the
shipped fail-safe until one of these lands.

**Does §2 already let `Units` vary independently of `Resolution`? No.** Each
field has one static `Units` and one static `Resolution`; neither varies at
runtime, and there is no mechanism to change one without the other. A `Lookup`
substitutes a label for the numeric render but does not vary units. So this is
**one gap, not two**: a conditional field switches its *whole* numeric
interpretation (resolution and unit together — e.g. `0.1 %` ↔ `0.25 rpm`), and
the fix should switch them as a unit, not add two independent knobs.

**What Canboat does (checked `docs/canboat.xsd` before inventing a shape).**
Canboat already has conventions for exactly this, and we should match rather
than diverge:

- **`LookupFieldTypeEnumeration`** (`FieldType = LOOKUP_FIELDTYPE`): a
  selector field whose value "defines the field type of a following variable
  field". This is the closest match to the mode→(resolution,unit) case — the
  selector picks the *type* (hence resolution + unit) of the next field.
- **`LookupIndirectEnumeration`** / `…FieldOrder` (`EnumTriplet` with
  `Value1`/`Value2`): a field's lookup meaning depends on another field's
  value, referenced by field order.
- **`Condition`**: a field may or may not be present depending on a condition.
- **`Match`** + **`Fallback`**: proprietary PGN *variants* share a PGN number;
  the decoder picks the entry whose `Match` fields all match (manufacturer
  code, etc.). This is the "variants keyed by a selector" approach, at PGN
  granularity.
- (Related, for the separate per-bit-flag gap: **`LookupBitEnumeration`** /
  `FieldType = BITLOOKUP`, `BitPair`/`Bit` — named bits. If we tackle flags,
  match this too.)

**Three options, with what each costs Light:**

| Option | Shape | Index (§5b) size | Parser complexity | Picker w/o evaluating conditions |
|---|---|---|---|---|
| **1. Per-field `condition`** — `{selector_field, bit, value}` on the field with a small table of per-condition `(resolution, units)` | one field entry + a compact condition block | small: +~1 selector ref +N×(res,unit) per conditional field | must read the selector field first, then pick the variant — a two-pass decode within one PGN | yes — the picker lists the field by name; only the live unit/value needs the selector |
| **2. Field variants keyed by a selector** (à la Canboat `Match`, but per field) — N full field definitions, one per selector value | N field entries (or a variant sub-array) | larger: ~N× the field's detail | pick the matching variant by selector; each variant is an ordinary field | yes for names; the picker would show N rows or one merged row (a UI choice) |
| **3. Match Canboat `LOOKUP_FIELDTYPE`** — a selector field whose value maps to a field *type* that carries (resolution, unit) | selector lookup + a type table | medium; reuses Canboat's type catalogue if we adopt it | Canboat-level (moderate) but a known, documented model; interop with Canboat tables for free | yes — name lists without evaluation; unit is "varies until decoded" |

**Recommendation.** Option 3 (match Canboat `LOOKUP_FIELDTYPE`) is preferred:
it is the upstream convention, so vendor tables that already use it import
without a translation layer, and it keeps §2 a *subset* of Canboat rather than
a divergent dialect. Option 1 is the cheapest to ship if we only ever need the
narrow bit→(res,unit) case and want the smallest index. Option 2 is simplest to
parse but the heaviest on index size — poor for Light's 192 KB budget.

**In all three, the picker can still show a signal list without evaluating any
condition** — the field *name* is static; only the unit/value is deferred to
decode time. So Light's browse experience is unaffected; only live rendering of
a conditional field needs the selector. Until a decision lands, `Undecodable`
keeps those fields safe (not-available, never wrong).
