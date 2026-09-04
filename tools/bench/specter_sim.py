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
    from specter_pkg.tocan_codec import decode_raw, encode_physical, encode_raw
    from specter_pkg.specter_session import SpecterSession
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

# --------------------------------------------------------------------------
# The command loop. Throttle and rudder, commanded and echoed.
# --------------------------------------------------------------------------
# TID_SPECIFICATION.md gives all four: 0x0400 and 0x0402 are the COMMANDS,
# 0x0500 and 0x0502 are the ToCAN's report of what it is sending to the
# hardware. Every one is "From: Brain, Rate: 10 Hz".
#
# BRAIN IS SOURCE 0x01. The name "Brain" is not in BEN_TOCAN_SOURCE_IDS.md,
# so it was read across instead of guessed: can_node.cpp sends the same two
# commands as can_.send(0x0040001, ...) and can_.send(0x0040201, ...), which
# split as tid 0x0400 and 0x0402 with source 0x01, the Control Node. The
# specification and the source agree, so the address is quoted, not chosen.
#
# THE DISPLAY DRAWS NONE OF THESE FOUR, AND THAT IS THE POINT OF SENDING
# THEM. `gaugebar4Steering` and `gaugebar18Throttle` stay NO DATA, because
# TID_SPECIFICATION.md 0x0500 says what the number is: "This is not what the
# engine is currently doing, this is what is being sent to the engine from
# the ToCAN." The rig sends them so the bench can watch the command loop run
# and confirm the gauges IGNORE it. A gauge that started moving when these
# arrived would be the defect, not the fix.
BRAIN_SOURCE = 0x01

# --------------------------------------------------------------------------
# The automatic system test, step 0. THE RIG EMULATES EVERY SUBSYSTEM.
# --------------------------------------------------------------------------
# Scottina cannot query real bus devices, so it EMULATES them. The node then
# sees heartbeats arrive and passes HONESTLY, rather than hardcoding a pass.
#
# THE DIFFERENCE BETWEEN AN EMULATOR AND A LIE is that an emulator can be
# switched off. `silence <name>` stops one of these, and the node must then
# fail that check. If it does not, the node has an assume-ready path and the
# whole test is a decoration.
#
# ToCAN units  HEARTBEAT, TID 0x0000, from 0x91 to 0x94.
#              SCOTT'S DESIGNATION. These four are NOT in
#              BEN_TOCAN_SOURCE_IDS.md, which assigns nothing in 0x9x.
# ROS          HEARTBEAT from 0x02, the Mode Control Node. Ben's list.
# MAVLINK      A ROS TOPIC IN PRODUCTION, mavlink/heartbeat. BEN OWES IT.
#              There is no ToCAN identifier for it and none is invented: the
#              rig sends a heartbeat from a BENCH-ONLY source and the node
#              accepts it only in bench mode. On the vessel the node reads
#              the topic and this source means nothing.
# EGES         0x1401 Voltage Measurement, one per stud. BEN OWES THE MAP.
#              Studs 9, 10 and 11 stand for payload_battery_1 to 3 under the
#              agreed schema. They are not confirmed hardware.
TID_HEARTBEAT = 0x0000

# THE NODE'S OWN SYSTEM TEST, IMPORTED. NOT REIMPLEMENTED.
#
# "Shared codec, two applications" is a locked rule. At STAGE 1 Scottina is
# the node as well as every device, so the check logic it runs must be the
# SAME code `specter_pkg` runs at stage 2. A second copy here would pass this
# bench and prove nothing about the node, which is the exact migration risk
# the stage list names.
#
# The constants come from the node too. The rig SENDS from these sources and
# the node CHECKS these sources, so one definition means the two ends cannot
# drift apart and silently agree on nothing.
from specter_pkg.specter_system_test import (          # noqa: E402
    EGES_STUD_IDS,
    FIRST_ANSWER_GRACE_S,
    ROS_SOURCE,
    SYSTEM_TEST_SLOT_BASE,
    SpecterSystemTest,
    Subsystem,
    TOCAN_UNIT_SOURCES,
)

ROS_HEARTBEAT_SOURCE = ROS_SOURCE

#: BENCH ONLY. It stands in for the ROS topic mavlink/heartbeat until Ben
#: lands it. It is not a vessel address and it is not in Ben's list.
BENCH_MAVLINK_SOURCE = 0x95

#: The EGES studs, reported through 0x1401 with a voltage source id.

EGES_STUD_LABELS = {
    9: "payload_battery_1",
    10: "payload_battery_2",
    11: "payload_battery_3",
}
EGES_STUD_VOLTS = 12.8

#: Every subsystem the console can silence, and what stops when you do.
SILENCEABLE = ("tocan", "ros", "mavlink", "eges")

SYSTEM_TEST_PERIOD_S = 1.0

#: The checklist step the automatic system test runs. Slot 0 in the packed
#: table. The RESULTS ride in slots 14 to 17, which are not checklist steps.
SYSTEM_TEST_STEP = 0

#: `0x0402 Rudder Percentage Command`, from the DISPLAY at source 0x20.
#:
#: THE DISPLAY COMMANDS THE RUDDER NOW. Scott reversed the old rule on
#: 2026-09-02. Marvin also sends 0x0402 from source 0x01, so this rig obeys
#: whichever it saw last and says which one it was. TWO COMMANDERS ON ONE
#: RUDDER IS A REAL QUESTION FOR THE VESSEL and it is not one the bench may
#: answer.
TID_RUDDER_COMMAND = 0x0402

#: `0x1811 Rudder Percentage Feedback`. The MEASURED position, 10 Hz.
TID_RUDDER_FEEDBACK = 0x1811

#: The rig answers as a ToCAN device, the same source as the other 10 Hz
#: measured values.
RUDDER_FEEDBACK_SOURCE = 0x81   # SWITCHING_SOURCE, the 10 Hz device

#: How fast the modelled ram travels, in percent of full travel per second.
#:
#: A FULL SWEEP MUST TAKE REAL TIME. Port to starboard is 200 percent of
#: travel, so at 40 percent per second it takes five seconds and the whole
#: sweep about ten. A rig that jumped the feedback to the commanded value
#: would let the display finish a sweep in one slice, and every rule about
#: watching the metal move would be untested.
RUDDER_RATE_PERCENT_PER_S = 40.0

#: `0xFF` is NO COMMAND. The ram then holds where it is.
RUDDER_NO_COMMAND = 0xFF

#: How long a command stands before the ram stops. The display sends at
#: 10 Hz, so half a second is five missed frames.
#:
#: IT IS A HOLD, NOT A TIMEOUT ON THE SWEEP. The DISPLAY decides when its
#: sweep is finished, from this feedback. This only says what a modelled ram
#: does when nobody is commanding it any more, which is stop.
RUDDER_COMMAND_HOLD_S = 0.5

#: `0x0404 Linear Actuator Control`, from the DISPLAY at source 0x20.
#: spec byte 1 is the ACTUATOR ADDRESS, spec byte 2 the percentage extension.
TID_ACTUATOR_COMMAND = 0x0404

