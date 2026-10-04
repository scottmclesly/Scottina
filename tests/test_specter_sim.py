"""Operator actions against the SPECTER bench node (tools/bench/specter_sim.py).

Run from the repo root:  python -m unittest discover -s tests

WHY THIS FILE EXISTS. `_run_event` is the whole operator surface of the bench
node, and it had no test at all. Three of the eleven event types -- SESSION
BEGIN, STEP ABORT and SESSION COMPLETE -- had no branch in it. They were not
refused: `on_display_frame` accepted the sequence, counted it and echoed it,
so the DISPLAY read every one of them back as successful while the step table
never moved. The only symptom on the bench was a checklist that would not
advance, with nothing anywhere saying why.

The rig imports its byte layout from the `specter_pkg` codec, which lives
beside the rig on the bench machine. These tests skip where it is absent
rather than vendor a second copy of the wire format.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "bench"))

try:
    import specter_sim
    from specter_pkg.tocan_codec import SpecterEventType
    CODEC = None
except BaseException as error:   # the rig calls sys.exit() when absent
    specter_sim = None
    CODEC = "specter_pkg codec not available here: %s" % error


def event(event_type, step=0xFF, param=0, sequence=1):
    """One decoded display frame, as `_run_event` reads it."""
    return {"liveness_counter": 0, "event_sequence": sequence,
            "event_type": int(event_type), "step_index": step,
            "display_state": 0, "event_param": param,
            "build_flags": 0, "build_commit_low": 0}


@unittest.skipIf(CODEC, CODEC or "")
class SimTestCase(unittest.TestCase):
    def setUp(self):
        self.state = specter_sim.SimState(protocol_version=1, session_id=1,
                                          checklist_version=1)
        self.said = []
        self._say = specter_sim.say
        specter_sim.say = self.said.append

    def tearDown(self):
        specter_sim.say = self._say

    def steps(self):
        return self.state.snapshot()[:specter_sim.SPECTER_STEPS_IN_USE]


class TestEveryEventTypeIsHandled(SimTestCase):
    """The regression guard. An action with no branch is a silent lie to the
    display, because the rig echoes the sequence either way."""

    def test_no_event_type_falls_off_the_end_of_the_chain(self):
        for kind in SpecterEventType:
            if kind == SpecterEventType.NONE:
                continue
            with self.subTest(event=kind.name):
                self.said.clear()
                step = 4 if "ACTUATE" in kind.name else 0
                self.state._run_event(event(kind, step=step))
                unhandled = [line for line in self.said
                             if "UNHANDLED" in line]
                self.assertEqual(unhandled, [],
                                 "%s has no branch in _run_event" % kind.name)

    def test_an_event_with_no_branch_is_reported_loudly(self):
        """The guard must be able to fire, or it proves nothing."""
        self.state._run_event(event(99, step=0))
        self.assertTrue(any("UNHANDLED" in line for line in self.said))


class TestSessionBegin(SimTestCase):
    """The operator pressed Begin. This did nothing at all."""

    def test_it_starts_the_checklist(self):
        self.state._run_event(event(SpecterEventType.SESSION_BEGIN))
        self.assertEqual(self.steps()[0], specter_sim.ACTIVE,
                         "a session with no active step has nothing to confirm")

    def test_it_does_not_inherit_the_previous_run(self):
        """The exact bug the handshake reset exists to prevent."""
        self.state.set_all(specter_sim.GOOD)
        self.state._run_event(event(SpecterEventType.SESSION_BEGIN))
        self.assertNotIn(specter_sim.GOOD, self.steps()[1:],
                         "a new run must not show steps nobody performed")

    def test_it_holds_the_veto(self):
        self.state.set_all(specter_sim.GOOD)
        self.state._run_event(event(SpecterEventType.SESSION_BEGIN))
        self.assertTrue(self.state.veto, "a fresh run has checked nothing")

    def test_it_stops_any_hatch_the_previous_run_left_moving(self):
        self.state.hatch_port = 1
        self.state.hatch_stbd = 1
        self.state._run_event(event(SpecterEventType.SESSION_BEGIN))
        self.assertEqual(self.state.hatch_port, specter_sim.HATCH_STOPPED)
        self.assertEqual(self.state.hatch_stbd, specter_sim.HATCH_STOPPED)
        self.assertIsNone(self.state.hatch_cycle)


class TestStepAbort(SimTestCase):
    """A STEP-level abort. It does not end the session."""

    def test_it_returns_that_step_to_pending(self):
        self.state.set_step(6, specter_sim.ACTIVE)
        self.state._run_event(event(SpecterEventType.STEP_ABORT, step=6))
        self.assertEqual(self.steps()[6], specter_sim.PENDING)

    def test_it_leaves_every_other_step_alone(self):
        self.state.set_step(2, specter_sim.GOOD)
        self.state.set_step(6, specter_sim.ACTIVE)
        self.state._run_event(event(SpecterEventType.STEP_ABORT, step=6))
        self.assertEqual(self.steps()[2], specter_sim.GOOD,
                         "aborting one test must not end the run")

    def test_it_stops_the_hatches_on_the_hatch_step(self):
        self.state.hatch_port = 1
        self.state._run_event(
            event(SpecterEventType.STEP_ABORT, step=specter_sim.HATCH_STEP))
        self.assertEqual(self.state.hatch_port, specter_sim.HATCH_STOPPED)
        self.assertIsNone(self.state.hatch_cycle)


class TestSessionComplete(SimTestCase):
    """The operator declares the run finished."""

    def test_it_marks_no_step_good(self):
        self.state._run_event(event(SpecterEventType.SESSION_COMPLETE))
        self.assertNotIn(specter_sim.GOOD, self.steps())

    def test_it_never_clears_the_veto_on_its_own(self):
        """THE WHOLE POINT OF THE VETO. It is derived from the step table, so
        completing a run nobody performed must not release the boat."""
        self.state._run_event(event(SpecterEventType.SESSION_COMPLETE))
        self.assertTrue(self.state.veto)

    def test_it_clears_the_operator_request(self):
        self.state.operator_input_requested = True
        self.state._run_event(event(SpecterEventType.SESSION_COMPLETE))
        self.assertFalse(self.state.operator_input_requested)


class TestTheHandledEventsStillWork(SimTestCase):
    """The branches that already worked must not have moved."""

    def test_step_begin_activates_that_step(self):
        self.state._run_event(event(SpecterEventType.STEP_BEGIN, step=6))
        self.assertEqual(self.steps()[6], specter_sim.ACTIVE)

    def test_step_confirm_is_the_only_thing_that_makes_a_step_good(self):
        self.state._run_event(event(SpecterEventType.STEP_BEGIN, step=6))
        self.assertNotEqual(self.steps()[6], specter_sim.GOOD)
        self.state._run_event(event(SpecterEventType.STEP_CONFIRM, step=6))
        self.assertEqual(self.steps()[6], specter_sim.GOOD)

    def test_session_abort_returns_every_step_to_pending(self):
        self.state.set_all(specter_sim.GOOD)
        self.state._run_event(event(SpecterEventType.SESSION_ABORT))
        self.assertEqual(set(self.steps()), {specter_sim.PENDING})

    def test_a_step_index_above_the_capacity_is_refused(self):
        self.state._run_event(event(SpecterEventType.STEP_BEGIN, step=200))
        self.assertTrue(any("REFUSED" in line for line in self.said))


class TestTheWholeWalkAdvances(SimTestCase):
    """End to end, the way the operator drives it from the display."""

    def test_begin_then_walk_every_step_clears_the_veto(self):
        self.state._run_event(event(SpecterEventType.SESSION_BEGIN))
        self.assertTrue(self.state.veto)
        for index in range(specter_sim.SPECTER_STEPS_IN_USE):
            self.state._run_event(event(SpecterEventType.STEP_BEGIN,
                                        step=index))
            self.assertEqual(self.steps()[index], specter_sim.ACTIVE)
            self.state._run_event(event(SpecterEventType.STEP_CONFIRM,
                                        step=index))
        self.assertFalse(self.state.veto,
                         "13 confirmed steps is a complete checklist")


# --------------------------------------------------------------------------
# STEP 3, THE SEAKEEPER RIDE.
# --------------------------------------------------------------------------
# On the bench on 2026-09-10 the Ride screen showed its gauges greyed out and
# Start test did nothing. The display was right: it sent STEP_BEGIN on step 3
# ten times that day and this rig accepted every one. THE RIG HAD NO RIDE. It
# ran no sweep, sent no 0x2482, never set operator_input_requested for step 3,
# and REFUSED every actuate event that was not on step 4.

try:
    from specter_pkg.specter_ride import RideSimModel, RidePhase
    from specter_pkg.tocan_codec import SpecterActuateTarget, decode_ride_status
except BaseException:     # skipped with the rest when the codec is absent
    RideSimModel = None


@unittest.skipIf(CODEC, CODEC or "")
class TestTheSeakeeperRide(SimTestCase):
    """The node end of step 3, the way the display drives it."""

    RIDE = 3

    def setUp(self):
        super().setUp()
        self.ride = RideSimModel()
        self.now = 0

    def turn(self, count=1):
        """Run the node's Ride loop. Return the last 0x2482, decoded."""
        payload = None
        for _ in range(count):
            self.now += specter_sim.RIDE_REPEAT_MS
            payload, _keys = specter_sim.ride_turn(self.state, self.ride,
                                                   self.now)
        return decode_ride_status(payload)

    def flag(self):
        _data, fields = self.state.build_frame()
        return fields["operator_input_requested"]

    def test_at_rest_the_ride_reports_zero_not_no_data(self):
        """OBSERVATION 1. The gauges were greyed out over a Ride at rest."""
        got = self.turn()
        self.assertTrue(got["ride_link_up"])
        self.assertTrue(got["port_valid"] and got["starboard_valid"],
                        "a Ride at rest is a MEASUREMENT of zero")
        self.assertEqual((got["port_percent"], got["starboard_percent"]),
                         (0, 0))
        self.assertFalse(got["port_moving"] or got["starboard_moving"])

    def test_start_test_runs_the_sweep_out_and_back(self):
        """OBSERVATION 2. Start test must sweep 0 -> 100 -> 0 and finish."""
        self.state._run_event(event(SpecterEventType.STEP_BEGIN,
                                    step=self.RIDE))
        self.assertEqual(self.steps()[self.RIDE], specter_sim.ACTIVE)
        self.assertEqual(self.state.ride.phase, RidePhase.TO_FULL)

        saw_moving = False
        saw_full = False
        for _ in range(400):
            got = self.turn()
            if got["port_moving"] and got["starboard_moving"]:
                saw_moving = True
                self.assertFalse(self.flag(),
                                 "no Next while the surfaces travel")
            if min(got["port_percent"], got["starboard_percent"]) >= 97:
                saw_full = True
            if self.state.ride.phase == RidePhase.DONE:
                break

        self.assertTrue(saw_moving,
                        "the moving bits drive the During test footer")
        self.assertTrue(saw_full, "both surfaces MEASURED full travel")
        self.assertEqual(self.state.ride.phase, RidePhase.DONE)
        got = self.turn()
        self.assertLessEqual(max(got["port_percent"],
                                 got["starboard_percent"]), 3,
                             "and came back to zero")
        self.assertTrue(self.flag(),
                        "the node hands back to the operator: After test")
        self.assertEqual(self.steps()[self.RIDE], specter_sim.ACTIVE,
                         "ACTIVE, not GOOD. Only the operator confirms")

    def test_the_ride_actuate_events_are_no_longer_refused(self):
        self.state._run_event(event(SpecterEventType.STEP_BEGIN,
                                    step=self.RIDE, sequence=1))
        self.state._run_event(event(SpecterEventType.ACTUATE_UP,
                                    step=self.RIDE,
                                    param=int(SpecterActuateTarget.PORT),
                                    sequence=2))
        self.assertFalse(any("REFUSED" in line for line in self.said),
                         "step 3's actuate events used to be refused")
        self.assertTrue(self.state.ride.port_moving)
        self.assertFalse(self.state.ride.starboard_moving,
                         "PORT was asked for, and the sweep's starboard "
                         "target went with the sweep")

    def test_an_undefined_target_is_refused(self):
        self.state._run_event(event(SpecterEventType.ACTUATE_UP,
                                    step=self.RIDE, param=7))
        self.assertTrue(any("REFUSED" in line for line in self.said))

    def test_an_actuate_on_any_other_step_is_still_refused(self):
        self.state._run_event(event(SpecterEventType.ACTUATE_UP, step=5))
        self.assertTrue(any("REFUSED" in line for line in self.said))

    def test_confirm_leaves_a_retract_running(self):
        """The display's Next sends the retract, then the confirm, then waits
        for a MEASURED zero. A confirm that stopped the retract held the
        operator on the screen."""
        self.ride.port = self.ride.starboard = 80
        self.turn()
        self.state._run_event(event(SpecterEventType.ACTUATE_DOWN,
                                    step=self.RIDE, sequence=1))
        self.state._run_event(event(SpecterEventType.STEP_CONFIRM,
                                    step=self.RIDE, sequence=2))
        self.assertEqual(self.steps()[self.RIDE], specter_sim.GOOD)
        self.assertTrue(self.state.ride.port_moving,
                        "the retract is still driven after the confirm")
        for _ in range(200):
            self.turn()
        self.assertTrue(self.state.ride.all_stowed)

    def test_restart_stops_the_ride(self):
        self.state._run_event(event(SpecterEventType.STEP_BEGIN,
                                    step=self.RIDE, sequence=1))
        self.turn(4)
        self.state._run_event(event(SpecterEventType.STEP_RERUN,
                                    step=self.RIDE, sequence=2))
        self.assertFalse(self.state.ride.port_moving
                         or self.state.ride.starboard_moving)
        self.assertEqual(self.steps()[self.RIDE], specter_sim.PENDING)

    def test_a_new_session_stops_the_ride(self):
        self.state._run_event(event(SpecterEventType.STEP_BEGIN,
                                    step=self.RIDE, sequence=1))
        self.turn(4)
        self.state._run_event(event(SpecterEventType.SESSION_BEGIN,
                                    sequence=2))
        self.assertEqual(self.state.ride.phase, RidePhase.IDLE)
        self.assertFalse(self.state.ride.port_moving)

    def test_the_flag_belongs_to_the_step_begun_last(self):
        self.state._run_event(event(SpecterEventType.STEP_BEGIN,
                                    step=self.RIDE, sequence=1))
        for _ in range(400):
            self.turn()
            if self.state.ride.phase == RidePhase.DONE:
                break
        self.assertTrue(self.flag())
        self.state._run_event(event(SpecterEventType.STEP_BEGIN, step=5,
                                    sequence=2))
        self.assertFalse(self.flag(),
                         "step 5 was begun since. The Ride no longer answers")

    def test_ride_node_hands_step_3_to_the_real_node(self):
        """The real node owns the Ride transport. With `ride node` set, this
        rig must not be a second node on step 3."""
        stop = specter_sim.threading.Event()
        specter_sim.handle_command(self.state, stop, "ride node")
        self.assertFalse(self.state.ride_sim)
        self.state._run_event(event(SpecterEventType.STEP_BEGIN,
                                    step=self.RIDE, sequence=1))
        self.state._run_event(event(SpecterEventType.ACTUATE_UP,
                                    step=self.RIDE,
                                    param=int(SpecterActuateTarget.PORT),
                                    sequence=2))
        self.assertFalse(self.state.ride.port_moving,
                         "THE RIG DROVE A RIDE THE REAL NODE OWNS")
        self.assertTrue(any("IGNORED" in line for line in self.said))
        self.assertFalse(self.state.ride_began,
                         "the Ride's operator flag is not the rig's to set")

        specter_sim.handle_command(self.state, stop, "ride sim")
        self.assertTrue(self.state.ride_sim)
        self.state._run_event(event(SpecterEventType.ACTUATE_UP,
                                    step=self.RIDE,
                                    param=int(SpecterActuateTarget.PORT),
                                    sequence=3))
        self.assertTrue(self.state.ride.port_moving,
                        "`ride sim` gives step 3 back to the rig")

    def test_the_console_negative_tests(self):
        stop = specter_sim.threading.Event()
        saved = specter_sim.RIDE
        specter_sim.RIDE = self.ride
        try:
            specter_sim.handle_command(self.state, stop, "ride lost")
            got = self.turn()
            self.assertFalse(got["ride_link_up"], "the Ride is unreachable")
            self.assertFalse(got["port_valid"],
                             "so nothing is reported as a measurement")
            specter_sim.handle_command(self.state, stop, "ride found")
            self.assertTrue(self.turn()["ride_link_up"])

            specter_sim.handle_command(self.state, stop, "ride freeze")
            self.assertTrue(self.state.ride_frame_frozen,
                            "the node is gone: 0x2482 stops altogether")
            specter_sim.handle_command(self.state, stop, "ride thaw")
            self.assertFalse(self.state.ride_frame_frozen)

            specter_sim.handle_command(self.state, stop, "ride set 60 20")
            got = self.turn()
            self.assertEqual((got["port_percent"],
                              got["starboard_percent"]), (60, 20))
            specter_sim.handle_command(self.state, stop, "ride")
            self.assertTrue(any("SIMULATED Seakeeper Ride" in line
                                for line in self.said))
        finally:
            specter_sim.RIDE = saved


