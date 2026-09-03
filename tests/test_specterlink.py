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


def display_frame(seq=0, event_seq=0, event_type=0, step=0xFF, state=0,
                  event_param=0):
    """Build one display frame, 0x240220. Byte 0 is the liveness counter."""
    return bytes([seq, event_seq & 0x0F, event_type, step, state,
                  event_param, 0, 0])


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


class TestTheIdentityOnEveryDisplayFrame(unittest.TestCase):
    """Bytes 6 and 7 of the DISPLAY frame carry the build identity.

    The handshake request carries it too, but that request goes out only
    WHILE THE DISPLAY IS BLOCKED. A unit sat blocked on the bench for days
    and sent none, so nothing on the bus could name the image it ran, and
    the detector built for exactly that was blind to it. The display frame
    goes out every 500 ms in every state.
    """

    @staticmethod
    def df(flags=SL.BUILD_PRESENT, low=0):
        return bytes([0x2A, 0, 0, 0xFF, 0, 0, flags, low])

    def test_the_identity_is_decoded(self):
        f = SL.decode_display(self.df(0x83, 0xEF))
        self.assertEqual(f["build_flags"], 0x83)
        self.assertEqual(f["build_commit_low"], 0xEF)
        self.assertFalse(f["build_clean"])

    def test_a_clean_image_reads_clean(self):
        f = SL.decode_display(self.df(SL.BUILD_PRESENT, 0xEF))
        self.assertTrue(f["build_clean"])
        self.assertIn("CLEAN", f["build_text"])

    def test_the_commit_is_never_padded_with_zeros(self):
        """`..ef` says the rest is not on this frame. `00ef` would be a lie."""
        f = SL.decode_display(self.df(SL.BUILD_PRESENT, 0xEF))
        self.assertIn("..ef", f["build_text"])
        self.assertNotIn("00ef", f["build_text"])

    def test_an_unknown_identity_shows_no_commit(self):
        f = SL.decode_display(self.df(SL.BUILD_ID_UNKNOWN | SL.BUILD_PRESENT, 0))
        self.assertIn("unknown", f["build_text"])
        self.assertNotIn("..", f["build_text"])

    def test_zero_can_never_read_as_clean(self):
        """THE FALSE REASSURANCE THIS PREVENTS.

        An image built before the identity sends 0x00, and without a
        presence bit 0x00 also means clean. The bench read a four-day-old
        image as CLEAN because of exactly that, and every symptom on the
        display was then chased as if the image were current.
        """
        f = SL.decode_display(self.df(0x00, 0x00))
        self.assertFalse(f["build_clean"])
        self.assertIn("NO IDENTITY", f["build_text"])
        self.assertNotIn("CLEAN", f["build_text"])

    def test_the_other_display_fields_still_decode(self):
        f = SL.decode_display(self.df(0x81, 0xAB))
        self.assertEqual(f["liveness_counter"], 0x2A)
        self.assertEqual(f["step_index"], 0xFF)
        self.assertEqual(f["display_state"], 0)


class TestTheBuildFlags(unittest.TestCase):
    """Byte 4 of the handshake request names the image on the unit.

    The display was flashed from a working tree that was not origin. Nothing
    on the wire said so, and three unrelated-looking symptoms all came from
    that one cause. This byte turns that day of chasing into one line.
    """

    @staticmethod
    def hs(flags=SL.BUILD_PRESENT, major=0x0C, minor=0xEF):
        return bytes([1, major, minor, 24, flags, 0, 0, 0])

    def test_a_clean_image_reads_clean(self):
        f = SL.decode_hs_request(self.hs(SL.BUILD_PRESENT))
        self.assertTrue(f["build_clean"])
        self.assertIn("CLEAN", f["build_text"])
        self.assertIn("0cef", f["build_text"])

    def test_a_dirty_image_shouts(self):
        f = SL.decode_hs_request(self.hs(SL.BUILD_DIRTY | SL.BUILD_PRESENT))
        self.assertFalse(f["build_clean"])
        self.assertIn("DIRTY", f["build_text"])
        self.assertIn("***", f["build_text"])

    def test_dirty_and_behind_are_both_named(self):
        f = SL.decode_hs_request(self.hs(SL.BUILD_DIRTY | SL.BUILD_BEHIND | SL.BUILD_PRESENT))
        self.assertIn("DIRTY", f["build_text"])
        self.assertIn("BEHIND", f["build_text"])

    def test_an_unknown_identity_never_shows_a_plausible_commit(self):
        f = SL.decode_hs_request(self.hs(SL.BUILD_ID_UNKNOWN | SL.BUILD_PRESENT, 0, 0))
        self.assertIn("unknown", f["build_text"])
        self.assertNotIn("0000", f["build_text"])

    def test_the_flag_values(self):
        self.assertEqual(SL.BUILD_PRESENT, 0x80)
        self.assertEqual(SL.BUILD_DIRTY, 0x01)
        self.assertEqual(SL.BUILD_BEHIND, 0x02)
        self.assertEqual(SL.BUILD_ID_UNKNOWN, 0x04)
        self.assertEqual(SL.BUILD_REMOTE_STALE, 0x08)

    def test_the_other_request_fields_still_decode(self):
        f = SL.decode_hs_request(self.hs(0x83))
        self.assertEqual(f["protocol_version"], 1)
        self.assertEqual(f["step_capacity"], 24)
        self.assertEqual(f["firmware_text"], "0x0CEF")