#: `0x1812 Linear Actuator Feedback`. The MEASURED position, 1 Hz.
TID_ACTUATOR_FEEDBACK = 0x1812

#: THE ACTUATOR ADDRESSES ARE THEIR OWN SPACE, 0x00 to 0xFF. They are PAYLOAD
#: values, not CAN source addresses. 0x21 is the port hatch and 0x22 the
#: starboard, both Electrak MD Thomson units. The node source address is also
#: 0x21 and there is NO COLLISION: one is a byte inside a message and the
#: other is part of an identifier.
ACTUATOR_PORT = 0x21
ACTUATOR_STBD = 0x22
ACTUATOR_IDS = (ACTUATOR_PORT, ACTUATOR_STBD)
ACTUATOR_NAMES = {ACTUATOR_PORT: "port", ACTUATOR_STBD: "starboard"}

#: The rig answers as a ToCAN device, the same source as the other measured
#: values.
ACTUATOR_FEEDBACK_SOURCE = 0x81

#: How fast a hatch screw travels, in percent of full extension per second.
#:
#: A HATCH MUST TAKE REAL TIME. Closed to open is 100 percent, so at 20
#: percent per second it takes five seconds and the full open-then-close
#: cycle about ten. A rig that jumped the feedback to the commanded value
#: would let the display finish a cycle in one slice, and every rule about
#: watching the metal move would be untested.
ACTUATOR_RATE_PERCENT_PER_S = 20.0

#: How long a command stands before the screw stops. The display sends at
#: 1 Hz, so two and a half seconds is two missed frames.
#:
#: IT IS A HOLD, NOT A TIMEOUT ON THE CYCLE. The DISPLAY decides when its
#: cycle is finished, from this feedback.
ACTUATOR_COMMAND_HOLD_S = 2.5

# THE ECHO FOLLOWS ITS COMMAND. It is not a second wiggle.
#
# Each value here takes the value of the tid it maps to, so 0x0500 always
# reports what 0x0400 just commanded. Giving the echo its own sine would put
# a different number on the two frames, and a bench watching the loop would
# see a disagreement the vessel never has. The rig must not invent a fault.
ECHO_OF = {
    0x0500: 0x0400,
    0x0502: 0x0402,
}

# THE COMMAND LOOP IS HARD LIMITED, BECAUSE ITS OUT-OF-RANGE VALUES MEAN
# SOMETHING.
#
# Every other value in this file is bounded by its sine alone, and drifting a
# little past a plausible reading only looks odd. These four are different:
# TID_SPECIFICATION.md gives 0x0400, 0x0402, 0x0500 and 0x0502 the SAME
# out-of-range vocabulary, and it is not decoration.
#
#     0 to 100        a real throttle command
#     -100 to 100     a real rudder command
#     101 to 254      ERROR
#     0xFF            NO COMMAND
#
# `wiggle <gain>` multiplies the swing by up to 3, so an unclamped rudder
# reached +/-240 and an unclamped throttle reached 150. Both land inside the
# ERROR band, and a rig that transmits ERROR while nothing is wrong teaches
# the bench to ignore the one value that matters.
#
# So the clamp is on the VALUE, after the gain, and it is not negotiable.
# Injecting a real ERROR or a real NO COMMAND is a fault-injection job and
# belongs behind a flag, never behind the demonstration wiggle.
VALUE_LIMITS = {
    (0x0400, 'throttle'): (0.0, 100.0),
    (0x0500, 'throttle'): (0.0, 100.0),
    (0x0402, 'steering'): (-100.0, 100.0),
    (0x0502, 'steering'): (-100.0, 100.0),
}
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

    # The command loop. See BRAIN_SOURCE above. THE DISPLAY MUST IGNORE
    # ALL FOUR: they carry a command, never a measured position.
    (0x0400, BRAIN_SOURCE,  'throttle',              45.0,  35.0,  0.1,
     'throttle COMMAND %'),                 # 10 to 80 %
    (0x0500, BRAIN_SOURCE,  'throttle',              45.0,  35.0,  0.1,
     'throttle command ECHO %'),            # the same band, one phase apart
    (0x0402, BRAIN_SOURCE,  'steering',               0.0,  80.0,  0.1,
     'rudder COMMAND %, - port + stbd'),    # -80 to +80 %
    (0x0502, BRAIN_SOURCE,  'steering',               0.0,  80.0,  0.1,
     'rudder command ECHO %, - port + stbd'),
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

