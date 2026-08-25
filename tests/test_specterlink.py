"""Unit tests for the SPECTER bench-link model (kilodash/specterlink.py).

Run from the repo root:  python -m unittest discover -s tests

Covers the 8-byte decode of BOTH layouts (the two ids no longer share
one), the seven-group rollup of the 13 wire steps, liveness
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


def display_frame(seq=0, event_seq=0, event_type=0, step=0xFF, state=0):
    """Build one display frame, 0x240220. Byte 0 is the liveness counter."""
    return bytes([seq, event_seq & 0x0F, event_type, step, state, 0, 0, 0])


def node_frame(steps=None, veto=1, operator_input=0, mode=0, echo=0,
               shore=None):
    """Build one status frame, 0x248021.

    `steps` is a dict of step index to state. Any step left out is PENDING.
    `shore` sets slot 13, which is the shore link and is not a step.
    """
    packed = bytearray(8)
    packed[0] = ((veto & 0x01)
                 | ((operator_input & 0x01) << 1)
                 | ((mode & 0x03) << 2)
                 | ((echo & 0x0F) << 4))
    values = dict(steps or {})
    if shore is not None:
        values[SL.SHORE_LINK_SLOT] = shore
    for index, value in values.items():
        packed[1 + (index >> 2)] |= (value & 0x03) << ((index & 3) * 2)
    return bytes(packed)


def all_steps(state):
    """Every step in use at one state."""
    return {index: state for index in range(SL.STEPS_IN_USE)}


class TestDecode(unittest.TestCase):
    def test_rejects_wrong_length(self):
        for can_id in (SL.DISPLAY_CAN_ID, SL.NODE_CAN_ID):
            self.assertIsNone(SL.decode(b"", can_id))
            self.assertIsNone(SL.decode(b"\x01" * 7, can_id))
            self.assertIsNone(SL.decode(None, can_id))
            self.assertIsNotNone(SL.decode(b"\x01" * 8, can_id))

    def test_an_unknown_id_decodes_to_nothing(self):
        self.assertIsNone(SL.decode(b"\x00" * 8, 0x123456))

    def test_display_frame_fields(self):
        f = SL.decode(display_frame(seq=0x2A, event_seq=3, event_type=2,
                                    step=7, state=2),
                      SL.DISPLAY_CAN_ID)
        self.assertEqual(f["liveness_counter"], 0x2A)
        self.assertEqual(f["sequence"], 0x2A)
        self.assertEqual(f["event_sequence"], 3)
        self.assertEqual(f["event_type"], 2)
        self.assertEqual(f["step_index"], 7)
        self.assertEqual(f["display_state"], 2)

    def test_the_display_frame_carries_no_step_states(self):
        f = SL.decode(display_frame(seq=1), SL.DISPLAY_CAN_ID)
        self.assertIsNone(f["states"])

    def test_status_flag_bits(self):
        f = SL.decode(node_frame(veto=1, operator_input=1, mode=2, echo=5),
                      SL.NODE_CAN_ID)
        self.assertEqual(f["veto"], 1)
        self.assertEqual(f["operator_input_requested"], 1)
        self.assertEqual(f["vessel_mode"], 2)
        self.assertEqual(f["event_echo"], 5)

    def test_the_status_frame_carries_no_counter(self):
        """It is a snapshot. A lost frame is corrected by the next one."""
        f = SL.decode(node_frame(), SL.NODE_CAN_ID)
        self.assertIsNone(f["sequence"])

    def test_step_states_span_the_packed_bytes(self):
        f = SL.decode(node_frame(steps={0: SL.ACTIVE, 4: SL.GOOD,
                                        11: SL.FAULT, 12: SL.GOOD}),
                      SL.NODE_CAN_ID)
        self.assertEqual(f["steps"][0], SL.ACTIVE)
        self.assertEqual(f["steps"][4], SL.GOOD)
        self.assertEqual(f["steps"][11], SL.FAULT)
        self.assertEqual(f["steps"][12], SL.GOOD)
        self.assertEqual(f["steps"][1], SL.PENDING)
        self.assertEqual(len(f["steps"]), SL.STEPS_IN_USE)

    def test_the_seven_groups_partition_the_thirteen_steps(self):
        covered = [step for group in SL.GROUP_STEPS for step in group]
        self.assertEqual(sorted(covered), list(range(SL.STEPS_IN_USE)))
        self.assertEqual(len(covered), len(set(covered)))

    def test_a_group_is_good_only_when_every_step_is_good(self):
        # Group 1 covers steps 1, 2 and 3.
        f = SL.decode(node_frame(steps={1: SL.GOOD, 2: SL.GOOD, 3: SL.GOOD}),
                      SL.NODE_CAN_ID)
        self.assertEqual(f["states"][0], SL.GOOD)
        f = SL.decode(node_frame(steps={1: SL.GOOD, 2: SL.GOOD}),
                      SL.NODE_CAN_ID)
        self.assertNotEqual(f["states"][0], SL.GOOD)

    def test_any_fault_under_a_group_makes_the_group_fault(self):
        f = SL.decode(node_frame(steps={1: SL.GOOD, 2: SL.FAULT,
                                        3: SL.GOOD}),
                      SL.NODE_CAN_ID)
        self.assertEqual(f["states"][0], SL.FAULT)

    def test_otherwise_a_group_shows_its_least_advanced_step(self):
        # Group 3 covers steps 0, 6 and 12.
        f = SL.decode(node_frame(steps={0: SL.ACTIVE, 6: SL.GOOD,
                                        12: SL.GOOD}),
                      SL.NODE_CAN_ID)
        self.assertEqual(f["states"][2], SL.ACTIVE)
        f = SL.decode(node_frame(steps={0: SL.PENDING, 6: SL.ACTIVE,
                                        12: SL.GOOD}),
                      SL.NODE_CAN_ID)
        self.assertEqual(f["states"][2], SL.PENDING)

    def test_every_group_is_good_when_every_step_is_good(self):
        f = SL.decode(node_frame(steps=all_steps(SL.GOOD), veto=0),
                      SL.NODE_CAN_ID)
        self.assertEqual(f["states"], [SL.GOOD] * 7)

    def test_the_shore_link_is_slot_thirteen_and_is_not_a_step(self):
        """Slot 13 is the shore link. It is never a checklist step."""
        f = SL.decode(node_frame(steps=all_steps(SL.GOOD), shore=SL.PENDING),
                      SL.NODE_CAN_ID)
        self.assertEqual(f["shore_link"], SL.PENDING)
        # The shore link is absent from the steps and from the rollup.
        self.assertEqual(len(f["steps"]), SL.STEPS_IN_USE)
        self.assertEqual(f["states"], [SL.GOOD] * 7)

    def test_the_bench_vector(self):
        """One real frame from the rig: step 0 ACTIVE, the rest PENDING."""
        f = SL.decode(node_frame(steps={0: SL.ACTIVE}), SL.NODE_CAN_ID)
        self.assertEqual(f["steps"][0], SL.ACTIVE)
        self.assertEqual(f["veto"], 1)
        states = dict(zip(SL.GROUP_LETTERS, f["states"]))
        # E1 covers steps 0, 6 and 12. Step 0 is ACTIVE but 6 and 12 have
        # not run, so the group shows its LEAST advanced step, PENDING.
        # A group that read ACTIVE here would overstate the work done.
        self.assertEqual(states["E1"], SL.PENDING)
        self.assertEqual(states["S"], SL.PENDING)

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
        self.link.ingest(100.0, display_frame(seq=1))
        self.assertTrue(self.link.snapshot(now=100.0 + SL.STALE_S - 0.01)["alive"])
        self.assertFalse(self.link.snapshot(now=100.0 + SL.STALE_S)["alive"])

    def test_period_is_measured_not_assumed(self):
        for i in range(4):
            self.link.ingest(100.0 + i * 0.5, display_frame(seq=i))
        s = self.link.snapshot(now=101.5)
        self.assertAlmostEqual(s["period_ms"], 500.0, places=3)
        self.assertAlmostEqual(s["period_avg_ms"], 500.0, places=3)
        self.assertEqual(s["frames"], 4)

    def test_continuous_sequence_has_no_gaps(self):
        for i in range(10):
            self.link.ingest(100.0 + i * 0.5, display_frame(seq=i))
        s = self.link.snapshot(now=105.0)
        self.assertEqual((s["gaps"], s["repeats"]), (0, 0))

    def test_gap_and_repeat_are_counted_separately(self):
        self.link.ingest(100.0, display_frame(seq=1))
        self.link.ingest(100.5, display_frame(seq=3))   # gap: 2 missed
        self.link.ingest(101.0, display_frame(seq=3))   # repeat
        s = self.link.snapshot(now=101.0)
        self.assertEqual(s["gaps"], 1)
        self.assertEqual(s["repeats"], 1)

    def test_wrap_is_continuous_not_a_gap(self):
        """255 -> 0 is the normal 8-bit roll, not a lost frame."""
        self.link.ingest(100.0, display_frame(seq=255))
        self.link.ingest(100.5, display_frame(seq=0))
        self.assertEqual(self.link.snapshot(now=100.5)["gaps"], 0)

    def test_bad_length_counted_and_does_not_disturb_continuity(self):
        self.link.ingest(100.0, display_frame(seq=1))
        self.link.ingest(100.5, b"\x01\x02\x03")
        self.link.ingest(101.0, display_frame(seq=2))
        s = self.link.snapshot(now=101.0)
        self.assertEqual(s["bad_dlc"], 1)
        self.assertEqual(s["frames"], 2)
        self.assertEqual(s["gaps"], 0)

    def test_the_display_link_exposes_no_group_states(self):
        """Only the status frame carries steps. The screen handles None."""
        self.link.ingest(100.0, display_frame(seq=1))
        self.assertIsNone(self.link.snapshot(now=100.1)["states"])


class TestNodeLinkState(unittest.TestCase):
    def setUp(self):
        self.link = SL.LinkState(SL.NODE_CAN_ID, "NODE")

    def test_snapshot_exposes_the_group_rollup(self):
        data = node_frame(steps={1: SL.GOOD, 2: SL.GOOD, 3: SL.GOOD,
                                 9: SL.FAULT})
        self.link.ingest(100.0, data)
        s = self.link.snapshot(now=100.1)
        self.assertEqual(s["states"][0], SL.GOOD)    # S covers 1,2,3
        self.assertEqual(s["states"][4], SL.FAULT)   # T covers 9
        self.assertEqual(s["data"], data)

    def test_the_status_frame_counts_no_gap_and_no_repeat(self):
        """It carries no counter, so continuity cannot be judged on it."""
        for i in range(6):
            self.link.ingest(100.0 + i * 0.2, node_frame(steps={0: SL.ACTIVE}))
        s = self.link.snapshot(now=101.2)
        self.assertEqual((s["gaps"], s["repeats"]), (0, 0))
        self.assertIsNone(s["sequence"])
        self.assertEqual(s["frames"], 6)

    def test_the_measured_period_is_two_hundred_milliseconds(self):
        for i in range(5):
            self.link.ingest(100.0 + i * 0.2, node_frame())
        s = self.link.snapshot(now=100.8)
        self.assertAlmostEqual(s["period_avg_ms"], 200.0, places=3)


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

    def test_screen_declares_no_web_pressable_buttons(self):
        """The node control is physical-touch only.

        model_buttons() is the web mirror's AUTHORISATION surface, not a
        rendering hint: the box refuses a §6 button_press for anything the
        active screen does not list. This tile now draws one Button on the
        panel — the control that starts the bench node — and deliberately
        declares none, so the mirror cannot reach it.

        WEB-PROTOCOL.md §10 promises that a hostile actor on the LAN can
        navigate the diagnostics UI and cannot transmit on the vehicle bus
        "because no code path exists that would let them". Declaring this
        button would create precisely that path, on the one tile that can
        start a transmitter. Its absence is the safety property, so it is
        pinned here rather than trusted.

        Checked by parsing, not importing: this module stays stdlib-only, so
        it runs on a build host with no PIL and no framebuffer."""
        tree = self._tree("kilodash/screens/specter.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                self.assertNotEqual(node.name, "model_buttons",
                                    "SPECTER must inherit the empty button "
                                    "list — a declared button is one the LAN "
                                    "can press, and this one starts a "
                                    "transmitter")

    def test_panel_control_is_exactly_one_deliberate_button(self):
        """The tile draws one Button and no more.

        This is the only control on a screen whose whole job is otherwise to
        observe, and it causes bus traffic. A second one appearing is a scope
        change that should be read, not absorbed silently."""
        tree = self._tree("kilodash/screens/specter.py")
        buttons = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name)
                   and n.func.id == "Button"]
        self.assertEqual(len(buttons), 1,
                         "expected exactly one panel control on SPECTER")


if __name__ == "__main__":
    unittest.main()