class TestTheEventParameter(unittest.TestCase):
    """Byte 5 names WHICH payload hatch an actuate event drives.

    The panel must show the target, because ACTUATE UP on its own does not
    say what moves. It must NOT show a target on any other event, because
    byte 5 carries no meaning there.
    """

    def test_byte_5_is_decoded(self):
        frame = display_frame(seq=5, event_seq=3, event_type=8, step=4,
                              state=2, event_param=1)
        self.assertEqual(SL.decode_display(frame)["event_param"], 1)

    def test_an_actuate_event_names_its_hatch(self):
        self.assertEqual(SL.event_detail(8, 0), "ACTUATE UP BOTH")
        self.assertEqual(SL.event_detail(8, 1), "ACTUATE UP PORT")
        self.assertEqual(SL.event_detail(9, 2), "ACTUATE DOWN STARBOARD")
        self.assertEqual(SL.event_detail(10, 0), "ACTUATE STOP BOTH")

    def test_every_other_event_names_no_hatch(self):
        # STEP BEGIN with a stray byte 5 must still read STEP BEGIN. The
        # parameter has no meaning on it, so inventing one would mislead.
        self.assertEqual(SL.event_detail(2, 0), "STEP BEGIN")
        self.assertEqual(SL.event_detail(2, 1), "STEP BEGIN")
        self.assertEqual(SL.event_detail(5, 2), "STEP ABORT")

    def test_an_undefined_target_is_named_undefined(self):
        self.assertEqual(SL.event_detail(8, 7), "ACTUATE UP UNDEFINED 7")

    def test_the_three_event_values_did_not_move_anything(self):
        # No existing event changed value. This is what makes the wire
        # change safe: every frame that worked before still decodes the same.
        self.assertEqual(SL.event_type_name(5), "STEP ABORT")
        self.assertEqual(SL.event_type_name(7), "SESSION COMPLETE")
        self.assertEqual(SL.event_type_name(8), "ACTUATE UP")
        self.assertEqual(SL.event_type_name(9), "ACTUATE DOWN")
        self.assertEqual(SL.event_type_name(10), "ACTUATE STOP")


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
        blob = reader._filter()
        # Four filters now: the two periodic frames and the handshake pair.
        # Read the length rather than pin it, so adding a fifth id does not
        # break this test for the wrong reason.
        self.assertEqual(len(blob) % 8, 0, "each filter is an id and a mask")
        count = len(blob) // 8
        vals = struct.unpack("=" + "I" * (count * 2), blob)
        ids = [vals[i] & SL.CAN_EFF_MASK for i in range(0, len(vals), 2)]
        masks = [vals[i] for i in range(1, len(vals), 2)]
        for mask in masks:
            self.assertEqual(mask & 0x20000000, 0,
                             "CAN_ERR_FLAG must not appear in the mask")
        self.assertIn(SL.DISPLAY_CAN_ID, ids)
        self.assertIn(SL.NODE_CAN_ID, ids)
        self.assertIn(SL.HS_REQUEST_CAN_ID, ids)
        self.assertIn(SL.HS_RESPONSE_CAN_ID, ids)

    def test_the_handshake_pair_is_decoded(self):
        """The panel shows the firmware identity, so the pair must decode."""
        req = SL.decode(bytes([1, 0xED, 0xBF, 24, 0, 0, 0, 0]),
                        SL.HS_REQUEST_CAN_ID)
        self.assertEqual(req["firmware_id"], 0xEDBF)
        self.assertEqual(req["firmware_text"], "0x EDBF".replace(" ", ""))
        self.assertEqual(req["step_capacity"], 24)

        rsp = SL.decode(bytes([1, 0, 1, 0, 7, 0, 13, 0]),
                        SL.HS_RESPONSE_CAN_ID)
        self.assertEqual(rsp["result_text"], "ACCEPT")
        self.assertEqual(rsp["checklist_id"], 0x0001)
        self.assertEqual(rsp["session_id"], 7)
        self.assertEqual(rsp["step_count"], 13)

    def test_an_image_with_no_identity_is_named_not_called_version_zero(self):
        """0x0000 is not a version. It means the build cannot say."""
        req = SL.decode(bytes([1, 0, 0, 24, 0, 0, 0, 0]),
                        SL.HS_REQUEST_CAN_ID)
        self.assertEqual(req["firmware_id"], 0)
        self.assertEqual(req["firmware_text"], "none")

    def test_every_step_has_a_name_and_slot_13_is_not_a_step(self):
        self.assertEqual(len(SL.STEP_NAMES), SL.STEPS_IN_USE)
        self.assertEqual(SL.step_name(0), "automatic systems test")
        self.assertEqual(SL.step_name(7), "E-STOP test")
        self.assertEqual(SL.step_name(12), "navigation lights")
        self.assertEqual(SL.step_name(SL.SHORE_LINK_SLOT),
                         "shore operator link")


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


