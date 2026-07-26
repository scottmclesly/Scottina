# Synthetic table fixtures

Plumbing fixtures for the Light index export path (TABLES.md §5b/§5c) and,
later, Light's picker. **Not real decode tables.** Regenerate, never
hand-edit:

```
python tests/fixtures/tables/generate.py
```

Schema is derived from `tables/validate.py` (the sole source of truth); the
generator re-validates every store it writes, so a validator drift fails
generation rather than emitting a stale fixture.

## §5c detail-file collision — SETTLED (namespacing)

Detail files are **namespaced by store**: `<src>.pgn-<num>.json`. Two verified
stores that both define a PGN now export side by side with no overwrite. Store
names are `[a-z0-9_-]` (no dots), so the namespacing is unambiguous and a
decode-only reader still tells a table (`<name>.json`, one dot) from a detail
(two dots) by name alone.

`synthetic-overlap` redefines PGN **258050** (`0x3F002`), which
`synthetic-basic` also defines. Its job is now to **prove namespacing works**
— `test_fixtures_synthetic.py::test_overlapping_stores_export_side_by_side_no_overwrite`
exports both and asserts `synthetic-basic.pgn-258050.json` (3 signals) and
`synthetic-overlap.pgn-258050.json` (2 signals) both survive intact.

Rejected alternative, for the record: failing the export on cross-store PGN
overlap. Canboat's `pgns.json` overlaps with every vendor table ever
installed — overlap is the normal case, not an error.

**Detection on top of prevention (§5b):** each index entry carries the sha256
of its detail file. A consumer hashes the file it loads and refuses a
mismatched pair (`validate.detail_sha_ok`). Prevention (namespacing) plus
detection (per-detail sha) — the standing two-gate rule.

### Consumer note — multiple indexes in one flat dir

A consumer loading a flat `/tables/` may now see **several** `<src>.index.json`
files (one per exported store). Whether to merge them, present them as
separate table sets, or prefer one is a **consumer-side decision**, not a
contract one — the contract only guarantees each index and its namespaced
detail files are internally consistent and sha-bound.

## Provenance warning — SETTLED (`warn` in the index header)

The index header carries an optional **`warn`** string, populated at export
from an explicit `_synthetic` flag in the table file if present, else the
manifest's `source_doc`. Consumers render it **persistently**, so a synthetic
fixture is structurally incapable of appearing on screen as a real reading.

Provenance is deliberately **not** a §2 table-schema field: tables travel with
manifests, and the index header is where the consumer actually looks. The
`_synthetic` marker in these files is the export-time source for `warn`; every
store here also ingests with `source_doc = SOURCE_DOC`, so either path yields
the same banner.

## Schema notes (from `tables/validate.py`)

Two rules that guard against a confident-wrong reading are now **enforced** by
the validator (they were previously gaps this fixture set surfaced):

- **`Resolution: 0` is a validation error.** It was silently coerced to `1`
  (`float(x or 1)`); a real-table typo would then decode as raw counts and
  look plausible.
- **Single-frame (non-fast) PGNs must fit in 64 bits.** A field whose
  `BitOffset + BitLength` exceeds 64 (one 8-byte CAN frame) is skipped with a
  warning. Fast-packet PGNs assemble a larger payload and are exempt. The
  check lives in the shared validator, so it protects Prime's live decode and
  the Light export alike.

The fixtures respect both: no 0 resolutions, and every single-frame field
fits inside 64 bits.

## Non-usability safeguards

These must never decode a real bus by accident:

- every PGN is in **data page 3** (`pgn >> 16 == 3`); NMEA2000 uses DP 0/1
  only, DP 2/3 are ISO/reserved — no real device emits these ids;
- units are `SYN-`prefixed, lookup labels are `SYN-*`;
- `source_doc` = `SYNTHETIC FIXTURE — not a real PGN table`, the file carries
  the `_synthetic` marker, and that marker becomes the index `warn` banner.

## Contents

`synthetic-basic` — 11 PGN entries:

| PGN (hex) | name | signals | interval_ms | note |
|---|---|---|---|---|
| 0x3F000 | SYN Alpha | 1 | 100 | fast rate, units |
| 0x3F001 | SYN Bravo | 2 | 100 | |
| 0x3F002 | SYN Charlie | 3 | 1000 | **also in synthetic-overlap** |
| 0x3F003 | SYN Delta | 4 | 1000 | |
| 0x3F004 | SYN Echo | 5 | 5000 | slow |
| 0x3F005 | SYN Foxtrot | 6 | *absent* | unknown-interval path |
| 0x3F006 | SYN Golf | 2 | *out of range* | → unknown + warning |
| 0x3F007 | SYN Hotel | 3 | 250 | lookup enum |
| 0x3F008 | SYN India | 1 | 100 | lookup enum |
| 0x3FB00 | SYN Juliet | 3 | 1000 | **fast-packet → excluded** |
| 0x31000 | SYN Kilo | 1 | 100 | **PDU1 → excluded** |

9 eligible PDU2 single-frame PGNs vs Light's 6-PGN picker limit → real
selection pressure. `synthetic-overlap` — 1 PGN (0x3F002), a different
2-signal definition, exported as `synthetic-overlap.pgn-258050.json`
alongside basic's, proving the namespacing.

## Other stores in this directory

- **`synthetic-marine`** — the decode-RICHNESS store (bench test kit): 9
  eligible PGNs with a full field-shape set (manufacturer header, bit flags,
  lookup enums, signed + not-available, a conditional field), plus a
  fast-packet and a PDU1 the index excludes. It is the ground-truth emitter's
  table; the §2 schema gaps it surfaces (conditional field, per-bit flags, NA
  sentinel collisions) are reported in
  [`tools/bench/README.md`](../../../tools/bench/README.md).
- **`synthetic-invalid`** — a store the validator must **reject**: one field
  with `Resolution: 0`, one field extending past bit 64 of a single-frame
  PGN. Kept apart so `synthetic-marine` validates clean; proves the two
  validator checks fire (`test_fixtures_synthetic.py`).