#: The emulated subsystems of the automatic system test. The console reaches
#: it by name so `silence` and `restore` can switch one off and on.
SYSTEM_TEST = None
RUDDER = None
ACTUATORS = None


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
#: What SESSION_BEGIN does about the first step.
#:
#: A session that begins with no ACTIVE step gives the operator nothing to
#: confirm, so this rig marks step 0 ACTIVE when the run starts. The display
#: still overrides it with STEP_BEGIN for any step it names. IF THE DISPLAY
#: IS MEANT TO CHOOSE THE FIRST STEP ITSELF, set this False: the reset is
#: the part that is certainly correct, this line is the judgement call.
SESSION_BEGIN_ACTIVATES_FIRST_STEP = True

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
        self.checklist_version = checklist_version
        #
        # THE SESSION IS REAL NOW, AND 0x0000 MEANS NO SESSION.
        #
        # `self.session_id` used to be a fixed number off the command line,
        # so this rig reported session 1 from the moment it started and went
        # on reporting it for ever. A bench watching the node side could not
        # tell a logged-in operator from a display sitting on the logo, and
        # A WRONG PIN LOOKED EXACTLY LIKE A CORRECT ONE.
        #
        # The counter rule is decisions D12 and D14, and it is NOT written
        # again here: `SpecterSession` in the display repository is the one
        # place the node generates a session id, and this rig imports it.
        # A second copy of a counter is a second answer.
        self.session = SpecterSession(checklist_id=checklist_version)
        #: Seeded from --session-id so the first session can be forced to a
        #: known value for a capture. It is the COUNTER, not the current id.
        for _ in range(max(0, int(session_id) - 1)):
            self.session.begin_session()
        if int(session_id) > 0:
            self.session.end_session()
        self.hold_echo = hold_echo
        self.event_echo = 0
        self.operator_input_requested = False
        self.vessel_mode = int(SpecterVesselMode.CREWED)
        # Every sequence ever seen. It is a COUNTER for the panel, never a
        # dedup: see on_display_frame for why that was wrong.
        self.accepted = set()
        # The one sequence the dedup tests against.
        self.last_accepted = None
        # The automatic hatch cycle, when one is running.
        self.hatch_cycle = None
        self.events_run = 0
        self.handshakes_answered = 0
        # WHAT THE DISPLAY LAST ASKED FOR, so the tile can show a login
        # happening instead of only its result. None until the first event.
        self.last_event = None
        self.last_event_step = 0xFF
        # What the DISPLAY last told each hatch to do. Not where it is.
        self.hatch_port = HATCH_STOPPED
        self.hatch_stbd = HATCH_STOPPED
        # THE NODE SIDE OF THE AUTOMATIC SYSTEM TEST.
        #
        # It is `specter_pkg`'s own class, run here because at STAGE 1 this
        # rig IS the node. It is fed from the bus by `system_test_monitor`,
        # never from the emulator's own `silenced` set: reading the set would
        # prove the rig agrees with itself, and it would have reported four
        # healthy subsystems while the emitter thread was dead.
        self.checks = SpecterSystemTest()

    def session_report(self):
        """Everything the tile shows about the session, in one lock."""
        with self.lock:
            last = self.last_event
            last_step = self.last_event_step
            echo = self.event_echo
            states = list(self.states)
            waiting = self.operator_input_requested
        # EVERY ACTIVE STEP, NOT THE FIRST ONE. Reporting only the first hid
        # the step the operator had just begun: SESSION_BEGIN marks step 0
        # ACTIVE, so a STEP_BEGIN on step 1 left the tile still naming
        # step 0 while the operator worked on another screen.
        active = [i for i in range(SPECTER_STEPS_IN_USE)
                  if states[i] == ACTIVE]
        return {
            "active_steps": active,
            "running": self.session.session_is_running,
            "session_id": self.session.session_id,
            "checklist_id": self.checklist_version,
            "last_event": last,
            "last_event_step": last_step,
            "echo": echo,
            "active_step": active[0] if active else None,
            "waiting": waiting,
        }

    @property
    def session_id(self):
        """The CURRENT session, or 0x0000 while none runs."""
        return self.session.session_id

    @property
    def session_is_running(self):
        return self.session.session_is_running

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

    def begin_session(self):
        """Start a fresh checklist run. NOTHING IS INHERITED.

        Same reasoning as the handshake: a run that begins by inheriting the
        previous run's GOOD steps shows a checklist nobody performed. There
        is nothing to carry over at the moment the operator presses Begin.
        """
        with self.lock:
            self.states = [PENDING] * SPECTER_STEP_CAPACITY
            self.operator_input_requested = False
            self.hatch_cycle = None
            self.hatch_port = HATCH_STOPPED
            self.hatch_stbd = HATCH_STOPPED
            if SESSION_BEGIN_ACTIVATES_FIRST_STEP:
                self.states[0] = ACTIVE
        # THE SESSION ID ADVANCES HERE AND NOWHERE ELSE. It is outside the
        # lock because SpecterSession holds its own state and this rig must
        # not take two locks in one path.
        self.session.begin_session()

    def cycle_one_step(self, step):
        """Move ONE step to the next of the four wire states. Return the name.

        PENDING -> ACTIVE -> GOOD -> FAULT -> PENDING, for ever.

        WHY THIS EXISTS AND `walk` DOES NOT COVER IT. `walk_one_step` runs a
        checklist the way an operator runs it, so it stops at GOOD and never
        produces FAULT. That is correct for a checklist and useless for
        proving a renderer: a twenty second capture held 99 IDENTICAL status
        frames, so three of the four step states had never been on the glass
        at all, and nobody could say whether the display drew them.

        This walks the STATE MACHINE instead of the checklist. It is a
        rendering proof and it is not a session. It reports FAULT during a
        live session, which the node never does. See "FAULT semantics" in
        CLAUDE.md: a real FAULT is written on session termination.

        `operator_input_requested` follows ACTIVE, because that is the only
        state where the node waits for the operator.
        """
        order = (PENDING, ACTIVE, GOOD, FAULT)
        with self.lock:
            if not 0 <= step < SPECTER_STEP_CAPACITY:
                return None
            try:
                nxt = order[(order.index(self.states[step]) + 1) % len(order)]
            except ValueError:
                nxt = PENDING
            self.states[step] = nxt
            self.operator_input_requested = (nxt == ACTIVE)
            return STEP_STATES[nxt]

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
            # DEDUP ON THE LAST ACCEPTED SEQUENCE, NOT ON EVERY SEQUENCE EVER.
            #
            # The display holds ONE outstanding event at a time and repeats
            # its frame until this rig echoes it, so a repeat is always the
            # sequence that came immediately before. Remembering just that one
            # is enough to run each action exactly once.
            #
            # An unbounded set was WRONG, and the wrongness was hidden. The
            # sequence field is four bits and cycles 1 to 15, so a set that
            # never forgets ignores EVERYTHING once fifteen actions have run.
            # A display bug used to send every event as sequence 1, which kept
            # the set at one entry and hid this for the whole life of the rig.
            if sequence == self.last_accepted:
                return None
            self.last_accepted = sequence
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
        with self.lock:
            self.last_event = event
            self.last_event_step = fields["step_index"]
        step = fields["step_index"]

        if step != 0xFF and step >= SPECTER_STEP_CAPACITY:
            say("REFUSED: step index %d is above the capacity %d."
                % (step, SPECTER_STEP_CAPACITY))
            return

        if event == int(SpecterEventType.SESSION_BEGIN):
            # SESSION_BEGIN marks step 0 ACTIVE, so the automatic test starts
            # with it. `begin` takes the clock, so the first-answer grace runs
            # from the moment the operator pressed the key.
            self.checks.begin(time.monotonic())
            # THE OPERATOR PRESSED BEGIN, AND THIS RIG DID NOTHING.
            #
            # SESSION_BEGIN had no branch at all. `on_display_frame` still
            # accepted the sequence, counted it and echoed it, so the display
            # read its own action back as successful and moved on while the
            # step table never changed. Every step stayed PENDING and the
            # bench had nothing to witness. Silence is the worst possible
            # answer here: a refusal would at least have shown up.
            self.begin_session()
        elif event == int(SpecterEventType.STEP_BEGIN) and step != 0xFF:
            self.set_step(step, ACTIVE)
            with self.lock:
                self.operator_input_requested = False
            if step == SYSTEM_TEST_STEP:
                # RESTART TEST RESTARTS THE TEST. Without this the grace
                # window and every seen-at time carried over from the last
                # run, so a restart reported the OLD answer at once.
                self.checks.begin(time.monotonic())
            if step == HATCH_STEP:
                self.start_hatch_cycle()
        elif event == int(SpecterEventType.STEP_CONFIRM) and step != 0xFF:
            # A step becomes GOOD only on operator confirmation.
            self.set_step(step, GOOD)
            with self.lock:
                self.operator_input_requested = False
            if step == SYSTEM_TEST_STEP:
                self.checks.stop()
        elif event == int(SpecterEventType.STEP_RERUN) and step != 0xFF:
            self.set_step(step, PENDING)
            if step == SYSTEM_TEST_STEP:
                self.checks.stop()
        elif event in ACTUATE_EVENTS:
            self._run_actuate(event, fields)
        elif event == int(SpecterEventType.STEP_ABORT) and step != 0xFF:
            # A STEP-level abort. It stops what this rig started on that step
            # and returns the step to PENDING. It does NOT end the session:
            # the operator aborts one test, not the run.
            self.set_step(step, PENDING)
            if step == SYSTEM_TEST_STEP:
                self.checks.stop()
            with self.lock:
                self.operator_input_requested = False
                if step == HATCH_STEP:
                    self.hatch_cycle = None
                    self.hatch_port = HATCH_STOPPED
                    self.hatch_stbd = HATCH_STOPPED
        elif event == int(SpecterEventType.SESSION_ABORT):
            self.set_all(PENDING)
            self.checks.stop()
            with self.lock:
                self.operator_input_requested = False
                self.hatch_port = HATCH_STOPPED
                self.hatch_stbd = HATCH_STOPPED
            # The run is over. The id returns to 0x0000, and the COUNTER
            # keeps its place so the next session takes the next value.
            self.session.end_session()
        elif event == int(SpecterEventType.SESSION_COMPLETE):
            self.checks.stop()
            # The operator declares the run finished. It marks NO step GOOD.
            # The veto is derived from the step table, so setting steps here
            # would clear the veto on a checklist nobody actually ran, which
            # is the one thing the veto exists to prevent.
            with self.lock:
                self.operator_input_requested = False
                self.hatch_cycle = None
                self.hatch_port = HATCH_STOPPED
                self.hatch_stbd = HATCH_STOPPED
            self.session.end_session()
        elif event != int(SpecterEventType.NONE):
            # NEVER FAIL SILENTLY AGAIN. Three event types fell off the end
            # of this chain and the only symptom was a checklist that would
            # not move. An unhandled action is a bench finding, so say it.
            say("UNHANDLED event %s (%d), step %s. This rig echoed the "
                "sequence, so the DISPLAY believes the action ran. Nothing "
                "changed here." % (event_name(event), event,
                                   "none" if step == 0xFF else step))

    def _run_actuate(self, event, fields):
        """Drive the payload hatches from one actuate event.

        Byte 5 names the target: 0 both, 1 port, 2 starboard. The step is
        NOT confirmed by any of this. Only STEP_CONFIRM marks it GOOD, and
        only the operator sends that.
        """
        motion = ACTUATE_EVENTS[event]
        target = fields.get("event_param", 0)
        step = fields["step_index"]
        # A manual command from the operator OUTRANKS the automatic cycle. The
        # thread keeps its own schedule, so this only records the operator's
        # intent; the bench sees the last command win, which is what the
        # operator expects when they take hold of the pad.

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

    #: The automatic hatch cycle, in seconds. Both hatches open, hold, then
    #: close. It is the NODE'S work: the display sends STEP_BEGIN and watches.
    HATCH_OPEN_S = 4.0
    HATCH_HOLD_S = 2.0
    HATCH_CLOSE_S = 4.0

    def start_hatch_cycle(self):
        """Open both hatches, hold, then close them. STEP_BEGIN starts it.

        THE DISPLAY DOES NOT DRIVE THIS. It sends STEP_BEGIN like every other
        step and the node cycles the actuators. The operator then has the
        d-pad for manual control, which is what the rest of that screen is
        for.

        The step is left ACTIVE at the end, not GOOD. A step becomes GOOD only
        when the operator confirms it.
        """
        with self.lock:
            running = self.hatch_cycle is not None and self.hatch_cycle.is_alive()
        if running:
            say("The automatic hatch cycle is already running.")
            return

        def run():
            total = self.HATCH_OPEN_S + self.HATCH_HOLD_S + self.HATCH_CLOSE_S
            say("AUTOMATIC HATCH CYCLE: both hatches open, hold, close. "
                "%.0f s in total." % total)
            with self.lock:
                self.hatch_port = HATCH_OPENING
                self.hatch_stbd = HATCH_OPENING
            say("  opening both. %s" % self.hatch_text())
            time.sleep(self.HATCH_OPEN_S)

            with self.lock:
                self.hatch_port = HATCH_STOPPED
                self.hatch_stbd = HATCH_STOPPED
            say("  both open, holding. %s" % self.hatch_text())
            time.sleep(self.HATCH_HOLD_S)

            with self.lock:
                self.hatch_port = HATCH_CLOSING
                self.hatch_stbd = HATCH_CLOSING
            say("  closing both. %s" % self.hatch_text())
            time.sleep(self.HATCH_CLOSE_S)

            with self.lock:
                self.hatch_port = HATCH_STOPPED
                self.hatch_stbd = HATCH_STOPPED
                # The node has finished its part and now waits for the
                # operator. The step stays ACTIVE: only the operator makes it
                # GOOD, and the d-pad stays live for manual control.
                self.operator_input_requested = True
            say("  cycle complete. The step stays ACTIVE and the d-pad is "
                "live. %s" % self.hatch_text())

        thread = threading.Thread(target=run, name="hatch-cycle", daemon=True)
        with self.lock:
            self.hatch_cycle = thread
        thread.start()

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

        # THE AUTOMATIC SYSTEM TEST WRITES SLOTS 14 TO 17 AND NOTHING ELSE.
        #
        # They were never written at all: the rig packed 24 slots of which
        # 14 to 17 were always PENDING, so `specter_syscheck_line` had no
        # fault, no pass and no last-good and returned TESTING for ever.
        #
        # `operator_input_requested` for step 0 comes from the SAME object,
        # so the flag and the four results cannot disagree. It used to come
        # from the generic rule `nxt == ACTIVE`, which set it the moment the
        # step went ACTIVE. The display then opened NEXT over a test that had
        # produced no result at all, and the operator could confirm a check
        # that never ran. That is worse than the screen sitting still.
        now = time.monotonic()
        if self.checks.running:
            for slot, state in self.checks.slot_states(now).items():
                states[slot] = int(state)
            if states[SYSTEM_TEST_STEP] == ACTIVE:
                operator_input = self.checks.operator_input_requested(now)

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
        restarted = False
        if result == int(SpecterHandshakeResult.ACCEPT):
            if request["protocol_version"] != self.protocol_version:
                result = int(SpecterHandshakeResult.REJECT_PROTOCOL_VERSION)
            elif request["step_capacity"] < SPECTER_STEPS_IN_USE:
                result = int(SpecterHandshakeResult.REJECT_STEP_CAPACITY)

        with self.lock:
            if result == int(SpecterHandshakeResult.ACCEPT):
                self.event_echo = 0
                self.accepted.clear()
                self.last_accepted = None
                # A SUCCESSFUL HANDSHAKE STARTS A NEW SESSION.
                #
                # This rig used to clear the event echo and KEEP the step
                # table. So a display that power-cycled handshaked again and
                # inherited the previous session: steps still GOOD, the veto
                # still clear, and the display pulsing MISSION READY green on
                # a boot where nothing had been checked. Only restarting this
                # process cleared it, which is exactly the symptom Scott saw.
                #
                # A handshake is the two ends agreeing on a checklist BEFORE
                # it runs. There is nothing to inherit.
                self.states = [PENDING] * SPECTER_STEP_CAPACITY
                self.operator_input_requested = False
                self.hatch_port = HATCH_STOPPED
                self.hatch_stbd = HATCH_STOPPED
                restarted = True
            self.handshakes_answered += 1
            session_id = self.session_id
            checklist_id = self.checklist_version
            protocol_version = self.protocol_version

        if restarted:
            # A HANDSHAKE IS THE TWO ENDS AGREEING BEFORE THE RUN. There is
            # nothing to inherit, and that includes the system test result.
            # `stop` takes no lock of ours, so it runs outside the one above.
            self.checks.stop()

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

    @staticmethod
    def _limited(key, value):
        """Hold one value inside its documented range. Call it holding the lock.

        Only the command loop has limits. See VALUE_LIMITS for why those four
        cannot be allowed out of range and every other value can.
        """
        limits = VALUE_LIMITS.get(key)
        if limits is None:
            return value
        return max(limits[0], min(limits[1], value))

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
                if tid in ECHO_OF:
                    # Copied below, after every command has its new value.
                    continue
                if swing is None:
                    # A value that must not move, such as an hour meter.
                    continue
                self.values[key] = self._limited(
                    key, self.wiggled(centre, swing, tid))
            # THE ECHO IS COPIED, NEVER COMPUTED. It reports what the command
            # says, so the two frames can never disagree on the bench.
            for tid, _src, field, _c, _w, _rate, _lab in TELEMETRY_TABLE:
                if tid in ECHO_OF:
                    self.values[(tid, field)] = self.values[
                        (ECHO_OF[tid], field)]

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


