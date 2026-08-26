#!/usr/bin/env python3
"""SPECTER bench simulator. It is the boat side of the bench bus.

It does two things at the same time on one SocketCAN interface:

RECEIVE  the display frame, TID 0x2402 from source 0x20.
         can_id = 0x240220, extended, 8 bytes.
         It decodes and prints every field.
         It warns if no display frame arrives for 3 seconds.

         the handshake request, TID 0x2403 from source 0x20.
         can_id = 0x240320, extended, 8 bytes.
         It answers every request at once.

TRANSMIT the status frame, TID 0x2480 from source 0x21.
         can_id = 0x248021, extended, 8 bytes.
         It sends every 200 ms.

         the handshake response, TID 0x2481 from source 0x21.
         can_id = 0x248121, extended, 8 bytes.
         It sends one for each request.

The simulator holds the authoritative step states. Every step starts at
PENDING.

Do not put CAN_ERR_FLAG in the filter mask. The kernel moves such a filter
to the error-frame list. Then it never matches a data frame.

THE WIRE LAYER IS SHARED, THE RIG IS STILL STANDALONE
This file used to hold its own constants. It now imports the byte layout
from `specter_pkg.tocan_codec`, so there is exactly one definition of every
byte on the wire and this rig can never drift from the display. It imports
the CODEC ONLY. It reads no node state, no ROS2 topic and no vessel source.
`deploy_scottina.sh` in the display repository puts the codec on Scottina.

THE SEVEN GROUPS ARE A CONSOLE CONVENIENCE
The wire carries 13 steps at indices 0 to 12, two bits each. The seven
groups are how this console addresses them, unchanged from the first rig.
Each group covers a fixed set of step indices and the seven sets partition
0 to 12 exactly once. The letters stay identity labels only. This rig names
no vessel system, which is the safety envelope in README.md.

Console commands:
  3 GOOD        set every step of group 3 to GOOD
  g3 GOOD       the same
  E1 GOOD       the same, by group letter
  s7 GOOD       set one step, 0 to 12, by index
  all PENDING   set every step
  walk          move the first step that is not GOOD one state forward
  shore on      set the shore link slot 13. `shore off` clears it
  show          print the state and the next frame
  help          print the commands
  quit          stop and close the sockets

Use: python3 specter_sim.py can0
"""

import argparse
import errno
import glob
import math
import os
import select
import signal
import socket
import struct
import sys
import threading
import time

# The shared codec. One definition of every byte on the wire.
#
# The rig runs two ways: by hand as the bench operator, and under systemd,
# where the unit has no User= and the process is root. So `~` is not one
# path. Search the candidates in order and take the first that exists.
# SPECTER_CODEC_PATH always wins, so a rig in an unusual place still runs.
CODEC_TAIL = "specter_bench/marvin_ros2_ws/src/specter_pkg"


def find_codec_path():
    """Return the first codec directory that exists, or the best guess."""
    override = os.environ.get("SPECTER_CODEC_PATH")
    if override:
        return override
    candidates = [os.path.expanduser("~/" + CODEC_TAIL)]
    candidates.extend(sorted(glob.glob("/home/*/" + CODEC_TAIL)))
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return candidates[0]


CODEC_PATH = find_codec_path()
if os.path.isdir(CODEC_PATH):
    sys.path.insert(0, CODEC_PATH)

try:
    from specter_pkg.tocan_codec import (
        SPECTER_CHECKLIST_ID,
        SPECTER_STEPS_IN_USE,
        SPECTER_STEP_CAPACITY,
        SpecterActuateTarget,
        SpecterEventType,
        SpecterHandshakeResult,
        SpecterStepState,
        SpecterVesselMode,
        decode_display_frame,
        decode_handshake_request,
        encode_handshake_response,
        encode_status_frame,
        step_name,
    )
    from specter_pkg.tocan_codec import encode_physical, encode_raw
    from specter_pkg.tocan_ids import ToCanTid
    from specter_pkg.tocan_ids import (
        DISPLAY_CAN_SOURCE,
        SPECTER_NODE_CAN_SOURCE,
        SpecterTid,
    )
except ImportError as error:
    sys.stderr.write(
        "ERROR: cannot import the shared codec: %s\n"
        "       Looked in %s\n"
        "       Deploy it from the display repository:\n"
        "         tools/deploy_scottina.sh\n"
        "       Or set SPECTER_CODEC_PATH to the specter_pkg directory.\n"
        % (error, CODEC_PATH))
    sys.exit(2)

DISPLAY_SOURCE = DISPLAY_CAN_SOURCE
NODE_SOURCE = SPECTER_NODE_CAN_SOURCE

TID_DISPLAY_FRAME = int(SpecterTid.DISPLAY_FRAME)
TID_HANDSHAKE_REQUEST = int(SpecterTid.HANDSHAKE_REQUEST)
TID_STATUS_FRAME = int(SpecterTid.STATUS_FRAME)
TID_HANDSHAKE_RESPONSE = int(SpecterTid.HANDSHAKE_RESPONSE)

RX_CAN_ID = (TID_DISPLAY_FRAME << 8) | DISPLAY_SOURCE
RX_HS_CAN_ID = (TID_HANDSHAKE_REQUEST << 8) | DISPLAY_SOURCE
TX_CAN_ID = (TID_STATUS_FRAME << 8) | NODE_SOURCE
TX_HS_CAN_ID = (TID_HANDSHAKE_RESPONSE << 8) | NODE_SOURCE

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_EFF_MASK = 0x1FFFFFFF

CAN_FRAME_FMT = "<IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FMT)

PENDING = int(SpecterStepState.PENDING)
ACTIVE = int(SpecterStepState.ACTIVE)
GOOD = int(SpecterStepState.GOOD)
FAULT = int(SpecterStepState.FAULT)
STEP_STATES = {PENDING: "PENDING", ACTIVE: "ACTIVE",
               GOOD: "GOOD", FAULT: "FAULT"}
STATE_BY_NAME = {name: value for value, name in STEP_STATES.items()}

GROUP_KEYS = ["g1", "g2", "g3", "g4", "g5", "g6", "g7"]
GROUP_LETTERS = ["S", "P", "E1", "C", "T", "E2", "R"]

# Each group covers a fixed set of step indices. The seven sets partition
# 0 to 12 exactly once, so every step belongs to one group and no step
# belongs to two. The rollup rule is display-side and is not on the wire.
GROUP_STEPS = {
    1: (1, 2, 3),
    2: (4,),
    3: (0, 6, 12),
    4: (8,),
    5: (9,),
    6: (7,),
    7: (5, 10, 11),
}

# The shore link is NOT a checklist step. It is never walked, never started
# and never confirmed. The node carries it in packed slot 13, which is spare
# in the 24-slot table.
SHORE_LINK_SLOT = 13

TX_PERIOD_S = 0.2
STALE_LIMIT_S = 3.0