@unittest.skipIf(CODEC, CODEC or "")
class TestTheEngine(unittest.TestCase):
    """Step 10 and 11. The display commands the engine on 0x0405 and reads
    RUNNING from a live 0x1800. The rig must answer both, and must never
    send an rpm the engine did not earn."""

    def setUp(self):
        self.engine = specter_sim.EngineModel()

    def run_for(self, start, seconds, dt=0.05):
        t = start
        while t < start + seconds:
            t += dt
            self.engine.advance(t, dt)
        return t

    def rpm_frame(self):
        frames = self.engine.frames()
        if not frames:
            return None
        can_id, data, _label = frames[0]
        self.assertEqual(can_id, (0x1800 << 8) | 0x82)
        return int.from_bytes(data[0:2], "little")

    def test_the_ignition_powers_the_ecu(self):
        self.assertIsNone(self.rpm_frame(), "ignition off: nothing is sent")
        self.engine.note_action(1, 0x20, 0.0)
        self.assertEqual(self.rpm_frame(), 0, "ignition on: 0 rpm")

    def test_engine_start_cranks_then_idles(self):
        self.engine.note_action(1, 0x20, 0.0)
        self.engine.note_action(3, 0x20, 1.0)
        self.engine.advance(1.05, 0.05)
        self.assertEqual(self.rpm_frame(), int(specter_sim.ENGINE_CRANK_RPM))
        t = self.run_for(1.05, 3.0)
        self.assertEqual(self.engine.report()["phase"], "running")
        self.assertEqual(self.rpm_frame(), int(specter_sim.ENGINE_IDLE_RPM))
        self.engine.note_action(4, 0x20, t)
        self.run_for(t, 2.0)
        self.assertEqual(self.rpm_frame(), 0, "Engine Off: back to 0 rpm")
        self.engine.note_action(2, 0x20, t + 2.0)
        self.assertIsNone(self.rpm_frame(), "Ignition Off: silent again")

    def test_a_start_without_ignition_is_refused(self):
        note = self.engine.note_action(3, 0x20, 0.0)
        self.assertIn("REFUSED", note)
        self.run_for(0.0, 2.0)
        self.assertEqual(self.engine.report()["phase"], "off")

    def test_every_frame_is_one_action(self):
        for _ in range(3):
            self.engine.note_action(1, 0x20, 0.0)
        self.assertEqual(self.engine.report()["actions"], 3)

    def test_an_unknown_action_is_refused(self):
        self.assertIn("REFUSED", self.engine.note_action(5, 0x20, 0.0))
        self.assertEqual(self.engine.report()["refused"], 1)

    def test_nostart_cranks_and_never_catches(self):
        self.engine.set_no_start(True)
        self.engine.note_action(1, 0x20, 0.0)
        self.engine.note_action(3, 0x20, 0.0)
        self.run_for(0.0, 2.0)
        r = self.engine.report()
        self.assertEqual(r["phase"], "cranking")
        self.assertEqual(self.rpm_frame(), int(specter_sim.ENGINE_CRANK_RPM))
        self.run_for(2.0, specter_sim.ENGINE_STARTER_LIMIT_S)
        self.assertEqual(self.engine.report()["phase"], "off")

    def test_fail_gives_no_rpm_at_all(self):
        """`engine fail`: Engine Start turns nothing, so 0x1800 reads 0 and
        the display must never read RUNNING."""
        self.engine.set_fail(True)
        self.engine.note_action(1, 0x20, 0.0)
        note = self.engine.note_action(3, 0x20, 0.0)
        self.assertIn("FAILED", note)
        self.run_for(0.0, 3.0)
        self.assertEqual(self.engine.report()["phase"], "off")
        self.assertEqual(self.rpm_frame(), 0,
                         "the ECU is powered and reports 0 rpm")
        self.engine.set_fail(False)
        self.engine.note_action(3, 0x20, 3.0)
        self.run_for(3.0, 1.0)
        self.assertGreater(self.rpm_frame(), 0,
                           "with `engine ok` the next start gives a live rpm "
                           "within one second")

    def test_engine_off_drops_the_rpm_to_zero(self):
        self.engine.note_action(1, 0x20, 0.0)
        self.engine.note_action(3, 0x20, 0.0)
        t = self.run_for(0.0, 3.0)
        self.engine.note_action(4, 0x20, t)
        self.run_for(t, 2.0)
        self.assertEqual(self.rpm_frame(), 0)

    def test_freeze_stops_the_rpm(self):
        self.engine.note_action(1, 0x20, 0.0)
        self.engine.freeze(True)
        self.assertIsNone(self.rpm_frame())

    def test_the_synthetic_rpm_row_is_gone(self):
        tids = [row[0] for row in specter_sim.TELEMETRY_TABLE]
        self.assertNotIn(0x1800, tids,
                         "a free-running rpm makes every start look good")


