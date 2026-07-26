"""Schema validator for the Canboat-JSON subset defined in TABLES.md §2.

The one validator both ends of the contract run: the converter calls it on
ingest, the NMEA2K screen calls it again on every load — defense in depth,
so a hand-copied file that skipped the converter still gets validated before
it can drive decode.

Two-tier by design (TABLES.md §2): a malformed *file* raises
TableInvalid; a malformed *entry* is skipped with a warning while the rest
of the file loads. Unknown keys are ignored, never fatal.
"""

import hashlib
import json

NAME_MAX = 64
FIELD_BITS_MAX = 64
SINGLE_FRAME_BITS = 64          # one CAN frame = 8 bytes; a non-fast PGN's
                                # fields must fit inside it (payload-fit §2)
INDEX_VERSION = 1
# N2K periodic PGNs top out around 10 s; slower-than-this is effectively
# "on request", which the picker treats as unknown rather than a rate.
INTERVAL_MS_MAX = 600_000


class TableInvalid(ValueError):
    """The file as a whole is unusable (not JSON / no valid PGN entries)."""


class IndexInvalid(ValueError):
    """A Light index (TABLES.md §5b) is malformed or offers an ineligible
    PGN (fast-packet / PDU1)."""


def is_pdu1(pgn):
    """PDU1 PGNs (PF < 240) carry a destination address in their low byte —
    excluded from the Light index (§5b): masking it around would fall to the
    consumer's hardware filters."""
    return ((pgn >> 8) & 0xFF) < 240


def _interval_ms(entry):
    """Optional nominal transmission interval (TABLES.md §2). Accepts our
    `interval_ms`/`IntervalMs` or Canboat's `TransmissionInterval` (both ms).
    Returns (ms_or_None, warning_or_None): absent is unknown, never fatal;
    a non-integer or out-of-range value is dropped to unknown with a warning."""
    for key in ("interval_ms", "IntervalMs", "TransmissionInterval"):
        if key not in entry:
            continue
        try:
            ms = int(entry[key])
        except (TypeError, ValueError):
            return None, f"interval {entry[key]!r} not an integer"
        if not 0 < ms <= INTERVAL_MS_MAX:
            return None, f"interval {ms} out of 1..{INTERVAL_MS_MAX}"
        return ms, None
    return None, None


def _norm_lookup(fld):
    """Accept our `Lookup` dict or Canboat's `EnumValues` list; return a
    {raw_decimal_string: label} dict or None."""
    lk = fld.get("Lookup")
    if isinstance(lk, dict):
        out = {}
        for k, v in lk.items():
            try:
                out[str(int(k))] = str(v)
            except (TypeError, ValueError):
                continue
        return out or None
    ev = fld.get("EnumValues")
    if isinstance(ev, list):
        out = {}
        for item in ev:
            if not isinstance(item, dict):
                continue
            try:
                out[str(int(item["value"]))] = str(item["name"])
            except (KeyError, TypeError, ValueError):
                continue
        return out or None
    return None


