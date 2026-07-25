"""Light index + per-PGN detail export (TABLES.md §5b/§5c).

The browse-then-decode artifacts for Scottina Light's CAN gauge, emitted
ALONGSIDE the flat decode-only export, never replacing it. Light's picker
has to present PGNs on a 192 KB device before decoding any of them, which
needs a compact index and a rate budget the flat export doesn't carry.

Two shapes, one shared writer so the web download and the USB push emit
byte-identical bytes:
  - the index (`<src>.index.json`): abbreviated keys, names + interval only,
    no bit-field definitions — the picker;
  - per-PGN detail (`pgn-<num>.json`): the full §2 field definitions,
    loaded one at a time only for the handful the user selected.

Everything here is DERIVED from the store on every export — never
hand-maintained. An index that drifts from the tables it indexes decodes
plausible garbage, the exact failure this contract exists to prevent, so
`sha256` binds the index to the source table's exact bytes and `generated`
mirrors the manifest's `converted` time (re-exporting an unchanged store is
byte-stable).

Fast-packet and PDU1 PGNs are excluded at export time — Light cannot
reassemble the first safely (one dropped frame yields a plausible wrong
value) and the second's destination address would have to be masked in
hardware filters. The validator rejects them again on read: two gates, per
the standing rule (TABLES.md §6).
"""

import json
import os

from . import store, validate

INDEX_VERSION = validate.INDEX_VERSION


def index_filename(src):
    return src + ".index.json"


def detail_filename(pgn):
    return f"pgn-{int(pgn)}.json"


def eligible(entry):
    """A PGN Light may be offered: single-frame (reassembly-safe) and PDU2
    (no destination address to mask). TABLES.md §5b."""
    return not entry.get("fast") and not validate.is_pdu1(entry["pgn"])


def dumps(obj):
    """The one serializer for Light artifacts — compact (parsed on a 192 KB
    device) and deterministic (dicts are built in a fixed key order, so the
    two export paths emit byte-identical files)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def build_detail(entry):
    """Full §2 field definitions for one PGN (TABLES.md §5c). `fast` is
    carried so the consumer's read-side check can reject it independently."""
    d = {"v": INDEX_VERSION, "pgn": entry["pgn"], "name": entry["name"],
         "fast": bool(entry.get("fast"))}
    if entry.get("interval_ms") is not None:
        d["interval_ms"] = entry["interval_ms"]
    d["fields"] = [
        {"name": f["name"], "bit_offset": f["bit_offset"],
         "bit_length": f["bit_length"], "resolution": f["resolution"],
         "offset": f["offset"], "signed": f["signed"],
         "units": f["units"], "lookup": f["lookup"]}
        for f in entry["fields"]]
    return d


def _index_entry(entry):
    """One index row — names only, abbreviated keys. `ms` and per-signal `u`
    are omitted when unknown/empty to hold the ~120 B/PGN budget."""
    e = {"pgn": entry["pgn"], "n": entry["name"]}
    if entry.get("interval_ms") is not None:
        e["ms"] = entry["interval_ms"]
    sig = []
    for i, f in enumerate(entry["fields"]):
        s = {"i": i, "n": f["name"]}
        if f["units"]:
            s["u"] = f["units"]
        sig.append(s)
    e["sig"] = sig
    return e


def build_index(src, sha256, generated, entries):
    """entries: eligible normalized PGN entries, PGN-sorted by the caller."""
    return {"v": INDEX_VERSION, "src": src, "sha256": sha256,
            "generated": generated,
            "pgns": [_index_entry(e) for e in entries]}


def artifacts_for_table(name):
    """Build the Light artifacts for one installed store table, derived from
    the store. Returns `(index_str, {detail_filename: detail_str})`, or None
    when the table has no verifiable manifest. Raises validate.TableInvalid
    if the table itself no longer validates.

    `sha256` is the live digest of the table file (binding the index to the
    exact bytes exported beside it); `generated` mirrors the manifest's
    `converted`, so re-exporting an unchanged store is byte-stable."""
    meta = store.read_meta(name)
    if not meta or not meta.get("converted"):
        return None
    path = store.table_path(name)
    sha = store.sha256_file(path)
    if sha is None:
        return None
    tables, _warns = validate.validate_file(path)
    entries = [tables[p] for p in sorted(tables) if eligible(tables[p])]
    details_by_pgn = {e["pgn"]: build_detail(e) for e in entries}
    index = build_index(name, sha, meta["converted"], entries)
    # two-gate (§6): the writer just excluded fast/PDU1; the validator, which
    # does not trust the writer, restates it before anything is emitted.
    validate.check_pair(index, details_by_pgn)
    return (dumps(index),
            {detail_filename(p): dumps(d) for p, d in details_by_pgn.items()})


def _write_atomic(path, text):
    """tmp-file + fsync + rename, matching tables/store.py. A crash mid-write
    leaves the previous file or none, never a partial one."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_table_artifacts(dest_dir, name):
    """Emit `<name>.index.json` + `pgn-<num>.json` for one table into
    dest_dir, atomically (TABLES.md §5b). Returns the filenames written, or
    [] when the table has no artifacts."""
    built = artifacts_for_table(name)
    if built is None:
        return []
    index_str, details = built
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    ordered = [(index_filename(name), index_str), *sorted(details.items())]
    for fn, text in ordered:
        _write_atomic(os.path.join(dest_dir, fn), text)
        written.append(fn)
    return written
