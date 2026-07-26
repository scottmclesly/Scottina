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


# ==========================================================================
# synthetic-marine — the decode-RICHNESS store (bench test kit, Phase 1).
# Copies the *shape* of a proprietary marine PGN (manufacturer header, bit
# flags, lookup enums, signed + not-available, a conditional field) and
# invents every byte. DP3 + manufacturer 2047 keep it off any real bus; no
# field carries actuation semantics (SYN- neutral names only).
# ==========================================================================
# nine eligible single-frame PDU2 (PF 0xF1, PS 0..8), one fast, one PDU1
_MF = [_pgn(0xF1, i) for i in range(9)]
MF_A, MF_B, MR_A, MR_B, MENV, MFLG, MENU, MUNK, MNOI = _MF
M_FAST = _pgn(0xFC, 0)                     # fast-packet → excluded from index
M_PDU1 = _pgn(0x11, 0)                     # PF 0x11 < 240 → PDU1 → excluded
MARINE_ELIGIBLE = list(_MF)

# 2046 = 0x7FE: unassigned, and NOT the all-ones 11-bit sentinel — so it
# decodes as a real value, not not-available. (2047/0x7FF would collide with
# the NA sentinel; the shadowing of all-ones is a separate, tested fact.)
MANUFACTURER_ID = 2046
# Category lookup. No label at 15 (0xF): that is the all-ones 4-bit NA
# sentinel, which decodes not-available regardless — a lookup cannot claim
# all-ones. Kept as a tested fact, not a gap. 14 (0xE) is a normal enum.
CATEGORY_LOOKUP = {"0": "SYN-Alpha", "1": "SYN-Bravo", "2": "SYN-Charlie",
                   "14": "SYN-Error"}


def _mf(name, off, ln, res=1, signed=False, units="", lookup=None,
        undecodable=None):
    f = {"Name": name, "BitOffset": off, "BitLength": ln,
         "Resolution": res, "Signed": signed, "Units": units}
    if lookup:
        f["Lookup"] = lookup
    if undecodable:
        f["Undecodable"] = undecodable
    return f


def _feedback_fields():
    """SYN Feedback A/B — the shape test, EXACTLY 64 bits (payload-fit
    boundary). Field 11 is conditional; §2 cannot express that, so it is
    marked Undecodable — n2k renders it not-available rather than a
    known-wrong value (the fail-safe)."""
    return [
        _mf("SYN Manufacturer ID", 0, 11),        # fixed 2046 (unassigned)
        _mf("SYN Reserved 1", 11, 2),
        _mf("SYN Industry Group", 13, 3),
        _mf("SYN Source Instance", 16, 4),
        _mf("SYN Mode Flags", 20, 4),             # per-bit; §2 has no bit names
        _mf("SYN Status Flags", 24, 4),
        _mf("SYN Select", 28, 4),                 # bit1 picks field 11
        _mf("SYN Category", 32, 4, lookup=CATEGORY_LOOKUP),
        _mf("SYN Reserved 2", 36, 4),
        _mf("SYN Signed Level", 40, 8, signed=True),   # -100..100, NA 0x7F
        # conditional: the real (resolution, unit) depends on SYN Select bit 1
        # (0.1 SYN-pct when clear, 0.25 SYN-rpm when set). §2 cannot express
        # that, so the field is Undecodable — never a confident wrong number.
        _mf("SYN Scaled Value", 48, 16, res=0.1, units="SYN-pct",
            undecodable="resolution/unit depends on SYN Select bit 1; "
                        "§2 cannot express a conditional field"),
    ]


def _rapid_fields():
    return [_mf("SYN Counter", 0, 8),
            _mf("SYN Rapid Value", 8, 16, res=0.01, units="SYN-u")]


def _env_fields():
    return [_mf("SYN Temp", 0, 16, res=0.01, signed=True, units="SYN-degC"),
            _mf("SYN Pressure", 16, 16, res=0.1, units="SYN-kPa")]


def _flag_fields():
    # pure bit-flags: §2 cannot name bits, so the ONLY faithful encoding is
    # one 1-bit field per flag (1-bit fields have no NA sentinel, so all-set
    # is representable — unlike a wide flag field, whose all-ones = NA).
    return [_mf(f"SYN Flag {i}", i, 1) for i in range(8)]


def _enum_fields():
    return [_mf("SYN Enum A", 0, 4, lookup=CATEGORY_LOOKUP),
            _mf("SYN Enum B", 4, 4, lookup=dict(_ENUM))]