# --------------------------------------------------------------------------
# Vessel telemetry.
# --------------------------------------------------------------------------
# THESE FRAMES MUST BE INDISTINGUISHABLE FROM THE VESSEL'S. Same TID, same
# source, same byte layout, same scale. If the bench and the vessel differ in
# any way, the bench proves nothing.
#
# Source addresses. BEN_TOCAN_IDS.md gives the transmitter for both as the
# device CLASS, "ToCAN device", not a specific address. The device list in
# BEN_TOCAN_SOURCE_IDS.md assigns Yanmar 0x82 (engine) and NMEA 0x83. 0x1805
# is engine feedback, so it is Yanmar; 0x5000 is a sensor, so it is NMEA. The
# repository already records that pairing in test_tocan_frame.py. Override
# either one from the console if a bus survey shows different.
#
# Period. BEN_TOCAN_IDS.md states "Rate: 1 Hz" for both. That is measured, not
# guessed. Confirm it against the vessel and correct here if it differs.
TID_FUEL_LEVEL = 0x5000
TID_ALTERNATOR_POTENTIAL = 0x1805

NMEA_SOURCE = 0x83
YANMAR_SOURCE = 0x82

TELEMETRY_PERIOD_S = 1.0
TELEMETRY_FAST_PERIOD_S = 0.1

TID_VOLTAGE = 0x1401
TID_RELAY_STATE = 0x1400
# Both come from a ToCAN device. The Switching ToCAN device, 0x81, owns the
# relays and the power management sensors. Source: BEN_TOCAN_SOURCE_IDS.md.
SWITCHING_SOURCE = 0x81          # the 10 Hz messages

# --------------------------------------------------------------------------
# The full vessel telemetry table.
# --------------------------------------------------------------------------
# Every entry is a message the display shows a value for, and every field of
# it is quoted from a document:
#
#   tid, field   temp/ToCAN.c send_0x....(), byte offset and width
#   scale        temp/can_node.cpp, applied through the SHARED CODEC, never
#                repeated here
#   source       BEN_TOCAN_SOURCE_IDS.md. Ben's TID list gives the transmitter
#                as the device CLASS, "ToCAN device". Engine feedback is the
#                Yanmar device 0x82; a tank sensor is the NMEA device 0x83.
#   rate         BEN_TOCAN_IDS.md, the "Rate" column, verbatim
#
# centre and swing are the BENCH value only. They are not from any document
# and they exist so a gauge visibly moves. Both are bounded, so a long
# demonstration cannot wander out of a sane range.
#
# (tid, source, field, centre, swing, period_s, label)
# 0x1401 Voltage Measurement carries a Voltage Source ID, so ONE TID reports
# several sources. The specification warns that some devices report more
# sources than exist and that certain ones must be ignored, and nothing names
# which id is the battery. So the rig sends a plausible SET of ids and the
# panel lists every one it sees with its value. Scott picks the right one by
# looking at the bench, which is a five second answer.
#
# (voltage_source_id, label, centre volts, swing volts)
VOLTAGE_SOURCES = (
    # ID 1 IS THE BATTERY. The display reads this one for every battery gauge.
    (1, "start battery", 12.60, 0.60),      # 12.00 to 13.20 V
    (2, "house bank", 12.85, 0.45),         # 12.40 to 13.30 V
    (3, "alternator output", 14.35, 0.40),  # 13.95 to 14.75 V
    (7, "spare sender, ignore", 0.00, 0.00),
)

# 0x1400 Relay State Feedback. Bank 1 carries the engine room relays. The
# BANK and RELAY IDS FOR THE BLOWERS, THE PUMPS AND THE LIGHTS ARE NOT IN
# can_node.cpp AND NOT IN THE MARVIN TREE, so the rig sends a bank and the
# panel shows the bank and relay ids. Scott reads the ids off the bench.
# (bank_id, initial_relay_id, six states)
RELAY_BANKS = (
    (1, 1, (1, 1, 0, 0, 1, 0)),
    (2, 7, (0, 2, 1, 0xFF, 0xFF, 0xFF)),
)

TELEMETRY_TABLE = (
    # (tid, source, field, centre, swing, rate, label)
    # centre +/- swing is the whole band the value moves in. Every band below
    # is a value the vessel could really show.
    (0x5000, NMEA_SOURCE,   'fuel_level',            65.0,  10.0,  1.0,
     'fuel level %'),                       # 55 to 75 %
    (0x1805, YANMAR_SOURCE, 'potential',             14.40,  0.50, 1.0,
     'alternator potential V'),             # 13.90 to 14.90 V
    (0x1800, YANMAR_SOURCE, 'rpm',                 1200.0, 400.0,  0.1,
     'engine rpm'),                         # 800 to 1600 rpm
    (0x1801, YANMAR_SOURCE, 'tilt_trim',              4.0,   4.0,  1.0,
     'engine tilt/trim deg'),               # 0 to 8 deg
    (0x1803, YANMAR_SOURCE, 'oil_temperature',       92.0,   5.0,  1.0,
     'engine oil temperature degC'),        # 87 to 97 degC
    (0x1804, YANMAR_SOURCE, 'engine_temperature',    82.0,   5.0,  1.0,
     'engine temperature degC'),            # 77 to 87 degC
    (0x1806, YANMAR_SOURCE, 'fuel_rate',              0.012, 0.006, 1.0,
     'fuel rate m^3/h'),                    # 0.006 to 0.018
    (0x1807, YANMAR_SOURCE, 'engine_seconds',   4460400.0,  None,  1.0,
     'engine hours s'),                     # an hour meter never moves back
    (0x180A, YANMAR_SOURCE, 'percent_engine_load',   35.0,  20.0,  1.0,
     'engine load %'),                      # 15 to 55 %
    (0x180B, YANMAR_SOURCE, 'percent_engine_torque', 30.0,  18.0,  1.0,
     'engine torque %'),                    # 12 to 48 %
    (0x180F, YANMAR_SOURCE, 'oil_temperature',       70.0,   5.0,  0.1,
     'transmission oil temperature degC'),  # 65 to 75 degC
)

# The scales are NOT repeated here. They come from the shared codec, which
# takes them from can_node.cpp. One definition of every byte.
FUEL_PERCENT_MIN = 0.0
FUEL_PERCENT_MAX = 100.0
VOLTS_MIN = 10.5
VOLTS_MAX = 15.0

# The wiggle. Fuel drifts DOWN over minutes, as a tank does. Voltage moves in
# a narrow band around a charging alternator. Both are bounded, so a long
# demonstration cannot wander out of a sane range.
# THE WIGGLE IS FOR A DEMONSTRATION. It must read as alive within a few
# seconds of someone looking at it, without being told to wait. Fuel drifting
# down over minutes was invisible.
#
# Every value is a SINE about its centre. A sine is bounded by construction,
# so no value can wander out of its plausible range however long the rig runs.
# Each one gets a different phase so they do not all move as one.
#
# A ten second cycle means a value crosses its whole band twice a minute and
# is visibly moving at every glance.
WIGGLE_PERIOD_S = 10.0

# The operator turns the whole wiggle up or down with `wiggle <gain>`. 0 holds
# every value steady, which is what you want while explaining a reading.
WIGGLE_GAIN_DEFAULT = 1.0
WIGGLE_GAIN_MAX = 3.0

FUEL_DRIFT_PERCENT_PER_SECOND = 0.05
FUEL_WIGGLE_FLOOR = 15.0
VOLTS_CENTRE = 14.4
VOLTS_SWING = 0.35
VOLTS_PERIOD_S = 45.0

# The result each --refuse-handshake choice returns.
RESULT_BY_NAME = {
    "accept": int(SpecterHandshakeResult.ACCEPT),
    "protocol": int(SpecterHandshakeResult.REJECT_PROTOCOL_VERSION),
    "capacity": int(SpecterHandshakeResult.REJECT_STEP_CAPACITY),
    "notready": int(SpecterHandshakeResult.REJECT_NODE_NOT_READY),
}
RESULT_NAMES = {value: name for name, value in
                ((r.name, int(r)) for r in SpecterHandshakeResult)}