@unittest.skipIf(CODEC, CODEC or "")
class TestTheVentilationRelays(unittest.TestCase):
    """Step 5. The display switches bank 3 relay 8, both blowers, and relay
    6, both bilge pumps, with 0x0800."""

    def setUp(self):
        self.relays = specter_sim.RelayModel()

    def states(self):
        _can_id, data, _label = self.relays.frames()[0]
        self.assertEqual(data[0], 3, "bank 3")
        self.assertEqual(data[1], 3, "the six states start at relay 3")
        return list(data[2:8])

    def test_the_relays_report_what_was_commanded(self):
        self.assertEqual(self.states(), [0] * 6)
        self.assertIn("both blowers",
                      self.relays.note_command(3, 8, 1, 0x20, 0.0))
        self.assertEqual(self.states(), [0, 0, 0, 0, 0, 1], "relay 8 On")
        self.relays.note_command(3, 8, 0, 0x20, 2.0)
        self.relays.note_command(3, 6, 1, 0x20, 2.0)
        self.assertEqual(self.states(), [0, 0, 0, 1, 0, 0], "relay 6 On")
        self.relays.note_command(3, 6, 0, 0x20, 4.0)
        self.assertEqual(self.states(), [0] * 6)

    def test_a_bad_state_is_refused(self):
        self.assertIn("REFUSED", self.relays.note_command(3, 8, 7, 0x20, 0.0))
        self.assertEqual(self.states(), [0] * 6)

    def test_other_banks_are_kept_but_not_in_the_bank_3_frame(self):
        self.relays.note_command(2, 1, 1, 0x20, 0.0)
        self.assertEqual(self.relays.state_of(2, 1), 1)
        self.assertEqual(self.states(), [0] * 6)


