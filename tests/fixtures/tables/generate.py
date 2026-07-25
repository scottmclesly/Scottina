#!/usr/bin/env python3
"""Generate the synthetic decode-table fixtures used to exercise the Light
index export path (TABLES.md §5b/§5c) and, later, Light's picker.

These are PLUMBING fixtures, not real decode tables. Every PGN number, field
offset, resolution and unit is invented. They are made deliberately
un-usable on a real bus:

  * every PGN lives in **data page 3** (`pgn >> 16 == 3`). NMEA2000 assigns
    only DP 0 and DP 1; DP 2/3 are ISO/reserved, so no real device emits
    these ids — feeding real traffic through them can only ever produce
    garbage, never a plausible-looking value.
  * units are `SYN-` prefixed and lookup labels are `SYN-*`, so nothing a
    consumer renders can be mistaken for a real engineering unit.
  * the file carries a top-level `_synthetic` marker and every store is
    ingested with `source_doc = SOURCE_DOC`.

Schema is DERIVED FROM `tables/validate.py` (the sole source of truth), and
this generator re-validates every store it writes — if the validator drifts,
generation fails loudly rather than emitting something that no longer
matches the contract.

Regenerate (from the repo root) with:

    python tests/fixtures/tables/generate.py

The two `synthetic-*.json` files it writes are tracked; do not hand-edit
them — change this generator and re-run.
"""

import json
import os
import sys

# repo root on path so `tables` imports the in-tree contract modules
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
sys.path.insert(0, _REPO)

from tables import lightindex, validate  # noqa: E402

# The provenance warning. NOTE (contract gap): the §2 table schema has no
# field for this — it is ignored-unknown-key data in the file (`_synthetic`)
# and only becomes authoritative once carried into the §3 manifest's
# `source_doc` at ingest. See NOTES.md.
SOURCE_DOC = "SYNTHETIC FIXTURE — not a real PGN table"

# --- synthetic PGN ids, all in data page 3 (reserved → never a real bus id) --
DP3 = 0x30000


def _pgn(pf, ps=0):
    """A DP-3 PGN with the given PF/PS. PF ≥ 240 → PDU2, PF < 240 → PDU1."""
    pgn = DP3 | (pf << 8) | ps
    assert 0 < pgn <= 0x3FFFF, hex(pgn)
    return pgn


# nine single-frame PDU2 ids (PF 0xF0, PS 0..8), one fast-packet PDU2, one PDU1
P = [_pgn(0xF0, i) for i in range(9)]     # P[0..8]  eligible
P_FAST = _pgn(0xFB, 0)                     # PDU2 but fast-packet → excluded
P_PDU1 = _pgn(0x10, 0)                     # PF 0x10 < 240 → PDU1 → excluded
P_OVERLAP = P[2]                           # reused by synthetic-overlap

_UNITS = ["SYN-degC", "SYN-V", "SYN-m", "SYN-rad", "SYN-kn", "SYN-pct"]
_LENS = [8, 16, 4, 16, 8, 12]             # sum for 6 sigs == 64 (fits a frame)
_RES = [0.1, 0.01, 1, 0.001, 0.5, 0.25]   # never 0 (0 → 1 in validator)
_ENUM = {"0": "SYN-OFF", "1": "SYN-ON", "2": "SYN-FAULT"}


def _fields(prefix, n, lookup_at=None):
    """n sequentially-packed synthetic signals. `lookup_at` turns one field
    into a lookup-enum instead of a scaled numeric, so Light's enum rendering
    gets exercised."""
    out, off = [], 0
    for i in range(n):
        f = {"Name": f"{prefix} Sig {i}", "BitOffset": off,
             "BitLength": _LENS[i % len(_LENS)],
             "Resolution": _RES[i % len(_RES)], "Signed": bool(i % 2),
             "Units": _UNITS[i % len(_UNITS)]}
        if i == lookup_at:
            f.update({"BitLength": 4, "Resolution": 1, "Units": "",
                      "Lookup": dict(_ENUM)})
        off += f["BitLength"]
        out.append(f)
    return out