class ActuatorModel:
    """Two payload hatch screws that take real time to move.

    IT IS TWO INDEPENDENT DEVICES. Each has its own position, its own
    standing command and its own freeze, because the step exists to catch ONE
    of them failing and a shared model could not show that.

    IT DOES NOT KNOW WHAT A CYCLE IS, and it must not: the cycle lives in the
    display, and a rig that understood it would be a second implementation of
    the thing under test.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.position = {a: 0.0 for a in ACTUATOR_IDS}
        self.command = {a: None for a in ACTUATOR_IDS}
        self.command_at = {a: None for a in ACTUATOR_IDS}
        self.frozen = {a: False for a in ACTUATOR_IDS}
        self.moving = {a: False for a in ACTUATOR_IDS}

    def note_command(self, actuator, percent, now):
        """One 0x0404 arrived. `percent` is the raw byte."""
        with self.lock:
            if actuator not in self.position:
                # AN ADDRESS THIS RIG DOES NOT KNOW IS DROPPED, not stored.
                # A third hatch nobody has told the bench about must not
                # drive a gauge drawn for two.
                return
            if not 0 <= percent <= 100:
                # The specification carries no encoding above 100. The rig
                # REFUSES it rather than clamping: a clamp would let a wrong
                # command look like a good one.
                return
            self.command[actuator] = int(percent)
            self.command_at[actuator] = now

    def advance(self, now, dt):
        with self.lock:
            for a in ACTUATOR_IDS:
                target = self.command[a]
                at = self.command_at[a]
                if (target is not None and at is not None
                        and (now - at) > ACTUATOR_COMMAND_HOLD_S):
                    # NOBODY IS COMMANDING ANY MORE. The screw stops where it
                    # is. It does NOT spring back to closed.
                    target = None
                    self.command[a] = None
                if target is None:
                    self.moving[a] = False
                    continue
                step = ACTUATOR_RATE_PERCENT_PER_S * dt
                gap = target - self.position[a]
                if abs(gap) <= step:
                    self.position[a] = float(target)
                    self.moving[a] = False
                else:
                    self.position[a] += step if gap > 0 else -step
                    self.moving[a] = True

    def place(self, actuator, percent):
        with self.lock:
            if actuator in self.position:
                self.position[actuator] = max(0.0, min(100.0, float(percent)))
                self.command[actuator] = None

    def freeze(self, actuator, on):
        """Stop sending 0x1812 for ONE hatch, or send it again.

        THE NEGATIVE TEST THIS EXISTS FOR: freeze one hatch mid-travel and
        the display must draw NO DATA for it, must not complete the step, and
        must not let the operator walk away from it.
        """
        with self.lock:
            if actuator in self.frozen:
                self.frozen[actuator] = bool(on)

    def report(self):
        with self.lock:
            return {a: {"position": self.position[a],
                        "command": self.command[a],
                        "frozen": self.frozen[a],
                        "moving": self.moving[a]} for a in ACTUATOR_IDS}

    def frames(self):
        """One 0x1812 per hatch, skipping any that is frozen."""
        out = []
        with self.lock:
            for a in ACTUATOR_IDS:
                if self.frozen[a]:
                    continue
                value = int(round(self.position[a]))
                value = max(0, min(100, value))
                data = bytes([a & 0xFF, value & 0xFF]) + bytes(6)
                out.append((((TID_ACTUATOR_FEEDBACK << 8)
                             | ACTUATOR_FEEDBACK_SOURCE), data,
                            "%s hatch %d%%" % (ACTUATOR_NAMES[a], value)))
        return out


def actuator_worker(sock, tx_lock, model, stop_event):
    """Move the modelled screws and report them at 1 Hz.

    The specification gives 0x1812 a 1 Hz rate. The model is advanced far
    more often than that, so the reported position is where the screw
    actually is at the moment the frame goes out rather than one second
    behind it.
    """
    tick = 0.1
    period = 1.0
    last = time.monotonic()
    due = last
    while not stop_event.is_set():
        now = time.monotonic()
        model.advance(now, now - last)
        last = now
        if now >= due:
            due = now + period
            for can_id, data, _label in model.frames():
                try:
                    with tx_lock:
                        send_frame(sock, can_id, data)
                except OSError:
                    pass
        stop_event.wait(tick)


class RudderModel:
    """A rudder that takes real time to move, and reports where it IS.

    THE RIG HAD A HATCH CYCLE AND NO STEERING EQUIVALENT. The display now
    commands the rudder directly with `0x0402` and finishes step 1 on the
    MEASURED `0x1811` position, so without something here that actually moves
    there was nothing for the display to measure and the step could not
    complete at all.

    IT MOVES TOWARD THE COMMAND AT A RATE. It does not jump. A rig that
    snapped the feedback to the commanded value would let the display finish
    a whole sweep inside one slice, and every rule about watching the metal
    move would be proved against nothing.

    THE POSITION IS THE ONLY OUTPUT. This class does not know what a sweep is
    and must not: the sweep lives in the display, and a rig that understood
    it would be a second implementation of the thing under test.
    """

    def __init__(self, position=0.0):
        self.lock = threading.Lock()
        self.position = float(position)
        self.command = None          # None means nobody is commanding
        self.command_at = None
        self.command_source = None
        self.frozen = False          # the bench can stop the feedback
        self.moving = False

    def note_command(self, percent, source, now):
        """One 0x0402 arrived. `percent` is the raw byte."""
        with self.lock:
            if percent == RUDDER_NO_COMMAND:
                self.command = None
                self.command_at = None
                self.command_source = source
                return
            value = percent - 256 if percent > 127 else percent
            if not -100 <= value <= 100:
                # 101 to 254 is ERROR in the specification. The rig REFUSES
                # it rather than clamping: a clamp would let a wrong command
                # look like a good one.
                return
            self.command = value
            self.command_at = now
            self.command_source = source

    def advance(self, now, dt):
        """Move toward the command. Return the position."""
        with self.lock:
            target = self.command
            if (target is not None and self.command_at is not None
                    and (now - self.command_at) > RUDDER_COMMAND_HOLD_S):
                # NOBODY IS COMMANDING ANY MORE. The ram stops where it is.
                # It does NOT spring back to centre.
                target = None
                self.command = None
            if target is None:
                self.moving = False
                return self.position
            step = RUDDER_RATE_PERCENT_PER_S * dt
            gap = target - self.position
            if abs(gap) <= step:
                self.position = float(target)
                self.moving = False
            else:
                self.position += step if gap > 0 else -step
                self.moving = True
            return self.position

    def report(self):
        with self.lock:
            return {"position": self.position, "command": self.command,
                    "source": self.command_source, "frozen": self.frozen,
                    "moving": self.moving}

    def freeze(self, on):
        """Stop sending 0x1811, or send it again.

        THE NEGATIVE TEST THIS EXISTS FOR: stop the feedback mid-sweep and
        the display must draw NO DATA and must NOT complete the step. A sweep
        that finishes on a dead feedback is the failure step 1 exists to
        catch.
        """
        with self.lock:
            self.frozen = bool(on)

    def frames(self):
        """The 0x1811 frame, or nothing while the feedback is frozen."""
        with self.lock:
            if self.frozen:
                return []
            value = int(round(self.position))
            value = max(-100, min(100, value))
        data = bytes([value & 0xFF]) + bytes(7)
        return [(((TID_RUDDER_FEEDBACK << 8) | RUDDER_FEEDBACK_SOURCE), data,
                 "rudder feedback %d%%" % value)]


def rudder_worker(sock, tx_lock, rudder, stop_event):
    """Move the modelled ram and report it at 10 Hz.

    The specification gives 0x1811 a 10 Hz rate, and the same period advances
    the model, so the reported position changes by a believable amount
    between frames instead of in one jump.
    """
    period = 0.1
    last = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        dt = now - last
        last = now
        rudder.advance(now, dt)
        for can_id, data, _label in rudder.frames():
            try:
                with tx_lock:
                    send_frame(sock, can_id, data)
            except OSError:
                pass
        stop_event.wait(period)


class SystemTestEmulator:
    """The four subsystems of the automatic system test, emulated.

    IT MUST BE SWITCHABLE OR IT PROVES NOTHING. `silence` stops one source.
    The node must then fail that check, and the display must name it. That is
    the whole negative test.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.silenced = set()

    def silence(self, name):
        """Stop one subsystem. Return True if the name is known."""
        name = name.lower()
        if name not in SILENCEABLE:
            return False
        with self.lock:
            self.silenced.add(name)
        return True

    def restore(self, name):
        """Start one subsystem again, or all of them."""
        name = name.lower()
        with self.lock:
            if name == "all":
                self.silenced.clear()
                return True
            if name not in SILENCEABLE:
                return False
            self.silenced.discard(name)
        return True

    def is_silent(self, name):
        with self.lock:
            return name in self.silenced

    def report(self):
        with self.lock:
            silenced = sorted(self.silenced)
        return silenced

    def frames(self):
        """Build every frame the emulated subsystems send this period."""
        out = []
        if not self.is_silent("tocan"):
            for source in TOCAN_UNIT_SOURCES:
                out.append((((TID_HEARTBEAT << 8) | source), bytes(8),
                            "ToCAN unit 0x%02X heartbeat" % source))
        if not self.is_silent("ros"):
            out.append((((TID_HEARTBEAT << 8) | ROS_HEARTBEAT_SOURCE),
                        bytes(8), "ROS heartbeat, Mode Control Node"))
        if not self.is_silent("mavlink"):
            out.append((((TID_HEARTBEAT << 8) | BENCH_MAVLINK_SOURCE),
                        bytes(8),
                        "MAVLink heartbeat, BENCH STAND-IN for the topic"))
        if not self.is_silent("eges"):
            for stud in EGES_STUD_IDS:
                data = encode_raw(TID_VOLTAGE,
                                  voltage_source_id=stud,
                                  voltage=int(round(EGES_STUD_VOLTS * 1000.0)))
                out.append((((TID_VOLTAGE << 8) | SWITCHING_SOURCE), data,
                            "EGES stud %d, %s"
                            % (stud, EGES_STUD_LABELS[stud])))
        return out