@unittest.skipIf(CODEC, CODEC or "")
class TestTheTrim(unittest.TestCase):
    """Step 2. The display sweeps 0 -> -20 -> 0 on 0x0403, now SIGNED, and
    reads 0x1801. The rig moves in Ben's range, -20 to +20, rests at 0, and
    REFUSES a command outside it."""

    def setUp(self):
        self.trim = specter_sim.TrimModel()

    def measured(self):
        frames = self.trim.frames()
        raw = frames[0][1][0]
        return raw - 256 if raw > 127 else raw

    def run_for(self, start, seconds, dt=0.05):
        t = start
        while t < start + seconds:
            t += dt
            self.trim.advance(t, dt)
        return t

    def test_it_rests_at_zero(self):
        self.assertEqual(self.measured(), 0)

    def test_a_signed_command_is_read_as_signed(self):
        t = 0.0
        for _ in range(200):
            self.trim.note_command(0xEC, 0x20, t)
            t = self.run_for(t, 0.1)
        self.assertEqual(self.measured(), -20,
                         "0xEC IS -20, not 236 degrees")

    def test_it_travels_at_three_degrees_per_second(self):
        self.trim.note_command(0xEC, 0x20, 0.0)
        self.run_for(0.0, 0.4)
        self.trim.note_command(0xEC, 0x20, 0.4)
        self.run_for(0.4, 0.6)
        self.assertEqual(self.measured(), -3)

    def test_a_command_outside_the_range_is_refused_not_clamped(self):
        for raw in (21, 30, 40, 0xEB, 0x80):
            self.trim.note_command(raw, 0x20, 0.0)
            self.assertIsNone(self.trim.report()["command"],
                              "%d WAS ACCEPTED. It must be refused" % raw)
        self.trim.note_command(20, 0x20, 0.0)
        self.assertEqual(self.trim.report()["command"], 20.0,
                         "+20 is inside Ben's range")
        self.assertEqual((specter_sim.TRIM_MIN_DEG, specter_sim.TRIM_MAX_DEG),
                         (-20.0, 20.0))
        self.assertEqual(specter_sim.TRIM_NOMINAL_DEG, 0.0)


if __name__ == "__main__":
    unittest.main()
