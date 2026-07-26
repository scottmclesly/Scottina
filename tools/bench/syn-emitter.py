#!/usr/bin/env python3
"""Synthetic ground-truth CAN emitter — bench equipment, NOT a Scottina feature.

Transmits the `synthetic-marine` table's PGNs (tests/fixtures/tables/) on a
bench CAN bus and, for every frame, records the raw bytes AND the field values
that frame encodes. That `ground-truth.jsonl` is the file Prime's decoder
(`kilodash/n2k.py`) and Light's decoder are checked against — numerically,
not by eye.

Independence: this emitter implements the §2 encode and its own expected
decode; it does NOT import `kilodash.n2k`. Two independent implementations of
one spec — that is what makes the golden-vector comparison meaningful rather
than circular. It loads only `tables/validate.py` (the shared schema) to read
the field definitions.

Safety envelope:
  * every arbitration id is asserted to be in reserved data page 3 — a
    comment is not enforcement;
  * refuses to transmit without --bench-bus-confirmed, and prints the PGN
    list + bitrate first;
  * lives under tools/bench/, never inside a Scottina package. No Scottina UI
    path constructs these frames (asserted in tests/test_syn_emitter.py).

Determinism: --seed N reproduces an identical frame sequence (values and
schedule). --dry-run writes the same ground truth without touching the bus,
so golden vectors are generated with zero hardware.

Usage:
    # generate ground truth only (no bus):
    python tools/bench/syn-emitter.py --dry-run --duration 0.6 --sources 2 \
        --out ground-truth.jsonl
    # transmit on a live bench bus (can0 already up at the bitrate):
    python tools/bench/syn-emitter.py --interface can0 --bench-bus-confirmed
"""

import argparse
import json
import os
import struct
import sys
import time
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO)
from tables import validate  # noqa: E402

DEFAULT_TABLE = os.path.join(
    _REPO, "tests/fixtures/tables/synthetic-marine.json")
DP3 = 0x30000
PRIORITY = 6
ABSENT_INTERVAL_MS = 1000          # emit rate for a PGN with no table interval
CAN_EFF_FLAG = 0x80000000

# Fields with a fixed content value (raw counts).
FIXED = {"SYN Manufacturer ID": 2047,   # 0x7FF: unassigned AND all-ones ⇒ NA
         "SYN Reserved 1": 3, "SYN Industry Group": 4, "SYN Reserved 2": 0xF}
# Fields swept by decoded VALUE (rather than raw), including edges/NA (None).
EDGE_VALUES = {"SYN Signed Level": [-100, 0, 100, None]}
# The conditional field §2 cannot express: when the Select field's bit 1 is
# set the real meaning is (0.25, SYN-rpm); the table carries only (0.1,
# SYN-pct). {pgn_name-independent: (alt_res, alt_unit, select_field, bit)}.
CONDITIONAL = {"SYN Scaled Value": (0.25, "SYN-rpm", "SYN Select", 1)}


# ----------------------------------------------------------------- decode --
def is_na(raw, n, signed):
    """§2 not-available sentinel — INDEPENDENT of n2k.is_na (deliberately)."""
    if n < 2:
        return False
    return raw == ((1 << (n - 1)) - 1 if signed else (1 << n) - 1)


def expected_value(field, raw):
    """Decoded value per §2, computed here so the golden test cross-checks two
    implementations. None for not-available."""
    n = field["bit_length"]
    if is_na(raw, n, field["signed"]):
        return None
    sval = raw - (1 << n) if field["signed"] and raw & (1 << (n - 1)) else raw
    return sval * field["resolution"] + field["offset"]


def encode_value(field, value):
    """Inverse of expected_value: decoded value → raw counts (two's-complement
    for signed). value None → the NA sentinel."""
    n = field["bit_length"]
    if value is None:
        return ((1 << (n - 1)) - 1) if field["signed"] else (1 << n) - 1
    raw = round((value - field["offset"]) / field["resolution"])
    return raw & ((1 << n) - 1)


