"""Exercises the synthetic table fixtures (tests/fixtures/tables/) against the
Light index export path, and covers the §5c decisions that settled the
detail-file collision:

- detail files are namespaced by store (`<src>.pgn-<num>.json`), so two stores
  defining the same PGN export side by side with NO overwrite;
- each index entry carries the sha256 of its detail file, so a mismatched pair
  is detectable (`validate.detail_sha_ok`);
- the index header's `warn` provenance banner survives export;
- and the two validator holes are closed: Resolution 0 and a single-frame PGN
  packed past bit 64 are both rejected.

Run from the repo root:  python -m unittest discover -s tests
See tests/fixtures/tables/NOTES.md for the settled contract decisions.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tables import lightindex, store, validate  # noqa: E402
from tests.fixtures.tables import generate  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "tables")


def _load(name):
    with open(os.path.join(FIX, name + ".json")) as f:
        return json.load(f)


class TestFixturesTracked(unittest.TestCase):
    """The tracked JSON must equal a fresh generation — never hand-edited."""

    def test_regeneration_is_byte_stable(self):
        for name, build in generate.STORES.items():
            want = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
            with open(os.path.join(FIX, name + ".json")) as f:
                self.assertEqual(f.read(), want,
                                 f"{name}.json drifted — rerun generate.py")

    def test_marker_and_source_doc_present(self):
        for name in generate.STORES:
            self.assertEqual(_load(name)["_synthetic"], generate.SOURCE_DOC)


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="syn-fix-")
        self._base = store.BASE
        store.BASE = self.tmp
        store.ensure_dirs()

    def tearDown(self):
        store.BASE = self._base
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ingest(self, name):
        obj = _load(name)
        tables, warns = validate.validate(obj)
        store.install(name, obj, source_doc=generate.SOURCE_DOC,
                      converter_version="1.0", pgn_count=len(tables))
        return tables, warns


class TestValidateAndIngest(_StoreCase):
    def test_both_stores_validate_and_ingest_verified(self):
        for name in ("synthetic-basic", "synthetic-overlap"):
            self._ingest(name)
        inv = {t["name"]: t for t in store.list_tables()}
        self.assertTrue(inv["synthetic-basic"]["verified"])
        self.assertTrue(inv["synthetic-overlap"]["verified"])
        self.assertEqual(inv["synthetic-basic"]["meta"]["source_doc"],
                         generate.SOURCE_DOC)

    def test_out_of_range_interval_degrades_not_fatal(self):
        tables, warns = self._ingest("synthetic-basic")
        self.assertIsNone(tables[generate.P[6]]["interval_ms"])   # OOR
        self.assertIsNone(tables[generate.P[5]]["interval_ms"])   # absent
        self.assertTrue(any("out of 1.." in w for w in warns), warns)

    def test_basic_index_offers_only_eligible_pgns(self):
        self._ingest("synthetic-basic")
        index_str, details = lightindex.artifacts_for_table("synthetic-basic")
        index = json.loads(index_str)
        offered = {e["pgn"] for e in index["pgns"]}
        self.assertEqual(offered, set(generate.P))                # the 9 PDU2
        for excluded in (generate.P_FAST, generate.P_PDU1):
            self.assertNotIn(excluded, offered)
            self.assertNotIn(
                lightindex.detail_filename("synthetic-basic", excluded),
                details)
        self.assertEqual({int(json.loads(d)["pgn"]) for d in details.values()},
                         set(generate.P))
        self.assertGreater(len(offered), 6)    # 9 vs Light's 6-PGN picker cap

    def test_units_and_lookups_reach_the_detail_files(self):
        self._ingest("synthetic-basic")
        _idx, details = lightindex.artifacts_for_table("synthetic-basic")
        merged = [json.loads(d) for d in details.values()]
        self.assertTrue(any(f["units"] for d in merged for f in d["fields"]))
        self.assertTrue(any(f["lookup"] for d in merged for f in d["fields"]))


class TestNamespacingAndBinding(_StoreCase):
    """The settled §5c: side-by-side export with no overwrite, plus the §5b
    per-detail sha and `warn` header."""

    def setUp(self):
        super().setUp()
        for name in ("synthetic-basic", "synthetic-overlap"):
            self._ingest(name)

    def test_overlapping_stores_export_side_by_side_no_overwrite(self):
        dest = os.path.join(self.tmp, "flat")
        for t in sorted(store.list_tables(), key=lambda t: t["name"]):
            lightindex.write_table_artifacts(dest, t["name"])
        pgn = generate.P_OVERLAP                                   # 0x3F002
        basic_fn = lightindex.detail_filename("synthetic-basic", pgn)
        overlap_fn = lightindex.detail_filename("synthetic-overlap", pgn)
        self.assertNotEqual(basic_fn, overlap_fn)                  # namespaced
        with open(os.path.join(dest, basic_fn)) as f:
            basic = json.load(f)
        with open(os.path.join(dest, overlap_fn)) as f:
            overlap = json.load(f)
        # both survived — each store's own definition, neither clobbered
        self.assertEqual(basic["name"], "SYN Charlie")
        self.assertEqual(len(basic["fields"]), 3)
        self.assertEqual(overlap["name"], "SYN Charlie OVERLAP VARIANT")
        self.assertEqual(len(overlap["fields"]), 2)
        # both index files present and internally consistent
        for src in ("synthetic-basic", "synthetic-overlap"):
            self.assertTrue(os.path.exists(
                os.path.join(dest, lightindex.index_filename(src))))

    def test_per_detail_sha_detects_a_tampered_pair(self):
        index_str, details = lightindex.artifacts_for_table("synthetic-basic")
        index = json.loads(index_str)
        self.assertTrue(all("sha" in e for e in index["pgns"]))   # every entry
        pgn = generate.P[0]
        good = details[lightindex.detail_filename("synthetic-basic", pgn)]
        self.assertTrue(validate.detail_sha_ok(index, pgn, good))
        tampered = good.replace('"pgn"', '"pgn" ')   # 1 byte, still valid JSON
        self.assertNotEqual(tampered, good)
        self.assertFalse(validate.detail_sha_ok(index, pgn, tampered))

    def test_warn_survives_export(self):
        for src in ("synthetic-basic", "synthetic-overlap"):
            index = json.loads(lightindex.artifacts_for_table(src)[0])
            self.assertEqual(index["warn"], generate.SOURCE_DOC)


class TestValidatorFixes(unittest.TestCase):
    """The two confident-wrong-reading holes, closed in the shared validator."""

    def _entry(self, fields, fast=False):
        return {"PGNs": [{"PGN": 0x3F000, "Name": "X", "FastPacket": fast,
                          "Fields": fields}]}

    def test_resolution_zero_is_rejected(self):
        # the sole field is invalid → the file has nothing usable → fatal
        with self.assertRaises(validate.TableInvalid):
            validate.validate(self._entry(
                [{"Name": "z", "BitOffset": 0, "BitLength": 8,
                  "Resolution": 0}]))
        # alongside a good field: bad one skipped with a Resolution warning
        tables, warns = validate.validate(self._entry([
            {"Name": "z", "BitOffset": 0, "BitLength": 8, "Resolution": 0},
            {"Name": "ok", "BitOffset": 8, "BitLength": 8, "Resolution": 0.1}]))
        self.assertEqual([f["name"] for f in tables[0x3F000]["fields"]], ["ok"])
        self.assertTrue(any("Resolution 0" in w for w in warns), warns)

    def test_single_frame_field_past_bit_64_is_rejected(self):
        over = {"Name": "past", "BitOffset": 60, "BitLength": 16}   # ends at 76
        good = {"Name": "ok", "BitOffset": 0, "BitLength": 8}
        tables, warns = validate.validate(self._entry([over, good]))
        self.assertEqual([f["name"] for f in tables[0x3F000]["fields"]], ["ok"])
        self.assertTrue(any("> 64" in w for w in warns), warns)
        # a fast-packet PGN assembles a larger payload → the same field is kept
        tables, _ = validate.validate(self._entry([over, good], fast=True))
        self.assertEqual(len(tables[0x3F000]["fields"]), 2)


if __name__ == "__main__":
    unittest.main()
