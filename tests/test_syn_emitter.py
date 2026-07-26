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
        # manufacturer 2047, category 15, signed 0x7F, wide-flag all-ones all
        # decode NA — the sentinel collisions the shape test exists to surface
        na_seen = set()
        for r in self.golden:
            for name, v in r["fields"].items():
                if v is None:
                    na_seen.add(name)
        for expect_na in ("SYN Manufacturer ID", "SYN Signed Level",
                          "SYN Category", "SYN Mode Flags"):
            self.assertIn(expect_na, na_seen)


class TestConditionalFieldDivergence(unittest.TestCase):
    """Field 11's meaning depends on another field's bit — §2 cannot express
    it. The test documents the divergence rather than hiding it."""

    def test_select_bit_reScales_beyond_the_table(self):
        tables, _ = validate.validate_file(MARINE)
        dec = n2k.Decoder(tables)
        divergent = 0
        for r in _golden():
            if "cond" not in r:
                continue
            rec = dec.feed(r["t"], r["id"], True, bytes.fromhex(r["raw"]))
            got = {f["name"]: f["value"] for f in rec["fields"]}
            c = r["cond"]
            # what the table decodes always equals n2k (the table variant)
            self.assertEqual(_eq(got["SYN Scaled Value"], c["table"]["value"]),
                             True)
            if c["select_bit"] == 1 and c["table"]["value"] not in (None, 0):
                divergent += 1
                # the true (rpm) meaning is 0.25/0.1 = 2.5x the table (pct)
                self.assertAlmostEqual(
                    c["intended"]["value"] / c["table"]["value"], 2.5, places=6)
                self.assertNotEqual(c["intended"]["unit"], c["table"]["unit"])
        self.assertGreater(divergent, 0, "no Select=1 divergence in golden")


def _eq(a, b):
    if a is None or b is None:
        return a is b or a == b
    return abs(a - b) < 1e-6


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