print_lock = threading.Lock()

#: The telemetry the rig transmits. `main()` replaces it with the configured
#: one. The console reaches it by name, exactly as it reaches the step state.
TELEMETRY = None


def say(text):
    """Print one block of text. Keep threads from mixing their output."""
    with print_lock:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def group_label(index):
    """Give the label for group index 1 to 7."""
    return "%s (%s)" % (GROUP_KEYS[index - 1], GROUP_LETTERS[index - 1])


def group_steps_text(index):
    """Give the step indices one group covers, as text."""
    return " ".join("%d" % step for step in GROUP_STEPS[index])


# The payload hatch, step 4. The operator drives it by hand from the display,
# so this rig must track WHICH hatch was told to move and in WHICH direction.
# It reports intent. There is no position feedback on the bus.
HATCH_STEP = 4
HATCH_STOPPED = 0
HATCH_OPENING = 1
HATCH_CLOSING = 2
HATCH_MOTION_NAMES = {
    HATCH_STOPPED: "stopped",
    HATCH_OPENING: "OPENING",
    HATCH_CLOSING: "CLOSING",
}
ACTUATE_EVENTS = {
    int(SpecterEventType.ACTUATE_UP): HATCH_OPENING,
    int(SpecterEventType.ACTUATE_DOWN): HATCH_CLOSING,
    int(SpecterEventType.ACTUATE_STOP): HATCH_STOPPED,
}


def target_name(value):
    """Give the name of a hatch target, or mark it undefined."""
    try:
        return SpecterActuateTarget(value).name
    except ValueError:
        return "UNDEFINED_%d" % value


def event_name(value):
    """Give the name of an event type, or mark it undefined."""
    try:
        return SpecterEventType(value).name
    except ValueError:
        return "UNDEFINED_%d" % value


class SimState:
    """The authoritative step states and the handshake fields."""

    def __init__(self, protocol_version, session_id, checklist_version,
                 hold_echo=False):
        self.lock = threading.Lock()
        self.states = [PENDING] * SPECTER_STEP_CAPACITY
        self.protocol_version = protocol_version
        self.session_id = session_id
        self.checklist_version = checklist_version
        self.hold_echo = hold_echo
        self.event_echo = 0
        self.operator_input_requested = False
        self.vessel_mode = int(SpecterVesselMode.CREWED)
        self.accepted = set()
        self.events_run = 0
        self.handshakes_answered = 0
        # What the DISPLAY last told each hatch to do. Not where it is.
        self.hatch_port = HATCH_STOPPED
        self.hatch_stbd = HATCH_STOPPED

    def snapshot(self):
        with self.lock:
            return list(self.states)

    def group_snapshot(self):
        """Roll the 13 steps up into the seven group states.

        A group is GOOD when every step under it is GOOD. It is FAULT when
        any step under it is FAULT. If neither holds it shows the state of
        the LEAST ADVANCED step under it, running PENDING, ACTIVE, GOOD.
        """
        states = self.snapshot()
        out = []
        for index in range(1, 8):
            covered = [states[step] for step in GROUP_STEPS[index]]
            if all(state == GOOD for state in covered):
                out.append(GOOD)
            elif any(state == FAULT for state in covered):
                out.append(FAULT)
            else:
                out.append(min(covered))
        return out

    @property
    def veto(self):
        """True while the checklist is incomplete. Fail-closed."""
        with self.lock:
            return not all(state == GOOD
                           for state in self.states[:SPECTER_STEPS_IN_USE])

    def set_step(self, step, value):
        """Set one step 0 to 23. Return True if the value changed."""
        with self.lock:
            if self.states[step] == value:
                return False
            self.states[step] = value
            return True

    def set_group(self, index, value):
        """Set every step of group index 1 to 7. Return True if changed."""
        with self.lock:
            changed = False
            for step in GROUP_STEPS[index]:
                if self.states[step] != value:
                    self.states[step] = value
                    changed = True
            return changed

    def set_all(self, value):
        """Set every step in use. Return True if any value changed."""
        with self.lock:
            changed = any(state != value
                          for state in self.states[:SPECTER_STEPS_IN_USE])
            for step in range(SPECTER_STEPS_IN_USE):
                self.states[step] = value
            return changed

    def set_shore_link(self, connected):
        """Set the shore link slot. It is not a checklist step."""
        value = GOOD if connected else PENDING
        with self.lock:
            if self.states[SHORE_LINK_SLOT] == value:
                return False
            self.states[SHORE_LINK_SLOT] = value
            return True

    def walk_one_step(self):
        """Move the first step that is not GOOD one state forward.

        Return False when every step in use is GOOD. This drives a checklist
        without an operator, so a bench run does not need a person.
        """
        with self.lock:
            for step in range(SPECTER_STEPS_IN_USE):
                state = self.states[step]
                if state == GOOD:
                    continue
                if state == PENDING:
                    self.states[step] = ACTIVE
                    self.operator_input_requested = False
                elif state == ACTIVE:
                    self.operator_input_requested = True
                    self.states[step] = GOOD
                else:
                    self.operator_input_requested = True
                return True
            return False

    def on_display_frame(self, fields):
        """Apply one display frame.

        The frame repeats. The action does not. The rig accepts a sequence
        once and ignores every repeat of it.
        """
        sequence = fields["event_sequence"]
        if sequence == 0:
            with self.lock:
                self.event_echo = 0
            return None
        with self.lock:
            if sequence in self.accepted:
                return None
            self.accepted.add(sequence)
            self.events_run += 1
        self._run_event(fields)
        if not self.hold_echo:
            with self.lock:
                self.event_echo = sequence
        return sequence

    def _run_event(self, fields):
        """Apply one operator action to the checklist state."""
        event = fields["event_type"]
        step = fields["step_index"]

        if step != 0xFF and step >= SPECTER_STEP_CAPACITY:
            say("REFUSED: step index %d is above the capacity %d."
                % (step, SPECTER_STEP_CAPACITY))
            return

        if event == int(SpecterEventType.STEP_BEGIN) and step != 0xFF:
            self.set_step(step, ACTIVE)
            with self.lock:
                self.operator_input_requested = False
        elif event == int(SpecterEventType.STEP_CONFIRM) and step != 0xFF:
            # A step becomes GOOD only on operator confirmation.
            self.set_step(step, GOOD)
            with self.lock:
                self.operator_input_requested = False
        elif event == int(SpecterEventType.STEP_RERUN) and step != 0xFF:
            self.set_step(step, PENDING)
        elif event in ACTUATE_EVENTS:
            self._run_actuate(event, fields)
        elif event == int(SpecterEventType.SESSION_ABORT):
            self.set_all(PENDING)
            with self.lock:
                self.operator_input_requested = False
                self.hatch_port = HATCH_STOPPED
                self.hatch_stbd = HATCH_STOPPED

    def _run_actuate(self, event, fields):
        """Drive the payload hatches from one actuate event.

        Byte 5 names the target: 0 both, 1 port, 2 starboard. The step is
        NOT confirmed by any of this. Only STEP_CONFIRM marks it GOOD, and
        only the operator sends that.
        """
        motion = ACTUATE_EVENTS[event]
        target = fields.get("event_param", 0)
        step = fields["step_index"]

        if step != HATCH_STEP:
            say("REFUSED: %s is for step %d, the payload hatch. It arrived "
                "on step %s." % (event_name(event), HATCH_STEP, step))
            return
        if target not in {int(x) for x in SpecterActuateTarget}:
            say("REFUSED: hatch target %d is not defined." % target)
            return

        with self.lock:
            if target in (int(SpecterActuateTarget.BOTH),
                          int(SpecterActuateTarget.PORT)):
                self.hatch_port = motion
            if target in (int(SpecterActuateTarget.BOTH),
                          int(SpecterActuateTarget.STARBOARD)):
                self.hatch_stbd = motion
            port = self.hatch_port
            stbd = self.hatch_stbd

        say("HATCH %s %s -> port %s, starboard %s"
            % (event_name(event), target_name(target),
               HATCH_MOTION_NAMES[port], HATCH_MOTION_NAMES[stbd]))

    def hatch_text(self):
        """One line saying what the display last told the hatches to do."""
        with self.lock:
            port = self.hatch_port
            stbd = self.hatch_stbd
        if (port == HATCH_STOPPED) and (stbd == HATCH_STOPPED):
            return "hatches stopped"
        return ("hatches: port %s, starboard %s  (intent only, no position "
                "feedback on the bus)"
                % (HATCH_MOTION_NAMES[port], HATCH_MOTION_NAMES[stbd]))

    def build_frame(self, advance=False):
        """Build the 8 status bytes. Return the bytes and the fields.

        `advance` is accepted and ignored. The old status frame carried a
        sequence counter to advance. The locked status frame is a snapshot
        and carries none. `specter_tile.py` still passes the argument.
        """
        del advance
        with self.lock:
            states = list(self.states)
            echo = self.event_echo
            operator_input = self.operator_input_requested
            mode = self.vessel_mode
        veto = not all(state == GOOD
                       for state in states[:SPECTER_STEPS_IN_USE])

        data = encode_status_frame(
            step_states=states,
            veto=veto,
            operator_input_requested=operator_input,
            vessel_mode=mode,
            event_echo=echo)

        fields = {
            "veto": veto,
            "operator_input_requested": operator_input,
            "vessel_mode": mode,
            "event_echo": echo,
            "states": states,
            "groups": self.group_snapshot(),
            "shore_link": states[SHORE_LINK_SLOT],
        }
        return data, fields

    def build_handshake_response(self, request, refuse, bad_step_count):
        """Answer one handshake request. Return the bytes and the fields.

        A successful handshake sets the event sequence and the echo to 0 at
        both ends. Without this a display restart sends a sequence this rig
        still holds as accepted, and the rig ignores a live action.
        """
        result = refuse
        if result == int(SpecterHandshakeResult.ACCEPT):
            if request["protocol_version"] != self.protocol_version:
                result = int(SpecterHandshakeResult.REJECT_PROTOCOL_VERSION)
            elif request["step_capacity"] < SPECTER_STEPS_IN_USE:
                result = int(SpecterHandshakeResult.REJECT_STEP_CAPACITY)

        with self.lock:
            if result == int(SpecterHandshakeResult.ACCEPT):
                self.event_echo = 0
                self.accepted.clear()
            self.handshakes_answered += 1
            session_id = self.session_id
            checklist_id = self.checklist_version
            protocol_version = self.protocol_version

        step_count = SPECTER_STEPS_IN_USE
        if bad_step_count:
            # Fault injection. Claim more steps than the display can hold.
            # The encoder refuses an out-of-range value on purpose, so build
            # the bytes here. The display must refuse this, not resize.
            step_count = SPECTER_STEP_CAPACITY + 7
            data = bytearray(8)
            data[0] = protocol_version
            data[1] = result
            data[2:4] = int(checklist_id).to_bytes(2, "little")
            data[4:6] = int(session_id).to_bytes(2, "little")
            data[6] = step_count
            data = bytes(data)
        else:
            data = encode_handshake_response(
                result=result,
                checklist_id=checklist_id,
                session_id=session_id,
                step_count=step_count,
                protocol_version=protocol_version)

        fields = {
            "protocol_version": protocol_version,
            "result": result,
            "checklist_id": checklist_id,
            "session_id": session_id,
            "step_count": step_count,
        }
        return data, fields