def system_test_worker(sock, tx_lock, emulator, stop_event, link):
    """Send the emulated subsystem heartbeats at 1 Hz.

    A silenced subsystem simply is not sent, which is exactly what a dead one
    looks like on the bus.

    THE `link.is_set()` FAULT. This loop used to read

        if link is None or link.is_set():

    and `link` is a DICT, not an Event. `dict` has no `is_set`, so the first
    pass raised AttributeError and this daemon thread DIED. Nothing printed
    it, so the rig looked healthy and the four heartbeats were never on the
    bus at all. A capture showed sources 0x01, 0x20, 0x21, 0x81, 0x82 and
    0x83 and nothing else. The display sat on TESTING for ever.

    It is not gated now. `telemetry_worker` is not gated either, and the two
    must behave the same way: they are both the RIG pretending to be devices.
    """
    del link
    errors = 0
    while not stop_event.is_set():
        for can_id, data, _label in emulator.frames():
            try:
                with tx_lock:
                    send_frame(sock, can_id, data)
            except OSError as error:
                errors += 1
                if errors in (1, 10, 100):
                    say("System test TX error (%d so far): %s"
                        % (errors, error))
        stop_event.wait(SYSTEM_TEST_PERIOD_S)


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


def open_monitor_socket(interface):
    """Open a socket that sees the system test evidence, and nothing else.

    WHY A THIRD SOCKET. `open_rx_socket` filters down to the two display ids
    so the printer only ever shows display traffic. Widening that filter would
    put every heartbeat through the decode-and-print path. This socket carries
    its own narrow filter instead, so the display path is untouched.

    WHY IT READS THE BUS AT ALL. At stage 1 this rig sends the heartbeats and
    also runs the node that checks them. It would be simpler for the node to
    read the emulator's `silenced` set directly, AND IT WOULD PROVE NOTHING:
    the rig would agree with itself. It reported four healthy subsystems for
    days while the emitter thread was dead. So the node reads the WIRE. If a
    frame is not on the bus, the check fails, whatever the rig believes it is
    sending.

    SocketCAN loops locally-sent frames back to every OTHER socket on the
    interface, so the frames this rig transmits arrive here exactly as a real
    device's would at stage 2.
    """
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    mask = CAN_EFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
    watched = [(TID_HEARTBEAT << 8) | source
               for source in TOCAN_UNIT_SOURCES]
    watched.append((TID_HEARTBEAT << 8) | ROS_HEARTBEAT_SOURCE)
    watched.append((TID_HEARTBEAT << 8) | BENCH_MAVLINK_SOURCE)
    watched.append((TID_VOLTAGE << 8) | SWITCHING_SOURCE)
    # THE DISPLAY'S OWN RUDDER COMMAND. can_id = (tid << 8) | source, and the
    # display is 0x20, so its command is distinguishable from Marvin's 0x01.
    watched.append((TID_RUDDER_COMMAND << 8) | DISPLAY_SOURCE)
    watched.append((TID_ACTUATOR_COMMAND << 8) | DISPLAY_SOURCE)
    can_filters = b"".join(
        struct.pack("=II", can_id | CAN_EFF_FLAG, mask)
        for can_id in watched)
    sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, can_filters)
    sock.bind((interface,))
    sock.settimeout(0.5)
    return sock