# --------------------------------------------------------------------------
# The panel row builder. THIS LAYER HAD NO TEST, which is how a dead branch
# survived: every field below decodes correctly and was covered, and the
# screen still rendered none of it.
# --------------------------------------------------------------------------
def _load_screen_module():
    """Import the SPECTER screen, standing in for PIL if it is absent.

    The screen needs PIL for names at import time and for the font cache.
    This file promises stdlib only, and the bench runs it on machines with
    no Pillow, so supply a stub when the real one is missing. A machine that
    HAS Pillow uses it, so this never hides a real import error there.
    """
    import types
    if "PIL" not in sys.modules:
        pil = types.ModuleType("PIL")

        def _truetype(*args, **kwargs):
            # theme.font() catches OSError and falls back, so this is the
            # documented path, not an error the test is papering over.
            raise OSError("no fonts in the test environment")

        pil.ImageFont = types.SimpleNamespace(truetype=_truetype,
                                              load_default=lambda: object())
        pil.Image = types.SimpleNamespace()
        pil.ImageDraw = types.SimpleNamespace()
        sys.modules["PIL"] = pil
        for name in ("ImageFont", "Image", "ImageDraw"):
            sys.modules["PIL." + name] = getattr(pil, name)
    from kilodash.screens import specter as screen_module
    return screen_module


def display_frame_build(flags, commit_low, state=0):
    """A display frame carrying a build identity in bytes 6 and 7."""
    data = bytearray(display_frame(state=state))
    data[6] = flags
    data[7] = commit_low
    return bytes(data)


def hs_request_frame(flags=SL.BUILD_PRESENT, major=0x0C, minor=0xEF):
    return bytes([1, major, minor, SL.STEP_CAPACITY, flags, 0, 0, 0])


def hs_response_frame(result=0, checklist=1, session=7, steps=13):
    return bytes([1, result, checklist & 0xFF, checklist >> 8,
                  session & 0xFF, session >> 8, steps, 0])


def _snapshot(can_id, label, data, decoder):
    """The shape LinkState.snapshot() returns, with the fields decoded."""
    fields = decoder(data)
    return {"can_id": can_id, "label": label, "alive": True, "age": 0.1,
            "frames": 10, "gaps": 0, "repeats": 0, "period_ms": 500.0,
            "period_avg_ms": 500.0, "period_min_ms": 500.0,
            "period_max_ms": 500.0, "fields": fields, "data": data,
            "states": (fields or {}).get("states")}


