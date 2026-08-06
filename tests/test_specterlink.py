"""Unit tests for the SPECTER bench-link model (kilodash/specterlink.py).

Run from the repo root:  python -m unittest discover -s tests

Covers the shared 8-byte decode (both directions use one layout), liveness
and the STALE threshold, measured period, sequence continuity (gaps vs
repeats, including 8-bit wrap), the `ip link` health parse, and — the scope
constraint made executable — an AST scan proving the SPECTER model and
screen construct no TX: no send*/write/sendmsg calls, no can-utils TX
invocations, socket recv-only. Stdlib only, no socket is opened.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kilodash import specterlink as SL  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def frame(seq=0, flags=0x11, lo=0x00, hi=0x00, proto=1, session=1, ver=1):
    return bytes([proto, session, seq, flags, lo, hi, 0, ver])


class TestDecode(unittest.TestCase):
    def test_rejects_wrong_length(self):
        self.assertIsNone(SL.decode(b""))
        self.assertIsNone(SL.decode(b"\x01" * 7))
        self.assertIsNone(SL.decode(None))
        self.assertIsNotNone(SL.decode(b"\x01" * 8))

    def test_header_fields(self):
        f = SL.decode(frame(seq=0x2A, proto=1, session=7, ver=3))
        self.assertEqual(f["protocol_version"], 1)
        self.assertEqual(f["session_id"], 7)
        self.assertEqual(f["sequence"], 0x2A)
        self.assertEqual(f["checklist_version"], 3)
        self.assertEqual(f["reserved"], 0)

    def test_flag_bits(self):
        # bit0 preflight_incomplete, bits1-3 active_group, bit4 session_active
        f = SL.decode(frame(flags=0x13))       # 0b00010011
        self.assertEqual(f["preflight_incomplete"], 1)
        self.assertEqual(f["active_group"], 1)
        self.assertEqual(f["session_active"], 1)
        f = SL.decode(frame(flags=0x0E))       # 0b00001110
        self.assertEqual(f["preflight_incomplete"], 0)
        self.assertEqual(f["active_group"], 7)
        self.assertEqual(f["session_active"], 0)

    def test_group_states_span_both_bytes(self):
        """g1..g4 in byte 4, g5..g7 in byte 5, two bits each, little-endian."""
        f = SL.decode(frame(lo=0b11100100, hi=0b00011011))
        #                     g4 g3 g2 g1        g7 g6 g5
        self.assertEqual(f["states"],
                         [SL.PENDING, SL.ACTIVE, SL.GOOD, SL.FAULT,
                          SL.FAULT, SL.GOOD, SL.ACTIVE])

    def test_the_bench_vector(self):
        """The exact bytes the bench put on the wire for S=ACTIVE, E1=GOOD,
        T=FAULT — pinned so a layout change cannot pass silently."""
        f = SL.decode(bytes([0x01, 0x01, 0x34, 0x13, 0x21, 0x03, 0x00, 0x01]))
        states = dict(zip(SL.GROUP_LETTERS, f["states"]))
        self.assertEqual(states["S"], SL.ACTIVE)
        self.assertEqual(states["E1"], SL.GOOD)
        self.assertEqual(states["T"], SL.FAULT)
        self.assertEqual(states["P"], SL.PENDING)
        self.assertEqual(f["active_group"], 1)
        self.assertEqual(f["session_active"], 1)

    def test_group_letters_are_the_specified_seven(self):
        self.assertEqual(SL.GROUP_LETTERS,
                         ("S", "P", "E1", "C", "T", "E2", "R"))


class TestLinkState(unittest.TestCase):
    def setUp(self):
        self.link = SL.LinkState(SL.DISPLAY_CAN_ID, "DISPLAY")

    def test_never_seen_is_stale_not_alive(self):
        s = self.link.snapshot(now=100.0)
        self.assertFalse(s["alive"])
        self.assertIsNone(s["age"])
        self.assertIsNone(s["states"])
        self.assertEqual(s["frames"], 0)

    def test_alive_within_the_threshold_and_stale_after(self):
        self.link.ingest(100.0, frame(seq=1))
        self.assertTrue(self.link.snapshot(now=100.0 + SL.STALE_S - 0.01)["alive"])
        self.assertFalse(self.link.snapshot(now=100.0 + SL.STALE_S)["alive"])

    def test_period_is_measured_not_assumed(self):
        for i in range(4):
            self.link.ingest(100.0 + i * 0.5, frame(seq=i))
        s = self.link.snapshot(now=101.5)
        self.assertAlmostEqual(s["period_ms"], 500.0, places=3)
        self.assertAlmostEqual(s["period_avg_ms"], 500.0, places=3)
        self.assertEqual(s["frames"], 4)

    def test_continuous_sequence_has_no_gaps(self):
        for i in range(10):
            self.link.ingest(100.0 + i * 0.5, frame(seq=i))
        s = self.link.snapshot(now=105.0)
        self.assertEqual((s["gaps"], s["repeats"]), (0, 0))

    def test_gap_and_repeat_are_counted_separately(self):
        self.link.ingest(100.0, frame(seq=1))
        self.link.ingest(100.5, frame(seq=3))      # gap: 2 was missed
        self.link.ingest(101.0, frame(seq=3))      # repeat: same sequence
        s = self.link.snapshot(now=101.0)
        self.assertEqual(s["gaps"], 1)
        self.assertEqual(s["repeats"], 1)

    def test_wrap_is_continuous_not_a_gap(self):
        """255 -> 0 is the normal 8-bit roll, not a lost frame."""
        self.link.ingest(100.0, frame(seq=255))
        self.link.ingest(100.5, frame(seq=0))
        self.assertEqual(self.link.snapshot(now=100.5)["gaps"], 0)

    def test_bad_length_counted_and_does_not_disturb_continuity(self):
        self.link.ingest(100.0, frame(seq=1))
        self.link.ingest(100.5, b"\x01\x02\x03")
        self.link.ingest(101.0, frame(seq=2))
        s = self.link.snapshot(now=101.0)
        self.assertEqual(s["bad_dlc"], 1)
        self.assertEqual(s["frames"], 2)
        self.assertEqual(s["gaps"], 0)

    def test_snapshot_exposes_decoded_states(self):
        self.link.ingest(100.0, frame(seq=1, lo=0x21, hi=0x03))
        s = self.link.snapshot(now=100.1)
        self.assertEqual(s["states"][0], SL.ACTIVE)
        self.assertEqual(s["states"][4], SL.FAULT)
        self.assertEqual(s["data"],
                         bytes([1, 1, 1, 0x11, 0x21, 0x03, 0, 1]))


class TestIds(unittest.TestCase):
    def test_the_two_ids_are_the_specified_pair(self):
        self.assertEqual(SL.DISPLAY_CAN_ID, 0x240220)
        self.assertEqual(SL.NODE_CAN_ID, 0x248021)

    def test_filter_mask_omits_the_error_flag(self):
        """A filter carrying CAN_ERR_FLAG is moved to the kernel's error-frame
        list, where it never matches a data frame. The bench lost an
        afternoon to that; this keeps it lost only once."""
        import struct
        reader = SL.SpecterReader.__new__(SL.SpecterReader)   # no socket
        vals = struct.unpack("=IIII", reader._filter())
        for mask in (vals[1], vals[3]):
            self.assertEqual(mask & 0x20000000, 0,
                             "CAN_ERR_FLAG must not appear in the mask")
        self.assertEqual(vals[0] & SL.CAN_EFF_MASK, SL.DISPLAY_CAN_ID)
        self.assertEqual(vals[2] & SL.CAN_EFF_MASK, SL.NODE_CAN_ID)


class TestLinkHealthParse(unittest.TestCase):
    SAMPLE = (
        "14: can0: <NOARP,UP,LOWER_UP> mtu 16 qdisc pfifo_fast state UP mode "
        "DEFAULT group default qlen 10\n"
        "    link/can  promiscuity 0 allmulti 0 minmtu 0 maxmtu 0 \n"
        "    can state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 0 \n"
        "\t  bitrate 250000 sample-point 0.875\n"
        "\t  re-started bus-errors arbit-lost error-warn error-pass bus-off\n"
        "\t  0          1          2          3          4          5      "
        "   numtxqueues 1 numrxqueues 1 gso_max_size 65536\n"
        "    RX:  bytes packets errors dropped  missed   mcast           \n"
        "          8544    1068      0       1       0       0 \n"
        "    TX:  bytes packets errors dropped carrier collsns           \n"
        "          1088     136      0       1       0       0 \n")

    def test_parses_the_real_shape(self):
        info = SL.parse_can_link(self.SAMPLE)
        self.assertTrue(info["present"])
        self.assertEqual(info["if_state"], "UP")
        self.assertEqual(info["can_state"], "ERROR-ACTIVE")
        self.assertEqual((info["berr_tx"], info["berr_rx"]), (0, 0))
        self.assertEqual(info["bitrate"], 250000)
        self.assertEqual(info["restarts"], 0)
        self.assertEqual(info["bus_errors"], 1)
        self.assertEqual(info["bus_off"], 5)
        self.assertEqual(info["rx"][1], 1068)      # packets
        self.assertEqual(info["tx"][1], 136)
        self.assertEqual(info["rx"][3], 1)         # dropped

    def test_down_interface_reads_as_stopped(self):
        text = ("14: can0: <NOARP> mtu 16 qdisc noop state DOWN mode DEFAULT\n"
                "    link/can  promiscuity 0\n"
                "    can state STOPPED (berr-counter tx 0 rx 0) restart-ms 0\n")
        info = SL.parse_can_link(text)
        self.assertEqual(info["if_state"], "DOWN")
        self.assertEqual(info["can_state"], "STOPPED")

    def test_absent_interface_is_a_state_not_a_crash(self):
        info = SL.parse_can_link("")
        self.assertFalse(info["present"])
        self.assertIsNone(info["can_state"])


class TestNoTxSurface(unittest.TestCase):
    """The scope constraint as an independent reject pass (the tree-wide
    scan in tests/test_txscan.py is the positive allow-list). This screen
    watches BOTH directions of the link, which is exactly the situation
    where someone would be tempted to also publish one — so it is pinned
    here, next to the code that would do it."""

    MODULES = ("kilodash/specterlink.py", "kilodash/screens/specter.py")
    SEND_ATTRS = {"send", "sendall", "sendto", "sendmsg", "sendfile"}
    TX_PROGS = {"cansend", "cangen", "canplayer", "canfdtest"}

    def _tree(self, rel):
        with open(os.path.join(ROOT, rel)) as f:
            return ast.parse(f.read(), rel)

    def test_no_send_shaped_calls(self):
        for rel in self.MODULES:
            with self.subTest(module=rel):
                for call in (n for n in ast.walk(self._tree(rel))
                             if isinstance(n, ast.Call)):
                    fn = call.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else \
                        fn.id if isinstance(fn, ast.Name) else ""
                    self.assertNotIn(name, self.SEND_ATTRS,
                                     f"{rel}:{call.lineno} transmits")

    def test_no_os_write_backdoor(self):
        for rel in self.MODULES:
            with self.subTest(module=rel):
                for call in (n for n in ast.walk(self._tree(rel))
                             if isinstance(n, ast.Call)):
                    fn = call.func
                    if isinstance(fn, ast.Attribute) and fn.attr == "write" \
                            and isinstance(fn.value, ast.Name) \
                            and fn.value.id == "os":
                        self.fail(f"{rel}:{call.lineno} os.write() back door")

    def test_no_can_utils_tx_tools(self):
        for rel in self.MODULES:
            with self.subTest(module=rel):
                for node in ast.walk(self._tree(rel)):
                    if isinstance(node, ast.Constant) \
                            and isinstance(node.value, str):
                        self.assertNotIn(node.value, self.TX_PROGS, rel)

    def test_screen_declares_no_buttons_and_builds_none(self):
        """No TX surface means no control surface. The web mirror may press
        only what a screen declares, so inheriting the base's empty list is
        the floor — and the panel must offer no Button either, or the two
        surfaces would disagree about what this tile can do.

        Checked by parsing, not importing: this module stays stdlib-only, so
        it runs on a build host with no PIL and no framebuffer."""
        tree = self._tree("kilodash/screens/specter.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                self.assertNotEqual(node.name, "model_buttons",
                                    "SPECTER must inherit the empty button "
                                    "list, never declare one")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "Button",
                                    "SPECTER must draw no control surface")


if __name__ == "__main__":
    unittest.main()