def system_test_monitor(sock, state, stop_event, rudder=None,
                        actuators=None):
    """Feed the node's system test from the frames actually on the bus.

    It notes evidence whether or not a test is running. `SpecterSystemTest`
    refuses every note while it is stopped, so a heartbeat that arrived before
    the operator pressed Begin cannot count as an answer to a test that had
    not started.
    """
    while not stop_event.is_set():
        try:
            packet = sock.recv(CAN_FRAME_SIZE)
        except socket.timeout:
            continue
        except OSError:
            if not stop_event.is_set():
                break
            break

        can_id, dlc, payload = struct.unpack(CAN_FRAME_FMT, packet)
        can_id_clean = can_id & CAN_EFF_MASK
        tid = (can_id_clean >> 8) & 0xFFFF
        source = can_id_clean & 0xFF
        now = time.monotonic()

        if tid == TID_ACTUATOR_COMMAND:
            # spec byte 1 is the actuator address, spec byte 2 the extension.
            if actuators is not None and dlc >= 2:
                actuators.note_command(payload[0], payload[1], now)
            continue

        if tid == TID_RUDDER_COMMAND:
            # spec byte 1 is data[0]. One signed byte, 1 percent per bit.
            if rudder is not None and dlc >= 1:
                rudder.note_command(payload[0], source, now)
            continue

        if tid == TID_HEARTBEAT:
            if source == BENCH_MAVLINK_SOURCE:
                # BENCH ONLY. It stands in for the ROS topic
                # mavlink/heartbeat, which Ben owes. 0x95 is NOT a vessel
                # address and this branch does not exist at stage 2: the real
                # node subscribes to the topic instead.
                state.checks.note_mavlink(now)
            else:
                state.checks.note_heartbeat(source, now)
        elif tid == TID_VOLTAGE:
            try:
                fields = decode_raw(TID_VOLTAGE, payload[:dlc])
            except Exception:
                continue
            state.checks.note_voltage(fields["voltage_source_id"], now)


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
    "  systest           print the emulated system test sources",
    "  silence <name>    stop one subsystem: %s" % ", ".join(SILENCEABLE),
    "                    the node must then FAIL that check",
    "  restore <name>    start it again. restore all restores every one",
    "  cycle <step>      move ONE step to the next of PENDING ACTIVE GOOD",
    "                    FAULT. A RENDERING PROOF, not a session: a live",
    "                    node never sends FAULT while a session runs",
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

    if head in ("silence", "restore"):
        if len(parts) < 2:
            say("Use: %s <%s>%s"
                % (head, "|".join(SILENCEABLE),
                   ", or restore all" if head == "restore" else ""))
            return
        name = parts[1].lower()
        ok = (SYSTEM_TEST.silence(name) if head == "silence"
              else SYSTEM_TEST.restore(name))
        if not ok:
            say("Unknown subsystem %r. One of: %s."
                % (parts[1], ", ".join(SILENCEABLE)))
            return
        silent = SYSTEM_TEST.report()
        say("%s %s. Silent now: %s."
            % ("Silenced" if head == "silence" else "Restored", name,
               ", ".join(silent) if silent else "nothing"))
        if head == "silence":
            say("  The node must FAIL that check, and the display must "
                "name it. If it does not, the node has an assume-ready "
                "path.")
        return

    if head == "session":
        # A BENCH LOGIN. It does what SESSION_BEGIN from the display does, so
        # the whole chain can be driven with nobody at the keypad.
        #
        # IT IS NOT A SHORTCUT PAST THE PIN. The display still has to log in
        # for ITS side of the session; this moves the NODE only. It exists
        # because the negative tests must be repeatable, and a test that needs
        # a person to press a key at the right moment is not.
        want = parts[1].lower() if len(parts) > 1 else "show"
        if want == "begin":
            state.begin_session()
            state.checks.begin(time.monotonic())
            show_outgoing(state, "Session begun. Step 0 ACTIVE, system test "
                                 "running.")
        elif want in ("end", "abort"):
            state.set_all(PENDING)
            state.checks.stop()
            state.session.end_session()
            show_outgoing(state, "Session ended. Every step PENDING.")
        else:
            say("Use: session begin, or session end.")
        return

    if head == "hatch":
        want = parts[1].lower() if len(parts) > 1 else "show"
        which = {"port": [ACTUATOR_PORT], "stbd": [ACTUATOR_STBD],
                 "starboard": [ACTUATOR_STBD],
                 "both": list(ACTUATOR_IDS)}.get(
                     parts[2].lower() if len(parts) > 2 else "both",
                     list(ACTUATOR_IDS))
        if want == "freeze":
            for a in which:
                ACTUATORS.freeze(a, True)
            say("0x1812 STOPPED for %s. The screw still moves; nothing "
                "reports where it is."
                % ", ".join(ACTUATOR_NAMES[a] for a in which))
        elif want in ("thaw", "restore"):
            for a in which:
                ACTUATORS.freeze(a, False)
            say("0x1812 is sent again for %s."
                % ", ".join(ACTUATOR_NAMES[a] for a in which))
        elif want in ("set", "open", "close", "closed"):
            if want == "open":
                value = 100.0
            elif want in ("close", "closed"):
                value = 0.0
            else:
                try:
                    value = float(parts[2])
                    which = list(ACTUATOR_IDS)
                except (IndexError, ValueError):
                    say("Use: hatch set <0 to 100>, or hatch open, "
                        "or hatch close.")
                    return
            for a in which:
                ACTUATORS.place(a, value)
            say("Placed %s at %.0f %%. Nothing is commanded."
                % (", ".join(ACTUATOR_NAMES[a] for a in which), value))
        else:
            report = ACTUATORS.report()
            lines = ["-" * 66,
                     "The two payload hatch screws. %.0f%% of travel per "
                     "second." % ACTUATOR_RATE_PERCENT_PER_S]
            for a in ACTUATOR_IDS:
                r = report[a]
                lines.append(
                    "  0x%02X %-9s measured %5.1f %%%s   command %s   "
                    "moving %s"
                    % (a, ACTUATOR_NAMES[a], r["position"],
                       "  NOT SENT" if r["frozen"] else "",
                       "none" if r["command"] is None
                       else "%d %%" % r["command"], r["moving"]))
            say("\n".join(lines))
        return

    if head == "rudder":
        want = parts[1].lower() if len(parts) > 1 else "show"
        if want == "freeze":
            # THE NEGATIVE TEST. Stop 0x1811 and the display must draw NO
            # DATA and must NOT complete step 1. A sweep that finishes on a
            # dead feedback is the failure that step exists to catch.
            RUDDER.freeze(True)
            say("Rudder feedback 0x1811 STOPPED. The ram still moves; "
                "nothing reports where it is.")
        elif want in ("thaw", "restore"):
            RUDDER.freeze(False)
            say("Rudder feedback 0x1811 is sent again.")
        elif want in ("set", "port", "starboard", "stbd"):
            # PLACE THE RAM. It is a bench control, not a command: it moves
            # the modelled position without anyone commanding 0x0402, so the
            # display can be shown a reading it would otherwise only reach
            # through a sweep that stops at its own tolerance.
            if want == "port":
                value = -100.0
            elif want in ("starboard", "stbd"):
                value = 100.0
            else:
                try:
                    value = float(parts[2])
                except (IndexError, ValueError):
                    say("Use: rudder set <-100 to 100>, or rudder port, "
                        "or rudder starboard.")
                    return
            value = max(-100.0, min(100.0, value))
            with RUDDER.lock:
                RUDDER.position = value
                RUDDER.command = None
            say("Rudder placed at %+.0f %%. Nothing is commanded." % value)
        elif want == "centre" or want == "center":
            with RUDDER.lock:
                RUDDER.position = 0.0
                RUDDER.command = None
            say("Rudder placed at centre. Nothing is commanded.")
        else:
            r = RUDDER.report()
            say("\n".join([
                "-" * 66,
                "The modelled rudder. It moves at %.0f%% of travel per second."
                % RUDDER_RATE_PERCENT_PER_S,
                "  measured 0x1811 : %+.1f %%%s"
                % (r["position"], "   NOT SENT, frozen" if r["frozen"] else ""),
                "  command  0x0402 : %s%s"
                % ("none" if r["command"] is None else "%+d %%" % r["command"],
                   "" if r["source"] is None
                   else "   from source 0x%02X" % r["source"]),
                "  moving          : %s" % r["moving"],
            ]))
        return

    if head == "checks":
        # WHAT THE NODE MEASURED, from the frames on the WIRE.
        now = time.monotonic()
        if not state.checks.running:
            say("The system test is not running. Use: session begin.")
            return
        lines = ["-" * 66,
                 "The automatic system test, AS THE NODE READ IT OFF THE BUS."]
        for sub, st in sorted(state.checks.states(now).items()):
            lines.append("  slot %2d  %-8s %s"
                         % (SYSTEM_TEST_SLOT_BASE + int(sub),
                            sub.name, STEP_STATES[int(st)]))
        lines.append("  operator_input_requested : %s"
                     % state.checks.operator_input_requested(now))
        say("\n".join(lines))
        return

    if head == "systest":
        silent = SYSTEM_TEST.report()
        lines = ["-" * 66,
                 "The automatic system test. The rig EMULATES every source.",
                 "  silent : %s" % (", ".join(silent) if silent
                                    else "nothing. Every subsystem answers")]
        for can_id, _data, label in SYSTEM_TEST.frames():
            lines.append("  0x%06X  %s" % (can_id, label))
        if silent:
            lines.append("  NOTE: a silenced subsystem sends NOTHING, which "
                         "is what a dead one looks like.")
        say("\n".join(lines))
        return

    if head == "cycle":
        if len(parts) < 2:
            say("Use: cycle <step>, 0 to %d. It moves that step to the next "
                "of PENDING ACTIVE GOOD FAULT." % (SPECTER_STEPS_IN_USE - 1))
            return
        step = step_index(parts[1]) if parts[1].lower().startswith("s") \
            else None
        if step is None:
            try:
                step = int(parts[1])
            except ValueError:
                step = None
        if step is None or not 0 <= step < SPECTER_STEPS_IN_USE:
            say("The step must be 0 to %d." % (SPECTER_STEPS_IN_USE - 1))
            return
        name = state.cycle_one_step(step)
        show_outgoing(state, "Cycle: step %d is now %s." % (step, name))
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
    parser.add_argument("--cycle-step", type=int, default=-1,
                        help="Walk ONE step through all four wire states, "
                             "PENDING ACTIVE GOOD FAULT, for ever. This is a "
                             "RENDERING PROOF, not a session: the node never "
                             "reports FAULT while a session runs. "
                             "-1 means do not cycle.")
    parser.add_argument("--cycle-period", type=float, default=2.0,
                        help="Seconds between the states of --cycle-step. "
                             "Long enough to read the screen. Default 2.")

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

    # REFUSE A BAD ARGUMENT BEFORE ANY SOCKET IS OPENED. A check that runs
    # after the sockets are up makes the operator wait for a failure the
    # command line already showed, and it leaves two open sockets on the way
    # out. Argparse cannot express these two rules, so they live here, first.
    if args.cycle_step >= 0 and not 0 <= args.cycle_step < SPECTER_STEPS_IN_USE:
        say("ERROR: --cycle-step must be 0 to %d. It names a checklist step."
            % (SPECTER_STEPS_IN_USE - 1))
        return 2
    if args.cycle_step >= 0 and args.cycle_period <= 0:
        say("ERROR: --cycle-period must be above 0 seconds.")
        return 2

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

    global TELEMETRY, SYSTEM_TEST, RUDDER, ACTUATORS
    SYSTEM_TEST = SystemTestEmulator()
    RUDDER = RudderModel()
    ACTUATORS = ActuatorModel()
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
    system_test_thread = threading.Thread(
        target=system_test_worker,
        args=(tx_sock, tx_lock, SYSTEM_TEST, stop_event, link),
        name="system-test", daemon=True)
    monitor_sock = open_monitor_socket(args.interface)
    monitor_thread = threading.Thread(
        target=system_test_monitor,
        args=(monitor_sock, state, stop_event, RUDDER, ACTUATORS),
        name="system-test-monitor", daemon=True)
    rudder_thread = threading.Thread(
        target=rudder_worker,
        args=(tx_sock, tx_lock, RUDDER, stop_event),
        name="rudder", daemon=True)
    actuator_thread = threading.Thread(
        target=actuator_worker,
        args=(tx_sock, tx_lock, ACTUATORS, stop_event),
        name="actuators", daemon=True)

    rx_thread.start()
    tx_thread.start()
    if telemetry_thread is not None:
        telemetry_thread.start()
    system_test_thread.start()
    monitor_thread.start()
    rudder_thread.start()
    actuator_thread.start()

    if args.duration > 0:
        def stop_later():
            stop_event.wait(args.duration)
            stop_event.set()
        threading.Thread(target=stop_later, daemon=True).start()

    if args.cycle_step >= 0:
        def cycle_later():
            """Drive one step round the four states, for ever.

            It never stops on its own. A rendering proof that ended after one
            pass would leave the bench looking at whatever state it stopped
            in, and the person watching would have to restart the rig to see
            the next one.
            """
            while not stop_event.is_set():
                stop_event.wait(args.cycle_period)
                if stop_event.is_set():
                    return
                name = state.cycle_one_step(args.cycle_step)
                say("Cycle: step %d is now %s." % (args.cycle_step, name))
        threading.Thread(target=cycle_later, daemon=True).start()
        say("Cycling step %d through PENDING ACTIVE GOOD FAULT every %.1f s."
            % (args.cycle_step, args.cycle_period))
        say("  This is a RENDERING PROOF. A live node never sends FAULT.")

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
