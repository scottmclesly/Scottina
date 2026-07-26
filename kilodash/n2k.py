"""NMEA2000 semantic-decode core for the NMEA2K screen (headless, testable).

Table-driven: everything here decodes against the PGN tables the converter
installed in the table store (TABLES.md) — this module never mutates the
store (consumers only read). Pipeline per the split TODO:

    raw frame → fast-packet reassembly → PGN lookup → field extraction

**Fast-packet reassembly lives here**, before decode, because Canboat-style
JSON describes *assembled* payloads: sequence/frame counters, per-(PGN,
source) state, out-of-order drops. Scottina Light's reassembly wasn't
extractable into Python, so this is a fresh implementation of the N2K
fast-packet framing, unit-tested in tests/test_n2k.py — bench-validate
against captured multi-frame PGNs before trusting it on real traffic
(Known gotchas: synthetic happy-path frames are not enough).

Scope: diagnostics only, RX-only — same constraint as busmon, enforced by
the same test style. Address-claim/ISO-request replies (the one TX
exception) are the link layer's job, never done here.
"""

import json
import math
import os
import threading
import time

FP_MAX = 223                    # fast-packet max assembled payload
RECORD_MAX = 20_000             # bounded decoded-record log
RATE_WINDOW = 2.0
ALERT_FLASH_S = 1.5


def split_id(cid):
    """29-bit arbitration id → (pgn, source, priority). PDU1 (PF < 240)
    carries a destination address in PS — masked out of the PGN."""
    src = cid & 0xFF
    prio = (cid >> 26) & 0x7
    pgn = (cid >> 8) & 0x3FFFF
    if (pgn >> 8) & 0xFF < 240:         # PDU1: PS byte is DA, not PGN
        pgn &= 0x3FF00
    return pgn, src, prio


def is_na(raw, bit_length, signed):
    """N2K not-available sentinel: all-ones unsigned / max-positive signed
    (fields wider than 1 bit — a 1-bit flag's 1 is a real value)."""
    if bit_length < 2:
        return False
    if signed:
        return raw == (1 << (bit_length - 1)) - 1
    return raw == (1 << bit_length) - 1


def extract_field(payload, f):
    """(raw, value, display) per TABLES.md §2, or None when the field lies
    beyond this payload. value is None for not-available."""
    n = f["bit_length"]
    if f["bit_offset"] + n > len(payload) * 8:
        return None
    raw = (int.from_bytes(payload, "little") >> f["bit_offset"]) \
        & ((1 << n) - 1)
    if f.get("undecodable"):
        # fail-safe (TABLES.md §2): the schema cannot correctly interpret this
        # field, so never compute a number from the wrong resolution — render
        # not-available. Wrong-and-confident is worse than absent.
        return raw, None, "n/d"
    if is_na(raw, n, f["signed"]):
        return raw, None, "—"
    sval = raw - (1 << n) if f["signed"] and raw & (1 << (n - 1)) else raw
    value = sval * f["resolution"] + f["offset"]
    lookup = f["lookup"]
    if lookup and str(raw) in lookup:
        return raw, value, lookup[str(raw)]
    if f["resolution"] == 1 and not f["offset"] and value == int(value):
        disp = f"{int(value)}"
    else:
        disp = f"{value:.4g}"
    if f["units"]:
        disp += f" {f['units']}"
    return raw, value, disp


_M_PER_DEG = 111_320.0          # good enough for a delta badge


def _field_value(fields, *needles):
    """First decoded field whose name matches a needle (case-insensitive
    substring) → (value, units), or (None, None). Table field names vary
    by vendor doc, so matching is deliberately loose."""
    for f in fields:
        name = f["name"].lower()
        if any(n in name for n in needles) and f["value"] is not None:
            return f["value"], (f.get("units") or "").lower()
    return None, None


def gps_bus_delta(fields, snap):
    """GPS-vs-bus comparison (diagnostics payoff): decoded position/COG/SOG
    fields from ANOTHER source vs our local GPS snapshot. Returns a dict
    with any of dist_m / d_sog_mps / d_cog_deg that could be computed —
    empty when nothing overlaps. Callers must exclude our own claimed SA:
    comparing our sourced PGNs against their own origin would "validate"
    the GPS against itself."""
    out = {}
    if not snap or snap.get("lat") is None:
        return out
    lat, _u = _field_value(fields, "latitude")
    lon, _u = _field_value(fields, "longitude")
    if lat is not None and lon is not None:
        mlat = math.radians((lat + snap["lat"]) / 2)
        dx = (lon - snap["lon"]) * _M_PER_DEG * math.cos(mlat)
        dy = (lat - snap["lat"]) * _M_PER_DEG
        out["dist_m"] = math.hypot(dx, dy)
    sog, units = _field_value(fields, "sog", "speed over ground")
    if sog is not None and snap.get("sog_mps") is not None:
        if "knot" in units or units == "kn":
            sog *= 0.514444
        out["d_sog_mps"] = sog - snap["sog_mps"]
    cog, units = _field_value(fields, "cog", "course over ground")
    if cog is not None and snap.get("cog_deg_true") is not None:
        if "rad" in units:
            cog = math.degrees(cog)
        out["d_cog_deg"] = (cog - snap["cog_deg_true"] + 180) % 360 - 180
    return out