class TelemetryState:
    """Every dynamic value the display shows, as the vessel sends it.

    The rig holds PHYSICAL values. The shared codec turns each one into the
    raw count the vessel puts on the wire. Nothing here knows a byte layout
    or a scale.
    """

    def __init__(self, fuel_percent=78.0, volts=VOLTS_CENTRE, wiggle=True):
        self.lock = threading.Lock()
        self.wiggle = wiggle
        self.gain = WIGGLE_GAIN_DEFAULT
        self.elapsed = 0.0
        self.values = {}
        for tid, _src, field, centre, _swing, _rate, _label in TELEMETRY_TABLE:
            self.values[(tid, field)] = centre
        # The two the operator sets by name keep their command-line defaults.
        self.values[(0x5000, 'fuel_level')] = fuel_percent
        self.values[(0x1805, 'potential')] = volts

    # -- the two named values the console sets -----------------------------
    def set_fuel(self, percent):
        """Set the fuel level. It is held inside the plausible range."""
        with self.lock:
            v = max(FUEL_PERCENT_MIN, min(FUEL_PERCENT_MAX, float(percent)))
            self.values[(0x5000, 'fuel_level')] = v
            return v

    def set_volts(self, volts):
        """Set the alternator potential. Held inside the plausible range."""
        with self.lock:
            v = max(VOLTS_MIN, min(VOLTS_MAX, float(volts)))
            self.values[(0x1805, 'potential')] = v
            return v

    def set_wiggle(self, on):
        with self.lock:
            self.wiggle = bool(on)
            return self.wiggle

    def set_gain(self, gain):
        """Turn the whole wiggle up or down. 0 holds every value steady."""
        with self.lock:
            self.gain = max(0.0, min(WIGGLE_GAIN_MAX, float(gain)))
            self.wiggle = self.gain > 0.0
            return self.gain

    def wiggled(self, centre, swing, seed):
        """A bounded sine about `centre`. Call it holding the lock.

        `seed` gives each value its own phase, so they do not all move as one
        and the screen looks like a vessel rather than a metronome.
        """
        if (not self.wiggle) or (self.gain <= 0.0) or (not swing):
            return centre
        phase = (self.elapsed / WIGGLE_PERIOD_S) * 2.0 * math.pi
        phase += float(seed % 11) * 0.57
        return centre + swing * self.gain * math.sin(phase)

    def advance(self, seconds):
        """Move every value one step. Bounded, so a demonstration is safe."""
        with self.lock:
            if not self.wiggle:
                return
            self.elapsed += seconds
            for tid, _src, field, centre, swing, _rate, _lab in TELEMETRY_TABLE:
                key = (tid, field)
                if swing is None:
                    # A value that must not move, such as an hour meter.
                    continue
                self.values[key] = self.wiggled(centre, swing, tid)

    def snapshot(self):
        with self.lock:
            return (self.values[(0x5000, 'fuel_level')],
                    self.values[(0x1805, 'potential')],
                    self.wiggle)

    def voltage_frames(self):
        """One 0x1401 frame for each voltage source id."""
        out = []
        for source_id, label, centre, swing in VOLTAGE_SOURCES:
            with self.lock:
                volts = self.wiggled(centre, swing, source_id * 3)
            millivolts = int(round(volts * 1000.0))
            data = encode_raw(TID_VOLTAGE,
                              voltage_source_id=source_id,
                              voltage=millivolts)
            out.append((((TID_VOLTAGE << 8) | SWITCHING_SOURCE), data,
                        volts, "voltage id %d, %s" % (source_id, label),
                        TID_VOLTAGE, SWITCHING_SOURCE))
        return out

    def relay_frames(self):
        """One 0x1400 frame for each relay bank."""
        out = []
        for bank_id, initial, states in RELAY_BANKS:
            fields = {"bank_id": bank_id, "initial_relay_id": initial}
            for index, value in enumerate(states):
                fields["relay_state_%d" % index] = value
            data = encode_raw(TID_RELAY_STATE, **fields)
            out.append((((TID_RELAY_STATE << 8) | SWITCHING_SOURCE), data,
                        float(bank_id),
                        "relay bank %d, relays %d to %d"
                        % (bank_id, initial, initial + 5),
                        TID_RELAY_STATE, SWITCHING_SOURCE))
        return out

    def frames(self, period_s=None):
        """Build the telemetry frames. Return one tuple for each message.

        `period_s` selects a rate group, so the 10 Hz messages go out at 10 Hz
        and the 1 Hz messages at 1 Hz, exactly as the vessel sends them. None
        returns every message, which is what the console report wants.

        The raw counts come from the SHARED CODEC through `encode_physical`,
        so the scale is the one can_node.cpp uses. No scale is repeated here
        and this rig cannot drift from the display.
        """
        out = []
        for tid, source, field, _centre, _swing, rate, label in TELEMETRY_TABLE:
            if (period_s is not None) and (abs(rate - period_s) > 1e-9):
                continue
            value = self.value_of(tid, field)
            data = encode_physical(tid, **{field: value})
            out.append(((tid << 8) | source, data, value, label, tid, source))
        return tuple(out)

    def value_of(self, tid, field):
        """Return the current physical value of one field."""
        with self.lock:
            return self.values.get((tid, field), 0.0)


