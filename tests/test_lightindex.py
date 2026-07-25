"""Unit tests for the Scottina Light table index (TABLES.md §5b/§5c) and the
interval_ms / index validation added to tables/validate.py.

Run from the repo root:  python -m unittest discover -s tests
Covers: fast-packet + PDU1 exclusion at export time (only single-frame PDU2
survives); interval_ms present / absent / out-of-range; index and detail
files agreeing on their PGN set, with a desynced pair rejected by the
validator; both export paths (shared writer, the web download, and the Files
Tables → USB push) producing byte-identical artifacts; and per-file write
atomicity (a crash mid-write leaves the previous file, never a partial one).
Stdlib + Flask test client only.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kilodash import tableconv  # noqa: E402
from kilodash.screens import files  # noqa: E402
from tables import lightindex, store, validate  # noqa: E402

# One single-frame PDU2 (eligible), one fast-packet, one PDU1 — the index
# must offer only the first.
TABLE = {"PGNs": [
    {"PGN": 130306, "Name": "Wind Data", "FastPacket": False,
     "TransmissionInterval": 100,
     "Fields": [{"Name": "Wind Speed", "BitOffset": 8, "BitLength": 16,
                 "Resolution": 0.01, "Units": "m/s"},
                {"Name": "Reference", "BitOffset": 40, "BitLength": 3}]},
    {"PGN": 129029, "Name": "GNSS Position", "Type": "Fast",
     "TransmissionInterval": 1000,
     "Fields": [{"Name": "SID", "BitOffset": 0, "BitLength": 8}]},
    {"PGN": 126720, "Name": "Proprietary", "FastPacket": False,
     "Fields": [{"Name": "Data", "BitOffset": 0, "BitLength": 8}]},
]}


class LightCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lightindex-test-")
        self._base = store.BASE
        store.BASE = self.tmp
        store.ensure_dirs()
        _t, _w = validate.validate(TABLE)
        store.install("wind", TABLE, source_doc="wind.pdf",
                      converter_version="1.0", pgn_count=len(_t))

    def tearDown(self):
        store.BASE = self._base
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestExclusion(LightCase):
    def test_only_single_frame_pdu2_indexed(self):
        index_str, details = lightindex.artifacts_for_table("wind")
        index = json.loads(index_str)
        self.assertEqual([e["pgn"] for e in index["pgns"]], [130306])
        # fast-packet (129029) and PDU1 (126720) never reach the index...
        self.assertEqual(set(details), {"pgn-130306.json"})
        # ...and the index/detail PGN sets agree
        self.assertEqual({e["pgn"] for e in index["pgns"]},
                         {json.loads(d)["pgn"] for d in details.values()})

    def test_is_pdu1_classification(self):
        self.assertTrue(validate.is_pdu1(126720))    # PF 0xEF < 240
        self.assertTrue(validate.is_pdu1(59904))     # ISO Request
        self.assertFalse(validate.is_pdu1(130306))   # PF 0xFD, PDU2

    def test_index_shape_and_abbrev_keys(self):
        index = json.loads(lightindex.artifacts_for_table("wind")[0])
        self.assertEqual(index["v"], 1)
        self.assertEqual(index["src"], "wind")
        self.assertTrue(index["sha256"] and index["generated"])
        e = index["pgns"][0]
        self.assertEqual((e["pgn"], e["n"], e["ms"]),
                         (130306, "Wind Data", 100))
        # names only; units present only when the signal has them
        self.assertEqual(e["sig"][0], {"i": 0, "n": "Wind Speed", "u": "m/s"})
        self.assertEqual(e["sig"][1], {"i": 1, "n": "Reference"})

    def test_generated_mirrors_manifest_converted(self):
        index = json.loads(lightindex.artifacts_for_table("wind")[0])
        self.assertEqual(index["generated"],
                         store.read_meta("wind")["converted"])


class TestInterval(unittest.TestCase):
    def _one(self, entry_extra):
        obj = {"PGNs": [{"PGN": 130306, "Name": "X",
                         "Fields": [{"Name": "f", "BitOffset": 0,
                                     "BitLength": 8}], **entry_extra}]}
        tables, warns = validate.validate(obj)
        return tables[130306]["interval_ms"], warns

    def test_present(self):
        ms, warns = self._one({"TransmissionInterval": 250})
        self.assertEqual(ms, 250)
        self.assertEqual(warns, [])
        # our own key is accepted too
        self.assertEqual(self._one({"interval_ms": 500})[0], 500)

    def test_absent_is_unknown(self):
        ms, warns = self._one({})
        self.assertIsNone(ms)
        self.assertEqual(warns, [])

    def test_out_of_range_dropped_with_warning(self):
        for bad in (-5, 0, 999_999_999):
            ms, warns = self._one({"TransmissionInterval": bad})
            self.assertIsNone(ms)
            self.assertTrue(any("interval" in w for w in warns))

    def test_non_integer_dropped(self):
        ms, warns = self._one({"interval_ms": "soon"})
        self.assertIsNone(ms)
        self.assertTrue(any("interval" in w for w in warns))


class TestIndexValidation(LightCase):
    def test_valid_pair_passes(self):
        index_str, details = lightindex.artifacts_for_table("wind")
        index = json.loads(index_str)
        by_pgn = {json.loads(d)["pgn"]: json.loads(d)
                  for d in details.values()}
        self.assertEqual(validate.validate_index(index), {130306})
        validate.check_pair(index, by_pgn)          # does not raise

    def test_pdu1_in_index_rejected_standalone(self):
        index = {"v": 1, "src": "x", "sha256": "d", "generated": "t",
                 "pgns": [{"pgn": 126720, "n": "Prop", "sig": []}]}
        with self.assertRaises(validate.IndexInvalid):
            validate.validate_index(index)

    def test_fast_detail_rejected_by_check_pair(self):
        index = {"v": 1, "src": "x", "sha256": "d", "generated": "t",
                 "pgns": [{"pgn": 130306, "n": "Wind", "sig": []}]}
        details = {130306: {"v": 1, "pgn": 130306, "name": "Wind",
                            "fast": True, "fields": []}}
        with self.assertRaises(validate.IndexInvalid):
            validate.check_pair(index, details)

    def test_desynced_pair_rejected(self):
        index_str, details = lightindex.artifacts_for_table("wind")
        index = json.loads(index_str)
        by_pgn = {json.loads(d)["pgn"]: json.loads(d)
                  for d in details.values()}
        # detail claims a PGN the index does not offer → disagreement
        by_pgn[130310] = {"v": 1, "pgn": 130310, "name": "Ghost",
                          "fast": False, "fields": []}
        with self.assertRaises(validate.IndexInvalid):
            validate.check_pair(index, by_pgn)


class TestBothPathsByteIdentical(LightCase):
    def test_writer_download_and_usb_agree(self):
        # (a) the shared builder
        index_a, details_a = lightindex.artifacts_for_table("wind")

        # (b) the atomic file writer
        wdir = os.path.join(self.tmp, "written")
        names = lightindex.write_table_artifacts(wdir, "wind")
        self.assertIn("wind.index.json", names)
        with open(os.path.join(wdir, "wind.index.json")) as f:
            index_b = f.read()

        # (c) the web app's Installed → index download
        app = tableconv.create_app()
        app.testing = True
        c = app.test_client()
        r = c.get("/tables/wind/index")
        self.assertEqual(r.status_code, 200)
        index_c = r.get_data(as_text=True)
        rd = c.get("/tables/wind/detail/130306")
        self.assertEqual(rd.status_code, 200)
        detail_c = rd.get_data(as_text=True)

        # (d) the Files screen's Tables → USB push
        mount = os.path.join(self.tmp, "stick")
        with mock.patch.object(files, "TABLE_DIR", self.tmp), \
                mock.patch.object(files, "MOUNT", mount), \
                mock.patch.object(files, "_sh", lambda *a, **k: None):
            n, msg = files._export_tables()
        dest = os.path.join(mount, files.DEST_SUB, "tables")
        with open(os.path.join(dest, "wind.index.json")) as f:
            index_d = f.read()
        with open(os.path.join(dest, "pgn-130306.json")) as f:
            detail_d = f.read()

        self.assertEqual(index_a, index_b)
        self.assertEqual(index_a, index_c)
        self.assertEqual(index_a, index_d)
        self.assertEqual(details_a["pgn-130306.json"], detail_c)
        self.assertEqual(details_a["pgn-130306.json"], detail_d)
        self.assertIn("Light index", msg)
        # ineligible PGNs are not silently offered anywhere
        self.assertFalse(os.path.exists(os.path.join(dest, "pgn-129029.json")))
        self.assertFalse(os.path.exists(os.path.join(dest, "pgn-126720.json")))


class TestAtomicity(LightCase):
    def test_crash_leaves_previous_index_not_partial(self):
        wdir = os.path.join(self.tmp, "out")
        lightindex.write_table_artifacts(wdir, "wind")     # good export
        idx = os.path.join(wdir, "wind.index.json")
        with open(idx) as f:
            good = f.read()

        # a crash at the rename step must leave the previous file intact and
        # never a partial one (tmp + os.replace)
        with mock.patch.object(lightindex.os, "replace",
                               side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                lightindex.write_table_artifacts(wdir, "wind")
        with open(idx) as f:
            self.assertEqual(f.read(), good)
        # the only completed index under the real name is the previous one;
        # a partial write only ever exists as an un-renamed .tmp
        self.assertEqual([n for n in os.listdir(wdir)
                          if n.endswith(".index.json")], ["wind.index.json"])


if __name__ == "__main__":
    unittest.main()