# ------------------------------------------------------------------ sweep --
def raw_edges(field):
    """The raw values a field cycles through — every edge, incl NA."""
    name, n = field["name"], field["bit_length"]
    if name in FIXED:
        return [FIXED[name]]
    if name in EDGE_VALUES:
        return [encode_value(field, v) for v in EDGE_VALUES[name]]
    if "Select" in name:
        return [0, 2]                       # toggles the conditional field
    if field["lookup"]:
        return sorted(int(k) for k in field["lookup"])   # every enum incl NA
    if n == 1:
        return [0, 1]
    if "Flag" in name:
        return [0, (1 << n) - 1]            # all-clear, all-set (=NA if n>1)
    if field["signed"]:
        na = (1 << (n - 1)) - 1
        return [0, 1 << (n - 1), na - 1, na]   # 0, most-neg, max-pos, NA
    na = (1 << n) - 1
    return [0, 1, na - 1, na]                   # 0, 1, max-non-NA, NA


def raw_for(entry, field, src, k, seed):
    """Deterministic raw value for `field` on frame `k` from source `src`.
    Source-instance fields carry the address; everything else cycles its edge
    list with a seed+src phase offset so --seed reproduces an identical run."""
    name, n = field["name"], field["bit_length"]
    if "Source Instance" in name:
        return src & ((1 << n) - 1)
    edges = raw_edges(field)
    key = f"{seed}:{entry['pgn']}:{src}:{name}".encode()
    off = zlib.crc32(key)
    return edges[(k + off) % len(edges)]


# ------------------------------------------------------------------ frame --
def arb_id(pgn, src):
    assert (pgn >> 16) == 3, f"PGN {pgn:#x} not in reserved data page 3"
    return (PRIORITY << 26) | (pgn << 8) | (src & 0xFF)


def build_frame(entry, src, k, seed):
    """(arb_id, 8 raw bytes, ground-truth dict) for one frame — no bus I/O."""
    acc, gt_fields, cond = 0, {}, None
    raws = {}
    for f in entry["fields"]:
        raw = raw_for(entry, f, src, k, seed)
        raws[f["name"]] = raw
        acc |= (raw & ((1 << f["bit_length"]) - 1)) << f["bit_offset"]
    data = acc.to_bytes(8, "little")
    for f in entry["fields"]:
        gt_fields[f["name"]] = expected_value(f, raws[f["name"]])
        if f["name"] in CONDITIONAL:
            alt_res, alt_unit, sel_name, sel_bit = CONDITIONAL[f["name"]]
            bit = (raws.get(sel_name, 0) >> sel_bit) & 1
            table_val = gt_fields[f["name"]]      # what §2 / the table decodes
            # NA (sentinel raw) is NA under either scale; only a real value
            # re-scales when Select bit 1 is set.
            intended = (raws[f["name"]] * alt_res
                        if (bit and table_val is not None) else table_val)
            cond = {"field": f["name"], "select_bit": bit,
                    "table": {"value": table_val, "unit": f["units"]},
                    "intended": {"value": intended,
                                 "unit": alt_unit if bit else f["units"]}}
    pgn = entry["pgn"]
    gt = {"pgn": pgn, "src": src, "id": arb_id(pgn, src), "dlc": 8,
          "raw": data.hex(), "fields": gt_fields}
    if cond is not None:
        gt["cond"] = cond
    return gt["id"], data, gt


# --------------------------------------------------------------- schedule --
def emit_pgns(tables):
    """Eligible single-frame PDU2 PGNs only. Fast-packet needs multi-frame
    framing and PDU1 is out of the gauge's scope, so neither is transmitted —
    they exist in the table purely for index-exclusion tests."""
    out = []
    for pgn in sorted(tables):
        e = tables[pgn]
        if e["fast"] or ((pgn >> 8) & 0xFF) < 240:
            continue
        out.append(e)
    return out