def telemetry_worker(sock, tx_lock, telemetry, stop_event, link):
    """Send the vessel telemetry at the rate each message documents.

    BEN_TOCAN_IDS.md gives a rate for every message. Engine rpm and the
    transmission are 10 Hz; the rest are 1 Hz. The rig sends each group at its
    own rate, so the wire looks like the vessel's and not like one loop.

    This runs with the rig, so the panel NODE button gates it like everything
    else. NODE on means the rig transmits. NODE off means the bus goes quiet.
    """
    errors = 0
    last_error_report = 0.0
    slow_due = 0.0
    elapsed = 0.0

    while not stop_event.is_set():
        telemetry.advance(TELEMETRY_FAST_PERIOD_S)
        elapsed += TELEMETRY_FAST_PERIOD_S

        groups = [TELEMETRY_FAST_PERIOD_S]
        if elapsed >= slow_due:
            slow_due = elapsed + TELEMETRY_PERIOD_S
            groups.append(TELEMETRY_PERIOD_S)

        for period in groups:
            batch = list(telemetry.frames(period))
            if period == TELEMETRY_PERIOD_S:
                # Both are 1 Hz in the specification.
                batch.extend(telemetry.voltage_frames())
                batch.extend(telemetry.relay_frames())
            for can_id, data, _v, _label, _tid, _src in batch:
                try:
                    with tx_lock:
                        send_frame(sock, can_id, data)
                    link['telemetry_tx'] += 1
                except OSError as error:
                    if error.errno in (errno.EBADF, errno.ENOTCONN,
                                       errno.ENODEV):
                        if not stop_event.is_set():
                            say('Telemetry TX stopped. Socket error: %s'
                                % error)
                        return
                    errors += 1
                    now = time.monotonic()
                    if now - last_error_report >= 5.0:
                        last_error_report = now
                        say('Telemetry TX error (%d so far): %s'
                            % (errors, error))
        stop_event.wait(TELEMETRY_FAST_PERIOD_S)


def open_rx_socket(interface):
    """Open a receive socket. Filter for the two display ids only."""
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    mask = CAN_EFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
    can_filters = b"".join(
        struct.pack("=II", can_id | CAN_EFF_FLAG, mask)
        for can_id in (RX_CAN_ID, RX_HS_CAN_ID))
    sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, can_filters)
    sock.bind((interface,))
    sock.settimeout(0.5)
    return sock


def open_tx_socket(interface):
    """Open a transmit socket. Set no receive filter to save buffer."""
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, b"")
    sock.bind((interface,))
    return sock


def send_frame(sock, can_id, data):
    """Send one extended 8-byte CAN frame."""
    packet = struct.pack(CAN_FRAME_FMT, can_id | CAN_EFF_FLAG, 8, data)
    sock.send(packet)


def format_display_frame(title, can_id, data, fields):
    """Format every field of one display frame as text."""
    raw = " ".join("%02X" % b for b in data)
    lines = ["=" * 66, title]
    lines.append("  can_id             : 0x%08X extended (tid 0x%04X, source 0x%02X)"
                 % (can_id, (can_id >> 8) & 0xFFFF, can_id & 0xFF))
    lines.append("  raw bytes          : %s" % raw)
    lines.append("  byte 0 liveness_counter  : %d" % fields["liveness_counter"])
    lines.append("  byte 1 event_sequence    : %d" % fields["event_sequence"])
    lines.append("  byte 2 event_type        : %d %s"
                 % (fields["event_type"], event_name(fields["event_type"])))
    step = fields["step_index"]
    if step == 0xFF:
        lines.append("  byte 3 step_index        : 255 none")
    elif step < SPECTER_STEP_CAPACITY:
        lines.append("  byte 3 step_index        : %d %s"
                     % (step, step_name(step)))
    else:
        lines.append("  byte 3 step_index        : %d OUT OF RANGE" % step)
    lines.append("  byte 4 display_state     : %d" % fields["display_state"])
    param = fields.get("event_param", 0)
    if fields["event_type"] in ACTUATE_EVENTS:
        lines.append("  byte 5 event_param       : %d hatch %s"
                     % (param, target_name(param)))
    elif param:
        lines.append("  byte 5 event_param       : %d SET ON AN EVENT THAT "
                     "CARRIES NONE" % param)
    else:
        lines.append("  byte 5 event_param       : 0 none")
    return "\n".join(lines)


def format_handshake_request(title, can_id, data, fields):
    """Format every field of one handshake request as text."""
    raw = " ".join("%02X" % b for b in data)
    lines = ["=" * 66, title]
    lines.append("  can_id             : 0x%08X extended (tid 0x%04X, source 0x%02X)"
                 % (can_id, (can_id >> 8) & 0xFFFF, can_id & 0xFF))
    lines.append("  raw bytes          : %s" % raw)
    lines.append("  byte 0 protocol_version  : %d" % fields["protocol_version"])
    lines.append("  byte 1 firmware_major    : %d" % fields["firmware_major"])
    lines.append("  byte 2 firmware_minor    : %d" % fields["firmware_minor"])
    lines.append("  byte 3 step_capacity     : %d" % fields["step_capacity"])
    return "\n".join(lines)


