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


if __name__ == "__main__":
    unittest.main()