def panel(display_data, node_data=None, hs_req=None, hs_rsp=None):
    """A screen with no app and no socket, holding one pair of frames."""
    module = _load_screen_module()
    screen = object.__new__(module.SpecterScreen)
    screen._snap = (
        _snapshot(SL.DISPLAY_CAN_ID, "DISPLAY", display_data, SL.decode_display),
        _snapshot(SL.NODE_CAN_ID, "NODE", node_data if node_data is not None
                  else node_frame(), SL.decode_node))
    screen._hs = ({"fields": SL.decode_hs_request(hs_req) if hs_req else None},
                  {"fields": SL.decode_hs_response(hs_rsp) if hs_rsp else None})
    screen._sim = "inactive"      # keep the telemetry block to one row
    return screen


def row(rows, label):
    for entry in rows:
        if entry["label"] == label:
            return entry
    return None


def node_frame_slots(flags, slots):
    """A status frame with any of the 24 packed slots set."""
    data = bytearray(8)
    data[0] = flags
    for n, value in enumerate(slots):
        data[1 + (n >> 2)] |= (value & 3) << ((n & 3) * 2)
    return bytes(data)


class TestTheSystemTestSlotsAreDecoded(unittest.TestCase):
    """Slots 14 to 17 carry the automatic system test. They were thrown away.

    `decode_node` unpacked all 24 slots and kept 13 steps and slot 13, so the
    four results the whole of step 0 produces reached nothing. The one screen
    a person watches while debugging the system test showed nothing about it.
    """

    def test_the_four_results_survive_the_decode(self):
        slots = [SL.PENDING] * 24
        slots[14] = SL.GOOD
        slots[15] = SL.GOOD
        slots[16] = SL.FAULT
        slots[17] = SL.GOOD
        fields = SL.decode_node(node_frame_slots(0x03, slots))
        self.assertEqual(fields["system_test"],
                         [SL.GOOD, SL.GOOD, SL.FAULT, SL.GOOD])

    def test_the_checklist_is_still_thirteen_steps(self):
        slots = [SL.GOOD] * 24
        fields = SL.decode_node(node_frame_slots(0x00, slots))
        self.assertEqual(len(fields["steps"]), 13,
                         "slots 14 to 17 are NOT checklist steps")

    def test_the_first_fault_is_named(self):
        self.assertEqual(
            SL.syscheck_fault([SL.GOOD, SL.FAULT, SL.FAULT, SL.GOOD]), "ROS",
            "first in slot order, so the panel names the same one every time")
        self.assertIsNone(SL.syscheck_fault([SL.GOOD] * 4))
        self.assertIsNone(SL.syscheck_fault([]))

    def test_a_pending_check_is_not_a_report(self):
        """A check still PENDING is not a pass. It is the whole rule."""
        self.assertFalse(SL.syscheck_reported([SL.GOOD, SL.GOOD,
                                               SL.PENDING, SL.GOOD]))
        self.assertTrue(SL.syscheck_reported([SL.GOOD, SL.GOOD,
                                              SL.FAULT, SL.GOOD]))
        self.assertFalse(SL.syscheck_reported([]))

    def test_the_panel_never_reads_ready_over_a_silent_subsystem(self):
        slots = [SL.PENDING] * 24
        slots[0] = SL.ACTIVE
        slots[14] = SL.GOOD
        slots[15] = SL.GOOD
        slots[16] = SL.FAULT
        slots[17] = SL.GOOD
        rows = panel(display_frame(),
                     node_frame_slots(0x03, slots)).specter_rows()
        summary = row(rows, "SYSTEM TEST")
        self.assertIsNotNone(summary, "the four results reach the panel")
        self.assertEqual(summary["state"], "fault")
        failed = row(rows, "  FAILED")
        self.assertIsNotNone(failed)
        self.assertIn("MAVLINK", failed["value"])

    def test_a_test_still_running_says_so(self):
        slots = [SL.PENDING] * 24
        slots[0] = SL.ACTIVE
        slots[14] = SL.GOOD
        rows = panel(display_frame(),
                     node_frame_slots(0x01, slots)).specter_rows()
        waiting = row(rows, "  WAITING")
        self.assertIsNotNone(waiting, "an unfinished test must not look done")
        self.assertIn("1 of 4", waiting["value"])
        self.assertEqual(row(rows, "SYSTEM TEST")["state"], "caution")

    def test_nothing_is_drawn_before_the_test_starts(self):
        slots = [SL.PENDING] * 24
        rows = panel(display_frame(),
                     node_frame_slots(0x01, slots)).specter_rows()
        self.assertEqual(row(rows, "SYSTEM TEST")["state"], "caution",
                         "all four PENDING is a caution, never an ok")