def format_handshake_response(fields, data):
    """Format the handshake response this rig sent."""
    raw = " ".join("%02X" % b for b in data)
    lines = ["-" * 66,
             "TX handshake response 0x%06X" % TX_HS_CAN_ID,
             "  raw bytes          : %s" % raw,
             "  result             : %d %s"
             % (fields["result"],
                RESULT_NAMES.get(fields["result"], "UNDEFINED")),
             "  checklist_id       : 0x%04X" % fields["checklist_id"],
             "  session_id         : 0x%04X" % fields["session_id"],
             "  step_count         : %d" % fields["step_count"]]
    return "\n".join(lines)


def decode_heartbeat(data):
    """Decode the display frame the Screen sends. Return every field.

    The name is kept because `specter_tile.py` imports it. What it decodes
    changed with the schema: the display frame is no longer the same layout
    as the status frame, and it carries no step states of its own.
    """
    fields = decode_display_frame(data)
    fields["sequence"] = fields["liveness_counter"]
    return fields


def rx_worker(sock, tx_sock, tx_lock, state, stop_event, link, options):
    """Receive, decode and print. Answer every handshake request."""
    count = 0
    while not stop_event.is_set():
        try:
            packet = sock.recv(CAN_FRAME_SIZE)
        except socket.timeout:
            now = time.monotonic()
            if link["last_rx"] is None:
                if now - link["started"] >= STALE_LIMIT_S:
                    if now - link["last_warn"] >= STALE_LIMIT_S:
                        link["last_warn"] = now
                        say("WARNING: stale link. No display frame 0x%06X for %.1f s."
                            % (RX_CAN_ID, now - link["started"]))
            elif now - link["last_rx"] >= STALE_LIMIT_S:
                if now - link["last_warn"] >= STALE_LIMIT_S:
                    link["last_warn"] = now
                    say("WARNING: stale link. No display frame 0x%06X for %.1f s."
                        % (RX_CAN_ID, now - link["last_rx"]))
            continue
        except OSError as error:
            if not stop_event.is_set():
                say("RX socket error: %s" % error)
            break

        can_id, dlc, payload = struct.unpack(CAN_FRAME_FMT, packet)
        can_id_clean = can_id & CAN_EFF_MASK
        data = payload[:dlc]

        if len(data) != 8:
            say("WARNING: frame 0x%06X has dlc %d. Expect 8. Skip."
                % (can_id_clean, dlc))
            continue

        now = time.monotonic()
        was_stale = (link["last_rx"] is not None
                     and now - link["last_rx"] >= STALE_LIMIT_S)
        link["last_rx"] = now

        if can_id_clean == RX_HS_CAN_ID:
            link["hs_rx_count"] += 1
            fields = decode_handshake_request(data)
            say(format_handshake_request(
                "RX handshake request #%d  t=%.3f"
                % (link["hs_rx_count"], time.time()),
                can_id_clean, data, fields))
            out, out_fields = state.build_handshake_response(
                fields, options["refuse"], options["bad_step_count"])
            try:
                with tx_lock:
                    send_frame(tx_sock, TX_HS_CAN_ID, out)
                link["hs_tx_count"] += 1
                say(format_handshake_response(out_fields, out))
            except OSError as error:
                say("TX handshake error: %s" % error)
            continue

        count += 1
        if was_stale:
            say("Link restored. Display frame 0x%06X is back." % RX_CAN_ID)

        fields = decode_display_frame(data)
        say(format_display_frame("RX display frame #%d  t=%.3f"
                                 % (count, time.time()),
                                 can_id_clean, data, fields))
        accepted = state.on_display_frame(fields)
        if accepted is not None:
            say("  event sequence %d accepted. %s step %d."
                % (accepted, event_name(fields["event_type"]),
                   fields["step_index"]))
    link["rx_count"] = count


def tx_worker(sock, tx_lock, state, stop_event, link, options):
    """Send the status frame every 200 ms.

    A transmit error must not stop the simulator. If no other node
    acknowledges, the driver queue fills and send() raises ENOBUFS. The
    thread reports the error, waits, and tries again. It only stops when
    the socket is closed.
    """
    count = 0
    errors = 0
    last_error_report = 0.0
    drop_after = options["drop_status_after"]
    while not stop_event.is_set():
        data, _fields = state.build_frame()
        if drop_after and count >= drop_after:
            if count == drop_after:
                say("FAULT INJECTION: stopped the status frame after %d frames."
                    % drop_after)
                count += 1
            stop_event.wait(TX_PERIOD_S)
            continue
        try:
            with tx_lock:
                send_frame(sock, TX_CAN_ID, data)
            count += 1
        except OSError as error:
            if error.errno in (errno.EBADF, errno.ENOTCONN, errno.ENODEV):
                if not stop_event.is_set():
                    say("TX stopped. Socket error: %s" % error)
                break
            errors += 1
            now = time.monotonic()
            if now - last_error_report >= 5.0:
                last_error_report = now
                say("TX error (%d so far): %s. The bus may have no other "
                    "node to acknowledge. Keep trying." % (errors, error))
        stop_event.wait(TX_PERIOD_S)
    link["tx_count"] = count
    link["tx_errors"] = errors


def parse_group(token):
    """Read a group token. Accept 3, g3, or E1. Return 1 to 7, or None."""
    token = token.strip()
    lowered = token.lower()
    if lowered.isdigit():
        number = int(lowered)
        if 1 <= number <= 7:
            return number
        return None
    if lowered.startswith("g") and lowered[1:].isdigit():
        number = int(lowered[1:])
        if 1 <= number <= 7:
            return number
        return None
    upper = token.upper()
    if upper in GROUP_LETTERS:
        return GROUP_LETTERS.index(upper) + 1
    return None


def parse_step(token):
    """Read a step token. Accept s7 only. Return 0 to 12, or None."""
    token = token.strip().lower()
    if not token.startswith("s") or not token[1:].isdigit():
        return None
    number = int(token[1:])
    if 0 <= number < SPECTER_STEPS_IN_USE:
        return number
    return None


HELP_TEXT = "\n".join([
    "Commands:",
    "  <group> <state>   set every step of one group. Example: 3 GOOD",
    "                    group is 1 to 7, g1 to g7, or S P E1 C T E2 R",
    "                    state is PENDING, ACTIVE, GOOD, or FAULT",
    "  s<step> <state>   set one step. Example: s7 GOOD",
    "                    step is 0 to %d" % (SPECTER_STEPS_IN_USE - 1),
    "  all <state>       set every step. Example: all PENDING",
    "  walk              move the first step that is not GOOD one forward",
    "  shore on|off      set the shore link, slot %d. Not a step."
    % SHORE_LINK_SLOT,
    "  fuel <percent>    set the fuel level, 0 to 100. Example: fuel 42",
    "  volts <v>         set the alternator potential, %.1f to %.1f"
    % (VOLTS_MIN, VOLTS_MAX),
    "  wiggle on|off     move the telemetry, or hold every value steady",
    "  wiggle hold       the same as off. Hold a value while you explain it",
    "  wiggle <gain>     0 to %.0f. Turn the movement up or down"
    % WIGGLE_GAIN_MAX,
    "  telemetry         print what is on the wire and at what period",
    "  show              print the state and the next transmit frame",
    "  help              print this text",
    "  quit              stop and close the sockets",
])


