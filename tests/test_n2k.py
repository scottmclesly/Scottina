"""Unit tests for the NMEA2000 decode core (kilodash/n2k.py).

Run from the repo root:  python -m unittest discover -s tests
Covers 29-bit id → PGN/source splitting (PDU1 vs PDU2), field extraction
(LSB-first packing, resolution/offset, signed, lookup enums, the
not-available sentinels), fast-packet reassembly (single-frame fit,
multi-frame, out-of-order and wrong-seq drops, restart tolerance), unknown
PGN accounting with a sample arbitration id for the CAN-screen handover,
the two alert kinds (range-exit transition-fired, appearance), the bounded
decoded log + JSONL export, and — same scope constraint as the CAN screen
— the RX-only AST scan over the N2K modules. Stdlib only, no sockets.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kilodash import n2k  # noqa: E402
from tests.test_busmon import TestNoTx  # noqa: E402

# 0x1F513 == 128275 Distance Log? Use real ids: Wind Data PGN 130306 (PDU2),
# ISO Request 59904 = 0xEA00 (PDU1).
WIND_ID = 0x09FD0223        # prio 2, PGN 130306 (0x1FD02), src 0x23
TABLES = {
    130306: {"pgn": 130306, "name": "Wind Data", "fast": False, "fields": [
        {"name": "SID", "bit_offset": 0, "bit_length": 8, "resolution": 1,
         "offset": 0, "signed": False, "units": "", "lookup": None},
        {"name": "Wind Speed", "bit_offset": 8, "bit_length": 16,
         "resolution": 0.01, "offset": 0, "signed": False, "units": "m/s",
         "lookup": None},
        {"name": "Reference", "bit_offset": 40, "bit_length": 3,
         "resolution": 1, "offset": 0, "signed": False, "units": "",
         "lookup": {"0": "True (ground)", "2": "Apparent"}},
    ]},
    129029: {"pgn": 129029, "name": "GNSS Position Data", "fast": True,
             "fields": [
                 {"name": "SID", "bit_offset": 0, "bit_length": 8,
                  "resolution": 1, "offset": 0, "signed": False, "units": "",
                  "lookup": None},
                 {"name": "Altitude", "bit_offset": 8, "bit_length": 16,
                  "resolution": 1, "offset": -100, "signed": True,
                  "units": "m", "lookup": None},
             ]},
}


class TestSplitId(unittest.TestCase):
    def test_pdu2_keeps_ps(self):
        pgn, src, prio = n2k.split_id(WIND_ID)
        self.assertEqual((pgn, src, prio), (130306, 0x23, 2))

    def test_pdu1_masks_da(self):
        # ISO Request 59904 (0xEA00) sent to DA 0x42 from 0x05
        cid = (6 << 26) | (0xEA << 16) | (0x42 << 8) | 0x05
        pgn, src, prio = n2k.split_id(cid)
        self.assertEqual((pgn, src, prio), (59904, 0x05, 6))


class TestExtract(unittest.TestCase):
    F = staticmethod(lambda **kw: {
        "bit_offset": 0, "bit_length": 8, "resolution": 1, "offset": 0,
        "signed": False, "units": "", "lookup": None, "name": "x", **kw})

    def test_resolution_and_units(self):
        f = self.F(bit_offset=8, bit_length=16, resolution=0.01, units="V")
        raw, value, disp = n2k.extract_field(b"\x00\x00\x05", f)
        self.assertEqual(raw, 0x0500)
        self.assertAlmostEqual(value, 12.8)
        self.assertEqual(disp, "12.8 V")

    def test_signed_and_offset(self):
        f = self.F(bit_length=16, signed=True, offset=-100)
        raw, value, _ = n2k.extract_field(b"\xFE\xFF", f)   # raw -2
        self.assertEqual(value, -102)

    def test_not_available(self):
        f = self.F(bit_length=16)
        _, value, disp = n2k.extract_field(b"\xFF\xFF", f)
        self.assertIsNone(value)
        self.assertEqual(disp, "—")
        fs = self.F(bit_length=8, signed=True)
        self.assertIsNone(n2k.extract_field(b"\x7F", fs)[1])
        # 1-bit flags: 1 is a real value, not NA
        f1 = self.F(bit_length=1)
        self.assertEqual(n2k.extract_field(b"\x01", f1)[1], 1)

    def test_lookup_and_bounds(self):
        f = self.F(bit_offset=4, bit_length=3, lookup={"2": "Apparent"})
        raw, value, disp = n2k.extract_field(b"\x20", f)
        self.assertEqual((raw, disp), (2, "Apparent"))
        self.assertIsNone(n2k.extract_field(b"\x20", self.F(bit_offset=8)))

    def test_undecodable_never_yields_a_number(self):
        # the §2 fail-safe: a field the schema cannot interpret decodes to
        # not-available (never a value from the wrong resolution)
        f = self.F(bit_length=16, resolution=0.1, units="pct",
                   undecodable="depends on another field's bit")
        raw, value, disp = n2k.extract_field(b"\x64\x00\x00", f)
        self.assertEqual(raw, 100)          # raw is still available
        self.assertIsNone(value)            # but no computed number
        self.assertEqual(disp, "n/d")       # distinct from NA "—"


def fp_frames(seq, payload):
    """Build fast-packet frames for an assembled payload."""
    out = [bytes([seq << 5, len(payload)]) + payload[:6]]
    rest, idx = payload[6:], 1
    while rest:
        out.append(bytes([(seq << 5) | idx]) + rest[:7])
        rest, idx = rest[7:], idx + 1
    return [f.ljust(8, b"\xFF") for f in out]


class TestFastPacket(unittest.TestCase):
    def setUp(self):
        self.fp = n2k.FastPacketAssembler()

    def test_multi_frame(self):
        payload = bytes(range(43))
        frames = fp_frames(1, payload)
        for f in frames[:-1]:
            self.assertIsNone(self.fp.feed(("k",), f))
        self.assertEqual(self.fp.feed(("k",), frames[-1]), payload)

    def test_fits_first_frame(self):
        self.assertEqual(self.fp.feed(("k",), fp_frames(0, b"\x01\x02")[0]),
                         b"\x01\x02")

    def test_out_of_order_drops(self):
        frames = fp_frames(2, bytes(range(20)))
        self.fp.feed(("k",), frames[0])
        self.assertIsNone(self.fp.feed(("k",), frames[2]))   # skipped idx 1
        self.assertEqual(self.fp.dropped, 1)
        self.assertIsNone(self.fp.feed(("k",), frames[1]))   # state is gone

    def test_seq_mismatch_drops(self):
        a = fp_frames(1, bytes(range(20)))
        b = fp_frames(3, bytes(range(20)))
        self.fp.feed(("k",), a[0])
        self.assertIsNone(self.fp.feed(("k",), b[1]))
        self.assertEqual(self.fp.dropped, 1)

    def test_restart_rearms(self):
        payload = bytes(range(20))
        frames = fp_frames(1, payload)
        self.fp.feed(("k",), frames[0])
        for f in frames[:-1]:                    # sender restarts cleanly
            self.fp.feed(("k",), f)
        self.assertEqual(self.fp.feed(("k",), frames[-1]), payload)

    def test_per_source_state(self):
        pa, pb = bytes(range(20)), bytes(range(50, 70))
        fa, fb = fp_frames(1, pa), fp_frames(1, pb)
        self.fp.feed(("a",), fa[0])
        self.fp.feed(("b",), fb[0])
        self.assertEqual(self.fp.feed(("b",), fb[1]), None)
        self.assertEqual(self.fp.feed(("a",), fa[1]), None)
        self.assertEqual(self.fp.feed(("a",), fa[2]), pa)
        self.assertEqual(self.fp.feed(("b",), fb[2]), pb)


class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.m = n2k.N2kMonitor(TABLES)

    def wind(self, ts, speed_raw=1234, sid=1):
        data = bytes([sid, speed_raw & 0xFF, speed_raw >> 8, 0, 0, 2, 0xFF,
                      0xFF])
        self.m.ingest(ts, WIND_ID, True, False, data)

    def test_decode_row(self):
        self.wind(1.0)
        rows, unknown, stats = self.m.snapshot(now=1.1)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["pgn"], r["src"], r["name"]),
                         (130306, 0x23, "Wind Data"))
        by_name = {f["name"]: f for f in r["fields"]}
        self.assertAlmostEqual(by_name["Wind Speed"]["value"], 12.34)
        self.assertEqual(by_name["Reference"]["disp"], "Apparent")
        self.assertEqual(stats["total"], 1)
        self.assertEqual(unknown, [])

    def test_unknown_counted_with_sample_id(self):
        cid = 0x09F80305                      # PGN 129027, not in tables
        self.m.ingest(1.0, cid, True, False, b"\x00" * 8)
        self.m.ingest(1.1, cid | 0x02, True, False, b"\x00" * 8)  # src 0x07
        rows, unknown, stats = self.m.snapshot()
        self.assertEqual(rows, [])
        self.assertEqual(len(unknown), 1)
        u = unknown[0]
        self.assertEqual(u["pgn"], 129027)
        self.assertEqual(u["count"], 2)
        self.assertEqual(u["sample_id"], cid)
        self.assertEqual(stats["unknown"], 2)
        # 11-bit frames are counted separately, never decoded
        self.m.ingest(1.2, 0x123, False, False, b"\x00")
        self.assertEqual(self.m.snapshot()[2]["non_n2k"], 1)

    def test_fast_packet_end_to_end(self):
        # GNSS altitude raw 50 → 50*1 + (-100) = -50 m, assembled from 2 frames
        payload = bytes([7, 50, 0]).ljust(9, b"\x00")
        cid = (3 << 26) | (0x1F805 << 8) | 0x10   # PGN 129029 src 0x10
        for f in fp_frames(4, payload):
            self.m.ingest(2.0, cid, True, False, f)
        rows, _, _ = self.m.snapshot()
        self.assertEqual(len(rows), 1)
        alt = {f["name"]: f for f in rows[0]["fields"]}["Altitude"]
        self.assertEqual(alt["value"], -50)

    def test_range_alert_fires_on_transition(self):
        self.m.alerts.set_range(130306, "Wind Speed", None, 20.0)
        self.wind(1.0, speed_raw=1000)    # 10 m/s, in range
        self.wind(1.1, speed_raw=3000)    # 30 m/s, out → fire
        self.wind(1.2, speed_raw=3100)    # still out → no refire
        self.wind(1.3, speed_raw=1000)    # back in
        self.wind(1.4, speed_raw=2500)    # out again → fire
        w = self.m.alerts.ranges[(130306, "Wind Speed")]
        self.assertEqual(w["hits"], 2)
        rows, _, stats = self.m.snapshot(now=1.5)
        self.assertTrue(rows[0]["alert"])
        self.assertEqual(stats["hits"], 2)
        # NA values never alert
        self.m.alerts.set_range(130306, "SID", None, 0)
        self.wind(1.6, sid=0xFF)
        self.assertEqual(self.m.alerts.ranges[(130306, "SID")]["hits"], 0)

    def test_appearance_alert(self):
        self.assertTrue(self.m.alerts.toggle_appearance(130306))
        self.wind(1.0)
        self.wind(1.1)
        self.assertEqual(self.m.alerts.appear[(130306, None)]["hits"], 2)
        self.assertFalse(self.m.alerts.toggle_appearance(130306))

    def test_export_jsonl(self):
        self.wind(1.0)
        self.wind(1.1)
        cid = 0x09F80305
        self.m.ingest(1.2, cid, True, False, b"\x00" * 8)   # unknown: not logged
        with tempfile.TemporaryDirectory() as d:
            n, path = self.m.export(d)
            self.assertEqual(n, 2)
            with open(path) as f:
                recs = [json.loads(l) for l in f]
            self.assertEqual(recs[0]["pgn"], 130306)
            self.assertAlmostEqual(recs[0]["fields"]["Wind Speed"], 12.34)
            n, _ = self.m.export(d, pgn=99999)
            self.assertEqual(n, 0)
            n, _ = self.m.export(d, pgn=130306, src=0x23)
            self.assertEqual(n, 2)


class TestGpsBusDelta(unittest.TestCase):
    """Phase 4 comparison: decoded position PGNs from another source vs
    the local snapshot — offsets in meters, unit-tolerant SOG/COG."""

    SNAP = {"lat": 51.5000, "lon": -0.1200, "sog_mps": 2.0,
            "cog_deg_true": 90.0}

    @staticmethod
    def f(name, value, units=""):
        return {"name": name, "value": value, "disp": "", "units": units}

    def test_position_offset_meters(self):
        # ~0.001° north ≈ 111 m
        fields = [self.f("Latitude", 51.5010, "deg"),
                  self.f("Longitude", -0.1200, "deg")]
        d = n2k.gps_bus_delta(fields, self.SNAP)
        self.assertAlmostEqual(d["dist_m"], 111.3, delta=1.0)
        self.assertEqual(n2k.delta_severity(d), "bad")
        close = [self.f("Latitude", 51.50003, "deg"),
                 self.f("Longitude", -0.12001, "deg")]
        d = n2k.gps_bus_delta(close, self.SNAP)
        self.assertEqual(n2k.delta_severity(d), "ok")

    def test_sog_cog_units_normalized(self):
        fields = [self.f("SOG", 4.0, "knots"),        # ≈ 2.06 m/s
                  self.f("COG", 1.5708, "rad")]       # ≈ 90°
        d = n2k.gps_bus_delta(fields, self.SNAP)
        self.assertAlmostEqual(d["d_sog_mps"], 0.058, places=2)
        self.assertAlmostEqual(d["d_cog_deg"], 0.0, places=1)

    def test_no_overlap_or_no_fix_is_empty(self):
        self.assertEqual(n2k.gps_bus_delta(
            [self.f("Wind Speed", 5.0, "m/s")], self.SNAP), {})
        self.assertEqual(n2k.gps_bus_delta(
            [self.f("Latitude", 51.5, "deg")], None), {})
        # not-available values never compare
        self.assertEqual(n2k.gps_bus_delta(
            [self.f("Latitude", None, "deg"),
             self.f("Longitude", None, "deg")], self.SNAP), {})

    def test_cog_wraps_shortest_way(self):
        snap = {**self.SNAP, "cog_deg_true": 359.0}
        d = n2k.gps_bus_delta([self.f("COG", 0.0349, "rad")], snap)  # ≈ 2°
        self.assertAlmostEqual(d["d_cog_deg"], 3.0, delta=0.2)


class TestNoTxN2k(TestNoTx):
    """Same RX-only enforcement, over the NMEA2K modules."""
    MODULES = ("kilodash/n2k.py", "kilodash/screens/n2k.py")

    def test_screen_docstring_codifies_scope(self):
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "kilodash/screens/n2k.py")) as f:
            doc = ast.get_docstring(ast.parse(f.read()))
        self.assertIn("rx-only", (doc or "").lower())


if __name__ == "__main__":
    unittest.main()