def _norm_field(fld):
    """Normalize one field dict, or raise ValueError describing the defect."""
    if not isinstance(fld, dict):
        raise ValueError("field is not an object")
    name = fld.get("Name") or fld.get("Id")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("field has no Name")
    try:
        bit_offset = int(fld["BitOffset"])
        bit_length = int(fld["BitLength"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"field {name!r}: BitOffset/BitLength missing or "
                         "not integers")
    if bit_offset < 0:
        raise ValueError(f"field {name!r}: negative BitOffset")
    if not 1 <= bit_length <= FIELD_BITS_MAX:
        raise ValueError(f"field {name!r}: BitLength out of 1..{FIELD_BITS_MAX}")
    res_raw = fld.get("Resolution", 1)
    if res_raw is None:                  # absent/None ⇒ identity scale
        res_raw = 1
    try:
        resolution = float(res_raw)
        eng_offset = float(fld.get("Offset", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError(f"field {name!r}: Resolution/Offset not numeric")
    if resolution == 0:
        # was silently coerced to 1 (`x or 1`); a real-table typo would then
        # decode as raw counts and look entirely plausible — reject it.
        raise ValueError(f"field {name!r}: Resolution 0 is not a valid scale")
    units = fld.get("Units", "")
    return {
        "name": name.strip(),
        "bit_offset": bit_offset,
        "bit_length": bit_length,
        "resolution": resolution,
        "offset": eng_offset,
        "signed": bool(fld.get("Signed", False)),
        "units": str(units) if units is not None else "",
        "lookup": _norm_lookup(fld),
    }


def _fast_packet(entry):
    """`FastPacket` bool, or Canboat `Type: "Fast"`."""
    fp = entry.get("FastPacket")
    if isinstance(fp, bool):
        return fp
    return str(entry.get("Type", "")).lower() == "fast"


def validate(obj):
    """Validate a parsed table object against TABLES.md §2.

    Returns (tables, warnings):
      tables   — {pgn: {"pgn", "name", "fast", "fields": [normalized…]}}
      warnings — human-readable strings for every skipped entry/field
    Raises TableInvalid when nothing usable survives.
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("PGNs"), list):
        raise TableInvalid("not a table: no PGNs array")
    tables, warnings = {}, []
    for i, entry in enumerate(obj["PGNs"]):
        if not isinstance(entry, dict):
            warnings.append(f"PGNs[{i}]: not an object — skipped")
            continue
        try:
            pgn = int(entry["PGN"])
            if not 0 < pgn <= 0x3FFFF:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            warnings.append(f"PGNs[{i}]: missing/invalid PGN — skipped")
            continue
        name = entry.get("Name") or entry.get("Description") or f"PGN {pgn}"
        raw_fields = entry.get("Fields", [])
        if not isinstance(raw_fields, list):
            warnings.append(f"PGN {pgn}: Fields is not a list — skipped")
            continue
        fast = _fast_packet(entry)
        fields = []
        for fld in raw_fields:
            try:
                nf = _norm_field(fld)
            except ValueError as e:
                warnings.append(f"PGN {pgn}: {e} — field skipped")
                continue
            end = nf["bit_offset"] + nf["bit_length"]
            if not fast and end > SINGLE_FRAME_BITS:
                # a single-frame PGN is one 8-byte CAN frame; a field past
                # bit 64 can never carry data (mislabelled fast-packet or a
                # broken table). Fast-packet PGNs assemble a larger payload.
                warnings.append(
                    f"PGN {pgn}: field {nf['name']!r} ends at bit {end} > "
                    f"{SINGLE_FRAME_BITS} of a single-frame PGN — skipped")
                continue
            fields.append(nf)
        if not fields:
            warnings.append(f"PGN {pgn}: no usable fields — skipped")
            continue
        interval_ms, iwarn = _interval_ms(entry)
        if iwarn:
            warnings.append(f"PGN {pgn}: {iwarn} — interval dropped")
        if pgn in tables:
            warnings.append(f"PGN {pgn}: duplicate entry — later one wins")
        tables[pgn] = {"pgn": pgn, "name": str(name)[:80],
                       "fast": fast, "interval_ms": interval_ms,
                       "fields": fields}
    if not tables:
        raise TableInvalid("no valid PGN entries"
                           + (f" ({warnings[0]})" if warnings else ""))
    return tables, warnings


def validate_bytes(raw):
    """Validate raw file bytes/str. TableInvalid on undecodable input."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        obj = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as e:
        raise TableInvalid(f"not JSON: {e}")
    return validate(obj)


def validate_file(path):
    """Validate a table file on disk. TableInvalid on unreadable input."""
    try:
        with open(path, "rb") as f:
            return validate_bytes(f.read())
    except OSError as e:
        raise TableInvalid(f"unreadable: {e}")


# ------------------------------------------------ Light index (§5b/§5c) --
def validate_index(obj):
    """Validate a Light index object against TABLES.md §5b. Returns the set
    of PGNs it offers. Raises IndexInvalid on a shape defect or a PDU1 entry
    — the one ineligibility derivable from the index alone. A fast-packet
    entry is caught by check_pair(), which has the source `fast` flags.

    Read-side usable: the consumer runs this before trusting an index, the
    same defense-in-depth the table validator gives (§6)."""
    if not isinstance(obj, dict) or obj.get("v") != INDEX_VERSION:
        raise IndexInvalid("not a v1 index")
    for k in ("src", "sha256", "generated"):
        if not isinstance(obj.get(k), str) or not obj[k]:
            raise IndexInvalid(f"missing {k}")
    if not isinstance(obj.get("pgns"), list):
        raise IndexInvalid("no pgns array")
    pgns = set()
    for e in obj["pgns"]:
        if not isinstance(e, dict):
            raise IndexInvalid("pgn entry is not an object")
        try:
            pgn = int(e["pgn"])
        except (KeyError, TypeError, ValueError):
            raise IndexInvalid("pgn entry missing integer pgn")
        if not 0 < pgn <= 0x3FFFF:
            raise IndexInvalid(f"pgn {pgn} out of range")
        if is_pdu1(pgn):
            raise IndexInvalid(f"pgn {pgn} is PDU1 — excluded from the index")
        if not isinstance(e.get("n"), str) or not e["n"]:
            raise IndexInvalid(f"pgn {pgn}: missing name")
        if "ms" in e:
            try:
                ms = int(e["ms"])
            except (TypeError, ValueError):
                raise IndexInvalid(f"pgn {pgn}: ms not an integer")
            if not 0 < ms <= INTERVAL_MS_MAX:
                raise IndexInvalid(f"pgn {pgn}: ms out of range")
        if not isinstance(e.get("sig"), list):
            raise IndexInvalid(f"pgn {pgn}: missing sig list")
        pgns.add(pgn)
    return pgns


def check_pair(index, details):
    """Cross-check a Light index against its per-PGN detail dicts
    (`{pgn: detail}`, §5c). Raises IndexInvalid when the PGN sets disagree or
    a detail is ineligible (fast-packet / PDU1). The two-gate rule: the
    writer already excludes these, and the validator does not trust it — a
    hand-desynced pair is rejected here."""
    idx_pgns = validate_index(index)
    det_pgns = set()
    for key, d in details.items():
        if not isinstance(d, dict):
            raise IndexInvalid(f"detail {key}: not an object")
        try:
            p = int(d["pgn"])
        except (KeyError, TypeError, ValueError):
            raise IndexInvalid(f"detail {key}: missing integer pgn")
        if p != int(key):
            raise IndexInvalid(f"detail {key}: pgn {p} mismatches its key")
        if is_pdu1(p):
            raise IndexInvalid(f"detail {p}: PDU1 not permitted")
        if d.get("fast"):
            raise IndexInvalid(f"detail {p}: fast-packet not permitted")
        det_pgns.add(p)
    if idx_pgns != det_pgns:
        raise IndexInvalid(
            f"index/detail PGN sets disagree: "
            f"index-only={sorted(idx_pgns - det_pgns)}, "
            f"detail-only={sorted(det_pgns - idx_pgns)}")


def detail_sha_ok(index, pgn, detail_bytes):
    """Detection gate for the §5b per-detail sha256: True iff the index's
    recorded `sha` for `pgn` matches sha256(detail_bytes). A consumer that
    loads `<src>.pgn-<num>.json` hashes its bytes and refuses to decode when
    this returns False — prevention (namespacing) plus detection, the
    standing two-gate rule. An index entry with no `sha` asserts nothing
    (True); an unknown pgn is False."""
    if isinstance(detail_bytes, str):
        detail_bytes = detail_bytes.encode()
    for e in index.get("pgns", []):
        try:
            if int(e["pgn"]) != int(pgn):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        want = e.get("sha")
        if not want:
            return True
        return hashlib.sha256(detail_bytes).hexdigest() == want
    return False