class TestTheCurrentStepIsMarked(unittest.TestCase):
    """A step list where only the COLOUR says which one is running.

    Colour is the first thing a photograph of a bench screen loses, and it is
    the pair GOOD and ACTIVE that a debugger needs most.
    """

    def test_the_active_step_carries_a_marker(self):
        slots = [SL.GOOD, SL.ACTIVE] + [SL.PENDING] * 22
        rows = panel(display_frame(),
                     node_frame_slots(0x03, slots)).specter_rows()
        labels = [entry["label"] for entry in rows]
        active = [text for text in labels if text.startswith(">>")]
        self.assertEqual(len(active), 1, "exactly one step is marked current")
        self.assertIn("1", active[0])
        done = [text for text in labels
                if text.strip().startswith("0 ")]
        self.assertTrue(done and not done[0].startswith(">>"),
                        "a finished step is NOT marked current")

    def test_the_marker_says_where_the_operator_is(self):
        slots = [SL.GOOD, SL.ACTIVE] + [SL.PENDING] * 22
        rows = panel(display_frame(),
                     node_frame_slots(0x03, slots)).specter_rows()
        current = [e for e in rows if e["label"].startswith(">>")][0]
        self.assertIn("the operator is here", current["value"])
        self.assertEqual(current["state"], "caution")

    def test_no_step_active_marks_nothing(self):
        slots = [SL.GOOD] * 13 + [SL.PENDING] * 11
        rows = panel(display_frame(),
                     node_frame_slots(0x00, slots)).specter_rows()
        self.assertFalse([e for e in rows if e["label"].startswith(">>")],
                         "a finished checklist marks no current step")


class TestTheBuildLineReachesThePanel(unittest.TestCase):
    """A blocked display sends no handshake request. It still has an image.

    The fallback that reads the identity off the display frame was written
    for exactly that case and never ran: it tested `df.get("fields")`, and
    `df` is already the fields dict. The bench read a DIRTY image as though
    nothing were wrong, which is the false reassurance the identity exists
    to prevent.
    """

    def test_the_build_line_appears_with_no_handshake_at_all(self):
        rows = panel(display_frame_build(SL.BUILD_PRESENT, 0xFD)).specter_rows()
        self.assertIsNotNone(row(rows, "BUILD"),
                             "a display that never handshakes still has a build")

    def test_a_dirty_image_reads_as_a_fault(self):
        rows = panel(display_frame_build(
            SL.BUILD_PRESENT | SL.BUILD_DIRTY, 0xFD)).specter_rows()
        build = row(rows, "BUILD")
        self.assertIn("DIRTY", build["value"])
        self.assertEqual(build["state"], "fault")

    def test_a_clean_image_carries_no_fault(self):
        rows = panel(display_frame_build(SL.BUILD_PRESENT, 0xFD)).specter_rows()
        build = row(rows, "BUILD")
        self.assertIn("CLEAN", build["value"])
        self.assertIsNone(build["state"])

    def test_an_image_with_no_identity_is_never_reported_clean(self):
        rows = panel(display_frame_build(0, 0)).specter_rows()
        build = row(rows, "BUILD")
        self.assertIn("NO IDENTITY", build["value"])
        self.assertEqual(build["state"], "fault")

    def test_the_handshake_request_wins_when_one_arrived(self):
        """It carries two commit bytes; the display frame carries one."""
        rows = panel(display_frame_build(SL.BUILD_PRESENT, 0xFD),
                     hs_req=hs_request_frame()).specter_rows()
        build = row(rows, "BUILD")
        self.assertIn("0cef", build["value"])
        self.assertNotIn("..fd", build["value"])
        self.assertIsNotNone(row(rows, "FIRMWARE"))

    def test_exactly_one_build_row_is_produced(self):
        rows = panel(display_frame_build(SL.BUILD_PRESENT, 0xFD),
                     hs_req=hs_request_frame()).specter_rows()
        self.assertEqual(sum(1 for r in rows if r["label"] == "BUILD"), 1)