def delta_severity(delta):
    """Threshold badge color key for a comparison: ok < 10 m, warn < 50 m,
    bad beyond (SOG/COG-only comparisons rate by SOG: 0.5 / 2 m/s)."""
    if "dist_m" in delta:
        d = delta["dist_m"]
        return "ok" if d < 10 else "warn" if d < 50 else "bad"
    if "d_sog_mps" in delta:
        d = abs(delta["d_sog_mps"])
        return "ok" if d < 0.5 else "warn" if d < 2 else "bad"
    return "ok"


class FastPacketAssembler:
    """Per-(PGN, source) fast-packet state. feed() returns the assembled
    payload bytes when a sequence completes, else None. Tolerates a
    restarted sequence (a new first-frame always re-arms); anything
    out-of-order or seq-mismatched drops the partial assembly — garbage
    never reaches decode."""

    def __init__(self):
        self._st = {}
        self.dropped = 0

    def feed(self, key, data):
        if not data:
            return None
        b0 = data[0]
        seq, idx = b0 & 0xE0, b0 & 0x1F
        if idx == 0:
            if len(data) < 2:
                return None
            total = data[1]
            if not 0 < total <= FP_MAX:
                self.dropped += 1
                return None
            st = {"seq": seq, "need": total, "idx": 0,
                  "buf": bytearray(data[2:8])}
            if len(st["buf"]) >= total:         # fits the first frame
                self._st.pop(key, None)
                return bytes(st["buf"][:total])
            self._st[key] = st
            return None
        st = self._st.get(key)
        if st is None or st["seq"] != seq or idx != st["idx"] + 1:
            if st is not None:
                self.dropped += 1
                del self._st[key]
            return None
        st["idx"] = idx
        st["buf"] += data[1:8]
        if len(st["buf"]) >= st["need"]:
            del self._st[key]
            return bytes(st["buf"][:st["need"]])
        return None


class Decoder:
    """PGN lookup + field extraction against the loaded tables. Unknown
    PGNs are counted, never silently dropped — undecoded traffic is signal
    (they get handed to the CAN screen's mental model via sample_id)."""

    def __init__(self, tables):
        self.tables = tables
        self.fp = FastPacketAssembler()
        self.unknown = {}          # pgn -> {count, srcs, sample_id}
        self.non_n2k = 0           # 11-bit frames: not NMEA2000

    def feed(self, ts, cid, ext, data):
        """One frame in → one decoded record out (or None while a fast
        packet assembles / for unknown PGNs)."""
        if not ext:
            self.non_n2k += 1
            return None
        pgn, src, _prio = split_id(cid)
        entry = self.tables.get(pgn)
        if entry is None:
            u = self.unknown.setdefault(
                pgn, {"count": 0, "srcs": set(), "sample_id": cid})
            u["count"] += 1
            u["srcs"].add(src)
            return None
        if entry["fast"]:
            payload = self.fp.feed((pgn, src), data)
            if payload is None:
                return None
        else:
            payload = data
        fields = []
        for f in entry["fields"]:
            got = extract_field(payload, f)
            if got is None:
                continue
            raw, value, disp = got
            rec = {"name": f["name"], "raw": raw, "value": value,
                   "disp": disp, "units": "" if f.get("undecodable")
                   else f["units"]}
            if f.get("undecodable"):
                rec["undecodable"] = f["undecodable"]   # reason, for the screen
            fields.append(rec)
        return {"ts": ts, "pgn": pgn, "src": src, "name": entry["name"],
                "fields": fields}


class AlertBook:
    """The NMEA2K screen's two alert kinds (both non-modal: badge + row
    flash, evaluated at ingest):

    - range-exit: numeric field outside configured min/max — fired on the
      transition out of range, per (pgn, src, field), so a stuck-bad value
      doesn't refire every frame; not-available values never alert.
    - appearance: a configured PGN (or PGN+source) was seen at all.
    """

    def __init__(self):
        self.ranges = {}           # (pgn, field) -> {min,max,hits,last_hit}
        self.appear = {}           # (pgn, src|None) -> {hits,last_hit}
        self._state = {}           # (pgn, src, field) -> was_out (bool)

    def set_range(self, pgn, field, lo, hi):
        self.ranges[(pgn, field)] = {"min": lo, "max": hi,
                                     "hits": 0, "last_hit": 0.0}

    def clear_range(self, pgn, field):
        self.ranges.pop((pgn, field), None)

    def toggle_appearance(self, pgn, src=None):
        key = (pgn, src)
        if key in self.appear:
            del self.appear[key]
            return False
        self.appear[key] = {"hits": 0, "last_hit": 0.0}
        return True

    def check(self, rec):
        """Evaluate one decoded record; returns True if anything fired."""
        fired = False
        for key in ((rec["pgn"], rec["src"]), (rec["pgn"], None)):
            w = self.appear.get(key)
            if w:
                w["hits"] += 1
                w["last_hit"] = rec["ts"]
                fired = True
        for f in rec["fields"]:
            w = self.ranges.get((rec["pgn"], f["name"]))
            if not w or f["value"] is None:
                continue
            out = (w["min"] is not None and f["value"] < w["min"]) or \
                  (w["max"] is not None and f["value"] > w["max"])
            skey = (rec["pgn"], rec["src"], f["name"])
            if out and not self._state.get(skey):
                w["hits"] += 1
                w["last_hit"] = rec["ts"]
                fired = True
            self._state[skey] = out
        return fired

    def hits(self):
        books = list(self.ranges.values()) + list(self.appear.values())
        return sum(w["hits"] for w in books)

    def alerting(self, pgn, src, now):
        for key, w in self.appear.items():
            if key[0] == pgn and key[1] in (None, src) \
                    and now - w["last_hit"] <= ALERT_FLASH_S:
                return True
        return any(k[0] == pgn and now - w["last_hit"] <= ALERT_FLASH_S
                   for k, w in self.ranges.items())

    def recent(self, now):
        books = list(self.ranges.values()) + list(self.appear.values())
        return sum(now - w["last_hit"] <= ALERT_FLASH_S for w in books)