def schedule(tables, sources, duration, seed):
    """Deterministic (scheduled_t, entry, src, k) events, time-ordered. Each
    (pgn, src) fires at k * interval. Ties broken by (pgn, src) for a stable,
    reproducible order."""
    events = []
    for e in emit_pgns(tables):
        iv = (e["interval_ms"] or ABSENT_INTERVAL_MS) / 1000.0
        for src in range(sources):
            k, t = 0, 0.0
            while t < duration:
                events.append((round(t, 6), e["pgn"], src, k, e))
                k += 1
                t = k * iv
    events.sort(key=lambda ev: (ev[0], ev[1], ev[2]))
    return events


def ground_truth(tables, sources, duration, seed):
    """Yield one ground-truth record per scheduled frame (no bus I/O)."""
    for t, _pgn, src, k, e in schedule(tables, sources, duration, seed):
        _id, _data, gt = build_frame(e, src, k, seed)
        yield {"t": t, **gt}


# ------------------------------------------------------------------- main --
def load_table(path):
    tables, _warns = validate.validate_file(path)
    return tables


def _print_plan(tables, args):
    print(f"[syn-emitter] table: {args.table}")
    print(f"[syn-emitter] bitrate: {args.bitrate} (interface must already be "
          f"up at this rate)")
    print(f"[syn-emitter] seed={args.seed} sources={args.sources} "
          f"duration={args.duration}s")
    print("[syn-emitter] PGNs to transmit (eligible single-frame PDU2):")
    for e in emit_pgns(tables):
        iv = e["interval_ms"] or ABSENT_INTERVAL_MS
        print(f"    {e['pgn']:#07x}  {e['name']:<20} every {iv:>5} ms")


def main(argv=None):
    ap = argparse.ArgumentParser(description="synthetic ground-truth emitter")
    ap.add_argument("--table", default=DEFAULT_TABLE)
    ap.add_argument("--interface", default="can0")
    ap.add_argument("--bitrate", type=int, default=250000)
    ap.add_argument("--sources", type=int, default=2)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="ground-truth.jsonl")
    ap.add_argument("--dry-run", action="store_true",
                    help="write ground truth only; never touch the bus")
    ap.add_argument("--bench-bus-confirmed", action="store_true",
                    help="required to transmit — asserts this is the bench bus")
    args = ap.parse_args(argv)

    tables = load_table(args.table)
    _print_plan(tables, args)

    if args.dry_run:
        n = 0
        with open(args.out, "w") as f:
            for rec in ground_truth(tables, args.sources, args.duration,
                                    args.seed):
                f.write(json.dumps(rec) + "\n")
                n += 1
        print(f"[syn-emitter] dry-run: wrote {n} ground-truth records → "
              f"{args.out} (no frames transmitted)")
        return 0

    if not args.bench_bus_confirmed:
        print("[syn-emitter] REFUSING to transmit without "
              "--bench-bus-confirmed.\n"
              "  Use --dry-run to generate ground truth off-bus, or pass\n"
              "  --bench-bus-confirmed once the bench bus (two nodes, 120 Ω\n"
              "  both ends, nothing else) is confirmed.", file=sys.stderr)
        return 2

    import socket
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.bind((args.interface,))
    print(f"[syn-emitter] transmitting on {args.interface} …")
    t0 = time.monotonic()
    n = 0
    with open(args.out, "w") as f:
        for t, _pgn, src, k, e in schedule(tables, args.sources,
                                           args.duration, args.seed):
            wait = t - (time.monotonic() - t0)
            if wait > 0:
                time.sleep(wait)
            cid, data, gt = build_frame(e, src, k, args.seed)
            frame = struct.pack("=IB3x8s", cid | CAN_EFF_FLAG, len(data), data)
            sock.send(frame)
            f.write(json.dumps({"t": t, **gt}) + "\n")
            n += 1
    sock.close()
    print(f"[syn-emitter] transmitted {n} frames; ground truth → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