def show_outgoing(state, reason):
    """Print the transmit bytes that go out after a change."""
    data, fields = state.build_frame()
    raw = " ".join("%02X" % b for b in data)
    lines = ["-" * 66,
             "%s" % reason,
             "  next TX can_id 0x%08X : %s" % (TX_CAN_ID, raw),
             "  byte 0 flags 0x%02X  veto %d  operator_input %d  "
             "mode %d  echo %d"
             % (data[0], 1 if fields["veto"] else 0,
                1 if fields["operator_input_requested"] else 0,
                fields["vessel_mode"], fields["event_echo"]),
             "  bytes 1 to 6 step states : %s"
             % " ".join("%02X" % b for b in data[1:7])]
    lines.append("  group rollup:")
    for index, value in enumerate(fields["groups"], start=1):
        lines.append("  %-8s : %d %-7s  steps %s"
                     % (group_label(index), value, STEP_STATES[value],
                        group_steps_text(index)))
    lines.append("  steps:")
    for step in range(SPECTER_STEPS_IN_USE):
        value = fields["states"][step]
        lines.append("    step %2d  %-7s %s"
                     % (step, STEP_STATES[value], step_name(step)))
    lines.append("  slot %d shore link : %d %s"
                 % (SHORE_LINK_SLOT, fields["shore_link"],
                    STEP_STATES[fields["shore_link"]]))
    lines.append("  payload hatch : %s" % state.hatch_text())
    lines.append("  cansend form: %08X#%s"
                 % (TX_CAN_ID, "".join("%02X" % b for b in data)))
    say("\n".join(lines))


def show_telemetry(telemetry):
    """Print what telemetry is on the wire, and at what period.

    Anyone watching the rig can read this and know exactly what the display is
    being told, from which address, and how often.
    """
    _fuel, _volts, wiggle = telemetry.snapshot()
    lines = ["-" * 66,
             "Vessel telemetry. These frames are what the VESSEL sends.",
             "  wiggle : %s" % (
                 ("on, gain %.2f, %.0f s cycle"
                  % (telemetry.gain, WIGGLE_PERIOD_S)) if wiggle
                 else "HELD STEADY"),
             "  %-9s %-6s %-34s %10s  %s"
             % ("can_id", "rate", "value", "raw", "bytes")]
    for can_id, data, value, label, tid, _src in telemetry.frames():
        rate = next(r for (t2, _s, _f, _c, _w, r, _l) in TELEMETRY_TABLE
                    if t2 == tid)
        lines.append("  0x%06X  %4.0fHz %-34s %10.3f  %s"
                     % (can_id, 1.0 / rate, label, value,
                        " ".join("%02X" % b for b in data)))
    lines.append("  NOTE: 0x1805 is the ALTERNATOR potential. It is not the "
                 "battery and not the stud voltage.")
    say("\n".join(lines))


def console_loop(state, stop_event):
    """Read commands from the keyboard. Drive the step states by hand.

    Wait on stdin with select and a short timeout. Do not block in
    readline. A blocked readline stops the signal handler from ending
    the program, because Python restarts the interrupted read.
    """
    interactive = sys.stdin.isatty()
    say(HELP_TEXT)
    while not stop_event.is_set():
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
        except (OSError, ValueError):
            return
        if not ready:
            continue

        try:
            line = sys.stdin.readline()
        except (EOFError, ValueError, OSError):
            line = ""

        if line == "":
            if interactive:
                say("End of input. Stop the simulator.")
                stop_event.set()
                return
            stop_event.wait(0.5)
            continue

        handle_command(state, stop_event, line)


def handle_command(state, stop_event, line):
    """Run one console command."""
    parts = line.split()
    if not parts:
        return

    head = parts[0].lower()

    if head in ("quit", "exit"):
        say("Stop the simulator.")
        stop_event.set()
        return

    if head == "help":
        say(HELP_TEXT)
        return

    if head == "show":
        show_outgoing(state, "Current state.")
        return

    if head == "walk":
        if state.walk_one_step():
            show_outgoing(state, "Walk: one step moved forward.")
        else:
            say("Every step in use is GOOD. The veto is clear.")
        return

    if head == "telemetry":
        show_telemetry(TELEMETRY)
        return

    if head == "wiggle":
        if len(parts) < 2:
            say("Use: wiggle on, wiggle off, or wiggle <gain> 0 to %.0f."
                % WIGGLE_GAIN_MAX)
            return
        word = parts[1].lower()
        if word == "on":
            TELEMETRY.set_gain(WIGGLE_GAIN_DEFAULT)
            say("Telemetry wiggle on at gain %.1f." % WIGGLE_GAIN_DEFAULT)
        elif word in ("off", "hold"):
            TELEMETRY.set_gain(0.0)
            say("Telemetry HELD STEADY. Every value stops where it is.")
        else:
            try:
                gain = float(word)
            except ValueError:
                say("wiggle needs on, off, hold, or a number. Got %s."
                    % parts[1])
                return
            got = TELEMETRY.set_gain(gain)
            if got <= 0.0:
                say("Gain 0. Every value is held steady.")
            else:
                say("Telemetry wiggle gain %.2f. A full cycle is %.0f s."
                    % (got, WIGGLE_PERIOD_S))
        show_telemetry(TELEMETRY)
        return

    if head in ("fuel", "volts"):
        if len(parts) < 2:
            say("Use: %s <value>." % head)
            return
        try:
            value = float(parts[1])
        except ValueError:
            say("%s needs a number. Got %s." % (head, parts[1]))
            return
        if head == "fuel":
            got = TELEMETRY.set_fuel(value)
            say("Fuel level set to %.2f %%." % got)
        else:
            got = TELEMETRY.set_volts(value)
            say("Alternator potential set to %.2f V." % got)
        if TELEMETRY.snapshot()[2]:
            say("NOTE: the wiggle is on, so this value drifts. "
                "Use `wiggle off` to hold it.")
        show_telemetry(TELEMETRY)
        return

    if head == "shore":
        if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
            say("Use: shore on, or shore off.")
            return
        connected = parts[1].lower() == "on"
        if state.set_shore_link(connected):
            show_outgoing(state, "Change: shore link %s." % parts[1].lower())
        else:
            say("No change. The shore link is already %s."
                % parts[1].lower())
        return

    if len(parts) < 2:
        say("Use: <group> <state>, s<step> <state>, or all <state>. "
            "Type help.")
        return

    state_name = parts[1].upper()
    if state_name not in STATE_BY_NAME:
        say("Unknown state %s. Use PENDING, ACTIVE, GOOD, or FAULT."
            % parts[1])
        return
    value = STATE_BY_NAME[state_name]

    if head == "all":
        if state.set_all(value):
            show_outgoing(state, "Change: every step to %s." % state_name)
        else:
            say("No change. Every step is already %s." % state_name)
        return

    step = parse_step(parts[0])
    if step is not None:
        if state.set_step(step, value):
            show_outgoing(state, "Change: step %d (%s) to %s."
                          % (step, step_name(step), state_name))
        else:
            say("No change. Step %d is already %s." % (step, state_name))
        return

    index = parse_group(parts[0])
    if index is None:
        say("Unknown group %s. Use 1 to 7, g1 to g7, S P E1 C T E2 R, "
            "or s0 to s%d." % (parts[0], SPECTER_STEPS_IN_USE - 1))
        return

    if state.set_group(index, value):
        show_outgoing(state, "Change: %s steps %s to %s."
                      % (group_label(index), group_steps_text(index),
                         state_name))
    else:
        say("No change. %s is already %s."
            % (group_label(index), state_name))