class N2kMonitor:
    """Thread-safe aggregate the NMEA2K screen renders from: one row per
    (PGN, source), a bounded decoded-record log with PGN/source filters,
    unknown-PGN accounting, and the AlertBook. Same shape as
    busmon.BusMonitor so busmon.RxReader can feed either."""

    def __init__(self, tables, record_max=RECORD_MAX):
        self.dec = Decoder(tables)
        self.alerts = AlertBook()
        self._lock = threading.Lock()
        self._rows = {}            # (pgn, src) -> row dict
        self._log = []             # bounded circular decoded records
        self._log_max = record_max
        self._log_head = 0
        self.total = 0

    # ------------------------------------------------------------- ingest
    def ingest(self, ts, cid, ext, rtr, data):
        if rtr:
            return
        with self._lock:
            self.total += 1
            rec = self.dec.feed(ts, cid, ext, data)
            if rec is None:
                return
            self.alerts.check(rec)
            key = (rec["pgn"], rec["src"])
            row = self._rows.get(key)
            if row is None:
                row = self._rows[key] = {
                    "pgn": rec["pgn"], "src": rec["src"], "name": rec["name"],
                    "count": 0, "rate": 0.0, "_rc": 0, "_rt": ts,
                }
            row["count"] += 1
            row["fields"] = rec["fields"]
            row["t_last"] = ts
            if len(self._log) < self._log_max:
                self._log.append(rec)
            else:
                self._log[self._log_head] = rec
                self._log_head = (self._log_head + 1) % self._log_max

    # ----------------------------------------------------------- snapshot
    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            rows = []
            for key in sorted(self._rows):
                r = self._rows[key]
                if now - r["_rt"] >= RATE_WINDOW:
                    r["rate"] = (r["count"] - r["_rc"]) / (now - r["_rt"])
                    r["_rc"], r["_rt"] = r["count"], now
                rows.append({
                    **{k: r[k] for k in ("pgn", "src", "name", "count",
                                         "rate", "fields", "t_last")},
                    "alert": self.alerts.alerting(r["pgn"], r["src"], now),
                })
            unknown = [
                {"pgn": pgn, "count": u["count"], "srcs": sorted(u["srcs"]),
                 "sample_id": u["sample_id"]}
                for pgn, u in sorted(self.dec.unknown.items())]
            stats = {"total": self.total, "log": len(self._log),
                     "hits": self.alerts.hits(),
                     "alerting": self.alerts.recent(now),
                     "unknown": sum(u["count"]
                                    for u in self.dec.unknown.values()),
                     "non_n2k": self.dec.non_n2k,
                     "fp_dropped": self.dec.fp.dropped}
            return rows, unknown, stats

    # ------------------------------------------------------------- export
    def export(self, cap_dir, pgn=None, src=None):
        """Write the (filtered) decoded log as JSON lines; returns
        (record_count, path). tmp + rename like every capture writer."""
        os.makedirs(cap_dir, exist_ok=True)
        path = os.path.join(
            cap_dir, f"n2k_{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
        with self._lock:
            if len(self._log) == self._log_max:
                recs = self._log[self._log_head:] + self._log[:self._log_head]
            else:
                recs = list(self._log)
        recs = [r for r in recs
                if (pgn is None or r["pgn"] == pgn)
                and (src is None or r["src"] == src)]
        with open(path + ".tmp", "w") as f:
            for r in recs:
                f.write(json.dumps({
                    "ts": round(r["ts"], 6), "pgn": r["pgn"], "src": r["src"],
                    "name": r["name"],
                    "fields": {fl["name"]: (fl["value"]
                                            if fl["value"] is not None
                                            else fl["disp"])
                               for fl in r["fields"]},
                }) + "\n")
        os.replace(path + ".tmp", path)
        from .busmon import write_geotag_sidecar
        write_geotag_sidecar(path)
        return len(recs), path