def _entry(pgn, name, prefix, n, *, interval=None, fast=False, lookup_at=None):
    e = {"PGN": pgn, "Name": name, "FastPacket": fast,
         "Fields": _fields(prefix, n, lookup_at)}
    if interval is not None:                 # absent key ⇒ unknown interval
        e["TransmissionInterval"] = interval
    return e


def build_basic():
    """One store with enough eligible PGNs (9) to make Light's 6-PGN picker
    limit bite, plus one fast-packet and one PDU1 that must be excluded."""
    return _wrap([
        _entry(P[0], "SYN Alpha", "Alpha", 1, interval=100),
        _entry(P[1], "SYN Bravo", "Bravo", 2, interval=100),
        _entry(P[2], "SYN Charlie", "Charlie", 3, interval=1000),  # overlap
        _entry(P[3], "SYN Delta", "Delta", 4, interval=1000),
        _entry(P[4], "SYN Echo", "Echo", 5, interval=5000),
        _entry(P[5], "SYN Foxtrot", "Foxtrot", 6),               # no interval
        _entry(P[6], "SYN Golf", "Golf", 2, interval=999_999_999),  # OOR
        _entry(P[7], "SYN Hotel", "Hotel", 3, interval=250, lookup_at=1),
        _entry(P[8], "SYN India", "India", 1, interval=100, lookup_at=0),
        _entry(P_FAST, "SYN Juliet (fast-packet)", "Juliet", 3,
               interval=1000, fast=True),                           # EXCLUDED
        _entry(P_PDU1, "SYN Kilo (PDU1)", "Kilo", 1, interval=100),  # EXCLUDED
    ])


def build_overlap():
    """A second store that redefines ONE PGN also present in synthetic-basic,
    with a deliberately different name/shape. Exists to exercise the §5c
    detail-file collision (`pgn-<num>.json` is not namespaced by store)."""
    return _wrap([
        _entry(P_OVERLAP, "SYN Charlie OVERLAP VARIANT", "Overlap", 2,
               interval=2000),
    ])


def _wrap(pgns):
    return {"_synthetic": SOURCE_DOC,
            "_note": "Plumbing fixture — invented ids/offsets, DP3 reserved, "
                     "not usable on a real bus. Regenerate via generate.py.",
            "PGNs": pgns}


STORES = {"synthetic-basic": build_basic, "synthetic-overlap": build_overlap}


def _selfcheck(name, obj):
    """Re-validate against tables/validate.py and assert the properties the
    fixtures exist to guarantee — so a generator/validator drift fails here."""
    tables, warns = validate.validate(obj)
    eligible = {p for p, e in tables.items() if lightindex.eligible(e)}
    if name == "synthetic-basic":
        assert eligible == set(P), (sorted(eligible), sorted(P))
        assert P_FAST in tables and not lightindex.eligible(tables[P_FAST])
        assert P_PDU1 in tables and not lightindex.eligible(tables[P_PDU1])
        assert tables[P[5]]["interval_ms"] is None            # absent path
        assert tables[P[6]]["interval_ms"] is None            # OOR → unknown
        assert any("out of 1.." in w for w in warns), warns   # with a warning
        assert any(f["lookup"] for f in tables[P[7]]["fields"])
        assert any(f["units"] for f in tables[P[0]]["fields"])
    return tables, warns


def main():
    for name, build in STORES.items():
        obj = build()
        _selfcheck(name, obj)
        path = os.path.join(_HERE, name + ".json")
        with open(path, "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.write("\n")
        n_pgn = len(obj["PGNs"])
        print(f"wrote {name}.json  ({n_pgn} PGN entries)")
    print(f"source_doc for ingest: {SOURCE_DOC!r}")


if __name__ == "__main__":
    main()