class TestTheDrawnScreenNamesTheImage(unittest.TestCase):
    """The unit in front of you was the one place that never said which
    image it was running."""

    #: The node control is bottom-anchored and opaque on the 320x480 panel,
    #: so this is how much glass the text block actually gets: five lines.
    FLOOR = 5 * 12

    def _lines(self, display_data, hs_req=None, hs_rsp=None, floor=None):
        import types
        screen = panel(display_data, hs_req=hs_req, hs_rsp=hs_rsp)
        screen.app = types.SimpleNamespace(w=320)
        drawn = []

        class Draw:
            def text(self, xy, text, font=None, fill=None):
                drawn.append(text)

        class Palette:
            fg = muted = warn = ok = bad = (0, 0, 0)

        screen._specter_lines(Draw(), Palette(), 0,
                              floor=self.FLOOR if floor is None else floor)
        return drawn

    def test_the_build_line_is_drawn_from_the_display_frame(self):
        lines = self._lines(display_frame_build(
            SL.BUILD_PRESENT | SL.BUILD_DIRTY, 0xFD))
        build = [line for line in lines if line.startswith("BUILD")]
        self.assertEqual(len(build), 1)
        self.assertIn("DIRTY", build[0])

    def test_nothing_is_ever_drawn_under_the_node_control(self):
        """The control is opaque and bottom-anchored, so a line written
        under it is a line the operator cannot read."""
        lines = self._lines(display_frame_build(SL.BUILD_PRESENT, 0xFD),
                            hs_rsp=hs_response_frame())
        self.assertLessEqual(len(lines) * 12, self.FLOOR)

    def test_build_survives_when_the_block_is_full(self):
        """A handshake row plus a build row is one line more than fits.

        BUILD is the line that must survive: a dirty image explains every
        other symptom at once. SHORE is the one that goes -- slot 13 is not
        a checklist step, and it is still on the web mirror.
        """
        lines = self._lines(display_frame_build(SL.BUILD_PRESENT, 0xFD),
                            hs_rsp=hs_response_frame())
        self.assertTrue(any(line.startswith("BUILD") for line in lines))
        self.assertTrue(any(line.startswith("HSHAKE") for line in lines))
        self.assertFalse(any(line.startswith("SHORE") for line in lines))

    def test_slot_13_is_on_the_mirror_and_not_on_the_glass(self):
        """The shore link is not a checklist step. It keeps its row on the
        web mirror, where space is free, and gives up its line on the 320x480
        panel, where the three things the operator watches -- the heartbeat,
        the coloured step numbers and the veto -- come first."""
        lines = self._lines(display_frame_build(SL.BUILD_PRESENT, 0xFD),
                            hs_rsp=hs_response_frame(), floor=1000)
        self.assertFalse(any(line.startswith("SHORE") for line in lines))
        rows = panel(display_frame_build(SL.BUILD_PRESENT, 0xFD),
                     node_frame(shore=SL.GOOD)).specter_rows()
        self.assertIsNotNone(row(rows, "13 shore operator link"))

    def test_the_veto_left_the_words_for_its_own_indicator(self):
        """It gates the boat. It is not a sentence among sentences."""
        lines = self._lines(display_frame_build(SL.BUILD_PRESENT, 0xFD))
        self.assertFalse(any(line.startswith("VETO") for line in lines))
        rows = panel(display_frame_build(SL.BUILD_PRESENT, 0xFD)).specter_rows()
        self.assertIsNotNone(row(rows, "VETO"), "the mirror still carries it")

    def test_the_handshake_request_still_wins_on_the_screen(self):
        lines = self._lines(display_frame_build(SL.BUILD_PRESENT, 0xFD),
                            hs_req=hs_request_frame())
        build = [line for line in lines if line.startswith("BUILD")]
        self.assertEqual(len(build), 1)
        self.assertIn("0cef", build[0])


if __name__ == "__main__":
    unittest.main()