def main():
    parser = argparse.ArgumentParser(
        description="SPECTER bench simulator. Receive 0x2402 and 0x2403. "
                    "Transmit 0x2480 and 0x2481.")
    parser.add_argument("interface", help="SocketCAN interface, for example can0")
    parser.add_argument("--protocol-version", type=int, default=1,
                        help="Byte 0 of the handshake response. Bench default 1.")
    parser.add_argument("--session-id", type=int, default=1,
                        help="The session id in the handshake response. "
                             "Bench default 1.")
    parser.add_argument("--checklist-version", type=int,
                        default=SPECTER_CHECKLIST_ID,
                        help="The checklist id in the handshake response. "
                             "Bench default 0x%04X." % SPECTER_CHECKLIST_ID)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Stop after S seconds. 0 means run until quit.")
    parser.add_argument("--fuel", type=float, default=78.0,
                        help="Starting fuel level in percent. Default 78.")
    parser.add_argument("--volts", type=float, default=VOLTS_CENTRE,
                        help="Starting alternator potential in volts. "
                             "Default %.1f." % VOLTS_CENTRE)
    parser.add_argument("--no-wiggle", action="store_true",
                        help="Hold the telemetry steady instead of drifting "
                             "it. The gauges then do not move.")
    parser.add_argument("--no-telemetry", action="store_true",
                        help="Do not transmit 0x5000 and 0x1805 at all. The "
                             "gauges go stale, which is the other thing "
                             "worth showing.")
    parser.add_argument("--walk-period", type=float, default=0.0,
                        help="Move one step forward every S seconds. "
                             "0 means do not walk.")

    faults = parser.add_argument_group("fault injection")
    faults.add_argument("--refuse-handshake", choices=sorted(RESULT_BY_NAME),
                        default="accept",
                        help="Answer every handshake with this result.")
    faults.add_argument("--drop-status-after", type=int, default=0,
                        metavar="N",
                        help="Stop sending the status frame after N frames. "
                             "Proves the display fails closed.")
    faults.add_argument("--hold-echo", action="store_true",
                        help="Accept an event but never echo its sequence. "
                             "Proves the display refuses new input.")
    faults.add_argument("--bad-step-count", action="store_true",
                        help="Claim a step count above the display capacity "
                             "in the handshake response. The display must "
                             "refuse it, not resize its table.")
    args = parser.parse_args()

    try:
        rx_sock = open_rx_socket(args.interface)
        tx_sock = open_tx_socket(args.interface)
    except OSError as error:
        say("ERROR: cannot open %s: %s" % (args.interface, error))
        return 1

    state = SimState(args.protocol_version & 0xFF,
                     args.session_id & 0xFFFF,
                     args.checklist_version & 0xFFFF,
                     hold_echo=args.hold_echo)

    options = {
        "refuse": RESULT_BY_NAME[args.refuse_handshake],
        "bad_step_count": args.bad_step_count,
        "drop_status_after": args.drop_status_after,
    }

    global TELEMETRY
    TELEMETRY = TelemetryState(fuel_percent=args.fuel,
                               volts=args.volts,
                               wiggle=not args.no_wiggle)

    stop_event = threading.Event()
    tx_lock = threading.Lock()
    link = {"last_rx": None, "started": time.monotonic(),
            "last_warn": 0.0, "rx_count": 0, "tx_count": 0, "tx_errors": 0,
            "hs_rx_count": 0, "hs_tx_count": 0, "telemetry_tx": 0}

    say("SPECTER bench simulator.")
    say("interface : %s" % args.interface)
    say("codec     : %s" % CODEC_PATH)
    say("RX filter : can_id 0x%06X (tid 0x%04X, source 0x%02X) display frame"
        % (RX_CAN_ID, TID_DISPLAY_FRAME, DISPLAY_SOURCE))
    say("            can_id 0x%06X (tid 0x%04X, source 0x%02X) handshake request"
        % (RX_HS_CAN_ID, TID_HANDSHAKE_REQUEST, DISPLAY_SOURCE))
    say("TX frame  : can_id 0x%06X (tid 0x%04X, source 0x%02X) status frame"
        % (TX_CAN_ID, TID_STATUS_FRAME, NODE_SOURCE))
    say("            can_id 0x%06X (tid 0x%04X, source 0x%02X) handshake response"
        % (TX_HS_CAN_ID, TID_HANDSHAKE_RESPONSE, NODE_SOURCE))
    say("TX period : %d ms" % int(TX_PERIOD_S * 1000))
    say("Stale limit: %.0f s" % STALE_LIMIT_S)
    say("Checklist : %d steps at indices 0 to %d. Slot %d is the shore link."
        % (SPECTER_STEPS_IN_USE, SPECTER_STEPS_IN_USE - 1, SHORE_LINK_SLOT))
    say("Every step starts at PENDING.")
    if args.no_telemetry:
        say("Vessel telemetry: NOT TRANSMITTED. The gauges will go stale.")
    else:
        say("Vessel telemetry, sent as the VESSEL sends it:")
        for tid, source, _field, _c, _w, rate, label in TELEMETRY_TABLE:
            say("  can_id 0x%06X  tid 0x%04X source 0x%02X  %-34s %2.0f Hz"
                % ((tid << 8) | source, tid, source, label, 1.0 / rate))
        say("  NOTE: 0x1805 is the ALTERNATOR potential. It is not the "
            "battery and not the stud voltage.")
    show_outgoing(state, "Start state.")

    def handle_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    rx_thread = threading.Thread(
        target=rx_worker,
        args=(rx_sock, tx_sock, tx_lock, state, stop_event, link, options),
        name="rx", daemon=True)
    tx_thread = threading.Thread(
        target=tx_worker,
        args=(tx_sock, tx_lock, state, stop_event, link, options),
        name="tx", daemon=True)
    telemetry_thread = None
    if not args.no_telemetry:
        telemetry_thread = threading.Thread(
            target=telemetry_worker,
            args=(tx_sock, tx_lock, TELEMETRY, stop_event, link),
            name="telemetry", daemon=True)

    rx_thread.start()
    tx_thread.start()
    if telemetry_thread is not None:
        telemetry_thread.start()

    if args.duration > 0:
        def stop_later():
            stop_event.wait(args.duration)
            stop_event.set()
        threading.Thread(target=stop_later, daemon=True).start()

    if args.walk_period > 0:
        def walk_later():
            while not stop_event.is_set():
                stop_event.wait(args.walk_period)
                if stop_event.is_set():
                    return
                if not state.walk_one_step():
                    say("Every step is GOOD. The veto is clear.")
                    return
        threading.Thread(target=walk_later, daemon=True).start()

    try:
        console_loop(state, stop_event)
        while not stop_event.is_set():
            stop_event.wait(0.5)
    finally:
        stop_event.set()
        tx_thread.join(timeout=2.0)
        rx_thread.join(timeout=2.0)
        rx_sock.close()
        tx_sock.close()
        if telemetry_thread is not None:
            telemetry_thread.join(timeout=2.0)
        say("Telemetry frames sent: %d." % link["telemetry_tx"])
        say("Stopped. TX frames accepted: %d. TX errors: %d. "
            "RX display frames decoded: %d. Handshakes answered: %d."
            % (link["tx_count"], link["tx_errors"], link["rx_count"],
               link["hs_tx_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