def _noise_fields():
    return [_mf("SYN Noise Value", 0, 16, units="SYN-u")]


def _fast_fields():
    # fast-packet: fields legitimately span past 64 bits (assembled payload),
    # which also proves the single-frame payload-fit check is fast-exempt.
    return [_mf(f"SYN FP {i}", i * 16, 16, res=0.1, units="SYN-u")
            for i in range(6)]                     # 0..95 bits


def _entry_f(pgn, name, fields, *, interval=None, fast=False):
    e = {"PGN": pgn, "Name": name, "FastPacket": fast, "Fields": fields}
    if interval is not None:
        e["TransmissionInterval"] = interval
    return e


def build_marine():
    """Decode-richness store: 9 eligible single-frame PDU2 PGNs (real 6-PGN
    picker pressure) with the full field-shape set, plus a fast-packet and a
    PDU1 that the index must exclude. SYN Noise is the always-present,
    never-selected negative-control target for the filter run (Phase 5)."""
    return _wrap([
        _entry_f(MF_A, "SYN Feedback A", _feedback_fields(), interval=100),
        _entry_f(MF_B, "SYN Feedback B", _feedback_fields(), interval=100),
        _entry_f(MR_A, "SYN Rapid A", _rapid_fields(), interval=20),
        _entry_f(MR_B, "SYN Rapid B", _rapid_fields(), interval=20),
        _entry_f(MENV, "SYN Environment", _env_fields(), interval=1000),
        _entry_f(MFLG, "SYN Flags", _flag_fields(), interval=500),
        _entry_f(MENU, "SYN Enums", _enum_fields(), interval=1000),
        _entry_f(MUNK, "SYN Unknown", _rapid_fields()),         # no interval
        _entry_f(MNOI, "SYN Noise", _noise_fields(), interval=50),
        _entry_f(M_FAST, "SYN FastPacket", _fast_fields(),
                 interval=500, fast=True),                      # EXCLUDED
        _entry_f(M_PDU1, "SYN PDU1", _noise_fields(), interval=1000),  # EXCLUDED
    ])


def build_invalid():
    """A store the validator must REJECT — one field with Resolution 0, one
    field extending past bit 64 of a single-frame PGN. Kept apart from
    synthetic-marine so that one stays clean; proves the two validator checks
    fire (test asserts each raises with the right reason)."""
    return _wrap([
        _entry_f(_pgn(0xF2, 0), "SYN Bad Resolution",
                 [_mf("SYN zero-scale", 0, 8, res=0)]),
        _entry_f(_pgn(0xF2, 1), "SYN Past Frame",
                 [_mf("SYN overrun", 60, 16)]),               # ends at 76 > 64
    ])


def _wrap(pgns):
    return {"_synthetic": SOURCE_DOC,
            "_note": "Plumbing fixture — invented ids/offsets, DP3 reserved, "
                     "not usable on a real bus. Regenerate via generate.py.",
            "PGNs": pgns}


STORES = {"synthetic-basic": build_basic, "synthetic-overlap": build_overlap,
          "synthetic-marine": build_marine, "synthetic-invalid": build_invalid}


def _selfcheck(name, obj):
    """Re-validate against tables/validate.py and assert the properties the
    fixtures exist to guarantee — so a generator/validator drift fails here."""
    if name == "synthetic-invalid":
        # this store exists to be REJECTED — both bad fields are the only
        # fields on their PGNs, so nothing survives and the file is fatal.
        try:
            validate.validate(obj)
        except validate.TableInvalid:
            return None, None
        raise AssertionError("synthetic-invalid unexpectedly validated clean")

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
    if name == "synthetic-marine":
        assert eligible == set(MARINE_ELIGIBLE), sorted(eligible)
        assert not lightindex.eligible(tables[M_FAST])        # fast excluded
        assert not lightindex.eligible(tables[M_PDU1])        # PDU1 excluded
        fa = tables[MF_A]["fields"]
        assert len(fa) == 11                                  # nothing dropped
        assert fa[-1]["bit_offset"] + fa[-1]["bit_length"] == 64  # boundary
        assert fa[-1]["undecodable"]                          # conditional field
        assert fa[0]["name"] == "SYN Manufacturer ID"
        cat = tables[MF_A]["fields"][7]["lookup"] or {}
        assert "15" not in cat                                # no all-ones label
        assert tables[MUNK]["interval_ms"] is None            # lower-bound path
        assert any(f["lookup"] for f in tables[MENU]["fields"])
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
