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

## ⚠ Finding — §5c detail-file collision (settle before Light parses)

`pgn-<num>.json` is **not namespaced by store**. Two *verified* stores that
each define the same PGN, exported to one flat directory, collide on that
filename. `synthetic-overlap` exists solely to demonstrate this: it redefines
PGN **258050** (`0x3F002`), which `synthetic-basic` also defines.

**Current exporter behaviour (characterised, pinned by
`tests/test_fixtures_synthetic.py`):**

- `lightindex.write_table_artifacts` writes each detail with `_write_atomic`
  (tmp + `os.replace`), which **silently overwrites**. No error, no warning.
- `screens/files.py::_export_light_index` iterates stores **sorted by name**,
  so `synthetic-basic` writes `pgn-258050.json` first and `synthetic-overlap`
  overwrites it. **Last store by sorted name wins.**
- Net result: on disk, `pgn-258050.json` describes *overlap's* 2-field
  variant, while `synthetic-basic.index.json` still lists 258050 as its own
  3-signal `SYN Charlie` and points at that same file. **The earlier store's
  index is left silently referencing another store's detail.** No diagnostic
  anywhere.

**Why it matters:** an index entry (`sig[].i` indices, signal names) and the
detail file it points at can disagree, so Light would render one store's
picker row against another store's field definitions — a plausible wrong
decode, exactly the failure §5b/§5c exist to prevent. This is a **contract
question, not a bug to patch here**: §5c has to decide one of

1. namespace detail files per store (e.g. `<src>.pgn-<num>.json`), or
2. forbid exporting two stores that define the same PGN into one flat dir
   (fail the export), or
3. define a deterministic, *declared* winner and require every index to
   carry the detail's own sha/identity so a consumer can detect the mismatch.

Cheaper to settle now, against this fixture, than after Light has a parser
built around one assumption.

## Contract gap — provenance has no schema home

The §2 table schema has **no field for a provenance/warning string**. The
"this is synthetic" marker survives only as:

- `_synthetic` — a top-level key the validator ignores (unknown-key path), so
  it is advisory to a human reading the file, not contract data; and
- `source_doc` in the §3 manifest — authoritative, but set by the *writer* at
  ingest (`store.install(..., source_doc=SOURCE_DOC)`), not carried by the
  table file. The normal inbox path stamps `source_doc="inbox:<file>"`, which
  would **lose** the warning.

So a fixture ingested through the converter inbox is labelled by its filename,
not by its synthetic nature. If §2 wants tables to be able to declare their
own provenance, that field does not exist today.

## Schema notes (from `tables/validate.py`)

Two constraints are *not* enforced by the validator, so the fixtures do not
rely on them and Light must not either:

- **No payload-fit / overlap check.** Only `BitOffset ≥ 0` and
  `BitLength ∈ 1..64` are bounded. "Single-frame" is semantic (PF ≥ 240, not
  fast), not a checked property of the field packing.
- **`Resolution: 0` silently becomes 1** (`float(x or 1)`), same for `Offset`.
  The fixtures never use 0 resolutions, so nothing is masked.

## Non-usability safeguards

These must never decode a real bus by accident:

- every PGN is in **data page 3** (`pgn >> 16 == 3`); NMEA2000 uses DP 0/1
  only, DP 2/3 are ISO/reserved — no real device emits these ids;
- units are `SYN-`prefixed, lookup labels are `SYN-*`;
- `source_doc` = `SYNTHETIC FIXTURE — not a real PGN table` and the file
  carries the `_synthetic` marker.

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
2-signal definition, for the collision case above.
