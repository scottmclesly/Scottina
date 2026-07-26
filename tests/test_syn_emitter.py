"""Golden-vector tests for the synthetic bench kit (Phase 3).

The ground-truth emitter (tools/bench/syn-emitter.py) and Prime's decoder
(kilodash/n2k.py) are two INDEPENDENT implementations of the §2 decode. This
is the first end-to-end numeric check the decoder has had: decode every golden
frame and assert the values equal the emitter's ground truth exactly, incl.
the not-available sentinels and the conditional-field divergence §2 cannot
express.

Also enforces the safety envelope: the emitter constructs only reserved
data-page-3 ids, and NO Scottina package (kilodash/) constructs these frames
or imports the emitter — bench equipment stays bench equipment.

Run from the repo root:  python -m unittest discover -s tests
"""

import importlib.util
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tables import validate  # noqa: E402
from kilodash import n2k  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMITTER = os.path.join(REPO, "tools/bench/syn-emitter.py")
GOLDEN = os.path.join(REPO, "tests/fixtures/golden/syn-marine.jsonl")
MARINE = os.path.join(REPO, "tests/fixtures/tables/synthetic-marine.json")

# golden was generated with these args — keep in lockstep with the file
GEN = dict(sources=2, duration=0.6, seed=1)


def _load_emitter():
    spec = importlib.util.spec_from_file_location("syn_emitter", EMITTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _golden():
    with open(GOLDEN) as f:
        return [json.loads(line) for line in f]


class TestGroundTruthDeterministic(unittest.TestCase):
    def test_regeneration_matches_committed_golden(self):
        em = _load_emitter()
        tables = em.load_table(MARINE)
        regen = [json.dumps({"t": r["t"], **{k: v for k, v in r.items()
                                             if k != "t"}})
                 for r in _regen(em, tables)]
        with open(GOLDEN) as f:
            committed = [line.rstrip("\n") for line in f]
        self.assertEqual(regen, committed,
                         "golden drifted — rerun syn-emitter.py --dry-run")


def _regen(em, tables):
    return list(em.ground_truth(tables, GEN["sources"], GEN["duration"],
                                GEN["seed"]))


class TestPrimeDecodeMatchesGroundTruth(unittest.TestCase):
    """The numeric cross-check: n2k.py decode == emitter ground truth."""

    def setUp(self):
        self.tables, _ = validate.validate_file(MARINE)
        self.dec = n2k.Decoder(self.tables)
        self.golden = _golden()

    def test_every_field_value_matches(self):
        checked = 0
        for r in self.golden:
            rec = self.dec.feed(r["t"], r["id"], True, bytes.fromhex(r["raw"]))
            self.assertIsNotNone(rec, hex(r["pgn"]))
            got = {f["name"]: f["value"] for f in rec["fields"]}
            for name, exp in r["fields"].items():
                checked += 1
                if exp is None:
                    self.assertIsNone(got.get(name),
                                      f"{r['pgn']:#x} {name} expected NA")
                else:
                    self.assertIsNotNone(got.get(name), f"{name} decoded NA")
                    self.assertAlmostEqual(got[name], exp, places=6,
                                           msg=f"{r['pgn']:#x} {name}")
        self.assertGreater(checked, 500)      # the run really is exercised

    def test_not_available_sentinels_decode_to_none(self):
        # signed 0x7F and a wide flag's all-ones both decode NA — the all-ones
        # sentinel collisions the shape test exists to surface
        na_seen = set()
        for r in self.golden:
            for name, v in r["fields"].items():
                if v is None:
                    na_seen.add(name)
        for expect_na in ("SYN Signed Level", "SYN Mode Flags",
                          "SYN Status Flags"):
            self.assertIn(expect_na, na_seen)

    def test_manufacturer_2046_decodes_as_a_real_value(self):
        # 2046 (0x7FE) is unassigned but NOT all-ones, so it is a real value —
        # the correction from 2047, which collided with the NA sentinel
        for r in self.golden:
            if "SYN Manufacturer ID" in r["fields"]:
                self.assertEqual(r["fields"]["SYN Manufacturer ID"], 2046)
                rec = self.dec.feed(r["t"], r["id"], True,
                                    bytes.fromhex(r["raw"]))
                mi = {f["name"]: f for f in rec["fields"]}["SYN Manufacturer ID"]
                self.assertEqual(mi["value"], 2046)
                return
        self.fail("no Manufacturer ID field in golden")

    def test_lookup_cannot_claim_all_ones(self):
        # a lookup label at a field's all-ones value is shadowed by the NA
        # sentinel — correct behaviour, documented so no one adds one
        f = {"name": "x", "bit_offset": 0, "bit_length": 4, "resolution": 1,
             "offset": 0, "signed": False, "units": "",
             "lookup": {"15": "SHOULD-NOT-RENDER"}, "undecodable": None}
        raw, value, disp = n2k.extract_field((0x0F).to_bytes(1, "little"), f)
        self.assertEqual((raw, value, disp), (15, None, "—"))   # NA, not label


class TestConditionalFieldFailsafe(unittest.TestCase):
    """Field 11's meaning depends on another field's bit — §2 cannot express
    it. The fail-safe: n2k must render it not-available with a reason, NEVER a
    confident wrong number. (The §2 proposal to actually decode it is in
    TABLES.md.)"""

    def test_conditional_field_renders_undecodable_not_a_number(self):
        tables, _ = validate.validate_file(MARINE)
        dec = n2k.Decoder(tables)
        checked = 0
        for r in _golden():
            if "cond" not in r:
                continue
            checked += 1
            self.assertIsNone(r["fields"]["SYN Scaled Value"])   # ground truth
            rec = dec.feed(r["t"], r["id"], True, bytes.fromhex(r["raw"]))
            sv = {f["name"]: f for f in rec["fields"]}["SYN Scaled Value"]
            self.assertIsNone(sv["value"])          # never a number
            self.assertEqual(sv["disp"], "n/d")     # distinct from NA "—"
            self.assertIn("condition", sv["undecodable"].lower())  # a reason
            self.assertEqual(sv["units"], "")       # no misleading unit
        self.assertGreater(checked, 0)

    def test_golden_documents_both_intended_meanings(self):
        # documentation only (not an n2k assertion): a conditional-aware schema
        # would decode 0.1 SYN-pct (bit0) or 0.25 SYN-rpm (bit1) — 2.5x apart
        seen = 0
        for r in _golden():
            c = r.get("cond")
            if not c or c["if_bit0"]["value"] in (None, 0):
                continue
            seen += 1
            self.assertAlmostEqual(
                c["if_bit1"]["value"] / c["if_bit0"]["value"], 2.5, places=6)
            self.assertNotEqual(c["if_bit1"]["unit"], c["if_bit0"]["unit"])
        self.assertGreater(seen, 0)


class TestSafetyEnvelope(unittest.TestCase):
    def test_every_golden_id_is_reserved_data_page_3(self):
        for r in _golden():
            self.assertEqual(r["pgn"] >> 16, 3, hex(r["pgn"]))

    def test_arb_id_refuses_non_dp3(self):
        em = _load_emitter()
        with self.assertRaises(AssertionError):
            em.arb_id(0x1F800, 0)          # a real (DP0) PGN — must be refused
        self.assertEqual(em.arb_id(0x3F100, 5) >> 8 & 0x3FFFF, 0x3F100)

    def test_emitter_refuses_tx_without_confirmation(self):
        em = _load_emitter()
        rc = em.main(["--table", MARINE, "--interface", "vcan-none"])
        self.assertEqual(rc, 2)            # refused, never opened a socket

    def test_no_scottina_package_constructs_these_frames(self):
        # the emitter lives under tools/bench (not a package) and nothing in
        # the Scottina package references it — bench gear stays bench gear
        self.assertFalse(os.path.exists(os.path.join(REPO, "tools/__init__.py")))
        self.assertFalse(
            os.path.exists(os.path.join(REPO, "tools/bench/__init__.py")))
        pkg = os.path.join(REPO, "kilodash")
        hits = []
        for root, _dirs, files in os.walk(pkg):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(root, fn)) as f:
                    text = f.read()
                if "syn-emitter" in text or "syn_emitter" in text \
                        or "tools/bench" in text or "tools.bench" in text:
                    hits.append(os.path.join(root, fn))
        self.assertEqual(hits, [], f"Scottina refs the emitter: {hits}")


if __name__ == "__main__":
    unittest.main()
