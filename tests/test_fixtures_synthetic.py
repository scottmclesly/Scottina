"""Exercises the synthetic table fixtures (tests/fixtures/tables/) against the
Light index export path, and PINS the §5c detail-file collision behaviour so
it cannot change silently before Light's picker is written.

Run from the repo root:  python -m unittest discover -s tests
See tests/fixtures/tables/NOTES.md for the collision finding and the contract
question it raises.
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


class TestValidateAndIngest(unittest.TestCase):
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

    def test_both_stores_validate_and_ingest_verified(self):
        for name in ("synthetic-basic", "synthetic-overlap"):
            self._ingest(name)
        inv = {t["name"]: t for t in store.list_tables()}
        self.assertTrue(inv["synthetic-basic"]["verified"])
        self.assertTrue(inv["synthetic-overlap"]["verified"])
        # the warning source_doc landed in the manifest (the one authoritative
        # place — the table file cannot carry it; see NOTES.md)
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
        # fast-packet + PDU1 absent from BOTH index and detail files
        self.assertNotIn(generate.P_FAST, offered)
        self.assertNotIn(generate.P_PDU1, offered)
        self.assertNotIn(lightindex.detail_filename(generate.P_FAST), details)
        self.assertNotIn(lightindex.detail_filename(generate.P_PDU1), details)
        self.assertEqual({int(json.loads(d)["pgn"]) for d in details.values()},
                         set(generate.P))
        # 9 offered vs Light's 6-PGN cap → the selection pressure is real
        self.assertGreater(len(offered), 6)

    def test_units_and_lookups_reach_the_detail_files(self):
        self._ingest("synthetic-basic")
        _idx, details = lightindex.artifacts_for_table("synthetic-basic")
        merged = [json.loads(d) for d in details.values()]
        has_units = any(f["units"] for d in merged for f in d["fields"])
        has_lookup = any(f["lookup"] for d in merged for f in d["fields"])
        self.assertTrue(has_units)
        self.assertTrue(has_lookup)


class TestDetailCollision(unittest.TestCase):
    """PIN the §5c collision: two verified stores defining PGN 258050, exported
    into one flat dir, collide on pgn-258050.json. Current behaviour is a
    SILENT overwrite, last store by sorted name winning — no error, no warning.
    This test locks that in so a future change to it is a deliberate, reviewed
    contract decision (see NOTES.md)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="syn-collide-")
        self._base = store.BASE
        store.BASE = self.tmp
        store.ensure_dirs()
        for name in ("synthetic-basic", "synthetic-overlap"):
            obj = _load(name)
            tables, _ = validate.validate(obj)
            store.install(name, obj, source_doc=generate.SOURCE_DOC,
                          converter_version="1.0", pgn_count=len(tables))

    def tearDown(self):
        store.BASE = self._base
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_flat_export_silently_overwrites_last_sorted_wins(self):
        dest = os.path.join(self.tmp, "flat")
        order = []
        for t in sorted(store.list_tables(), key=lambda t: t["name"]):
            if t["verified"]:
                lightindex.write_table_artifacts(dest, t["name"])
                order.append(t["name"])
        self.assertEqual(order, ["synthetic-basic", "synthetic-overlap"])

        detail_fn = lightindex.detail_filename(generate.P_OVERLAP)  # 258050
        with open(os.path.join(dest, detail_fn)) as f:
            on_disk = json.load(f)
        # last exporter (synthetic-overlap, sorts after basic) won, silently
        self.assertEqual(on_disk["name"], "SYN Charlie OVERLAP VARIANT")
        self.assertEqual(len(on_disk["fields"]), 2)

        # ...yet BOTH indexes still reference 258050, and basic's index now
        # points at a detail describing overlap's different definition
        with open(os.path.join(dest, "synthetic-basic.index.json")) as f:
            basic_idx = json.load(f)
        basic_entry = next(e for e in basic_idx["pgns"]
                           if e["pgn"] == generate.P_OVERLAP)
        self.assertEqual(basic_entry["n"], "SYN Charlie")      # index claim
        self.assertEqual(len(basic_entry["sig"]), 3)           # 3 signals
        self.assertNotEqual(len(basic_entry["sig"]),
                            len(on_disk["fields"]))            # detail differs
        # the collision is invisible — no marker, no error was raised above


if __name__ == "__main__":
    unittest.main()
