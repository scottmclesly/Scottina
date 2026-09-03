"""SPECTER bench-link model for the SPECTER screen (RX-only, headless,
testable).

Everything the SPECTER screen shows lives here, decoupled from PIL and from
the socket: the two periodic frames of the preflight link, their liveness,
their measured period, their sequence continuity, and the seven decoded
group states.

The link is one bench bus with two periodic 8-byte frames, both
little-endian. They no longer share one layout. Each id has its own:

    can_id 0x240220   display frame   the Screen talks     500 ms
    byte 0 liveness_counter   byte 1 event_sequence
    byte 2 event_type         byte 3 step_index
    byte 4 display_state      byte 5 event_param
    bytes 6 to 7 BUILD IDENTITY: flags, and the low commit byte

    can_id 0x240320  handshake request   the Screen asks    1000 ms
    byte 0 protocol   bytes 1 to 2 firmware identity
    byte 3 step_capacity   byte 4 BUILD FLAGS   bytes 5 to 7 reserved

    can_id 0x248021   status frame    the boat side talks  200 ms
    byte 0 flags   bit0 veto, bit1 operator_input_requested,
                   bits2-3 vessel_mode, bits4-7 event_echo
    bytes 1 to 6   24 step states, two bits each, step N at
                   byte 1 + (N >> 2), shift (N & 3) * 2
    byte 7         four spare step slots

    states  0 PENDING  1 ACTIVE  2 GOOD  3 FAULT
    groups  g1..g7 = S P E1 C T E2 R

THE SEVEN GROUPS ARE A ROLLUP, NOT THE WIRE
The wire carries 13 steps at indices 0 to 12. The seven groups are a
display-side rollup of those steps and they are not on the bus. Each group
covers a fixed set of step indices and the seven sets partition 0 to 12
exactly once. The screen keeps its seven-cell strip unchanged.

Only the status frame carries step states, so only the node link reports
`states`. The display frame carries none and reports None, which the screen
already handles.

Sequence continuity is available on the DISPLAY link only. Its byte 0 is a
liveness counter that steps once per frame. The status frame carries no
counter, so the node link reports `sequence` None and counts no gap. Its
liveness is the measured period and `alive`, not continuity.

Scope (CAN-N2K-Split-TODO, hard constraint): **diagnostics only — this
module and the SPECTER screen construct no TX frames and never write to the
bus.** The reader socket is used exclusively to recv(). This screen watches
BOTH directions of the link, but it speaks neither of them: the node status
it displays is the one another node actually put on the wire, observed, not
echoed. The bench simulator that publishes 0x248021 is separate bench
equipment, run by hand and imported by no Scottina package; the one runtime
TX carve-out in the whole system remains n2k/node.py.
tests/test_txscan.py asserts tree-wide that no send-shaped call exists here,
and tests/test_specterlink.py is the per-module reject pass.
"""

import collections
import re
import socket
import struct
import subprocess
import threading
import time

from .busmon import (CAN_EFF_FLAG, CAN_EFF_MASK, CAN_RTR_FLAG, FRAME_SIZE,
                     parse_frame)

DISPLAY_CAN_ID = 0x240220        # the Screen's display frame
NODE_CAN_ID = 0x248021           # the boat side's status frame
HS_REQUEST_CAN_ID = 0x240320     # the Screen asks for a handshake
HS_RESPONSE_CAN_ID = 0x248121    # the boat side answers

#: The 13 steps, in the order the wire numbers them. The panel shows a name
#: against each state, because "step 7 FAULT" means nothing across a bench and
#: "E-STOP test FAULT" means everything.
STEP_NAMES = (
    "automatic systems test",
    "steering",
    "engine trim",
    "seakeeper ride trim",
    "payload hatch",
    "engine ventilation",
    "power ACC mode test",
    "E-STOP test",
    "comms test",
    "telemetry",
    "engine start",
    "engine health",
    "navigation lights",
)

#: Byte 4 of the display frame.
DISPLAY_STATES = ("BLOCKED", "PIN", "RUNNING", "STALE", "FAULT REVIEW")

#: Byte 2 of the display frame.
EVENT_TYPES = ("NONE", "SESSION BEGIN", "STEP BEGIN", "STEP CONFIRM",
               "STEP RERUN", "STEP ABORT", "SESSION ABORT", "SESSION COMPLETE",
               "ACTUATE UP", "ACTUATE DOWN", "ACTUATE STOP")

#: The three events that carry a parameter in byte 5. Nothing else does.
ACTUATE_EVENTS = (8, 9, 10)

#: Byte 5, on an actuate event only. Which payload hatch it drives.
ACTUATE_TARGETS = ("BOTH", "PORT", "STARBOARD")

#: Byte 1 of the handshake response.
HS_RESULTS = ("ACCEPT", "REJECT PROTOCOL", "REJECT CAPACITY",
              "REJECT NOT READY")


#: What the bench rig transmits, for the panel to NAME. DISPLAY ONLY.
#:
#: This holds a label and a rate. It holds no byte layout, no scale and no
#: frame, so this package still constructs nothing: bench gear stays bench
#: gear, and the tree-wide safety test keeps that true. The authoritative
#: table, with the layouts and the bench values, lives with the rig itself.
#: (tid, source, label, hz)
TELEMETRY_SENT = (
    (0x5000, 0x83, "fuel level %", 1),
    (0x1805, 0x82, "alternator potential V", 1),
    (0x1800, 0x82, "engine rpm", 10),
    (0x1801, 0x82, "engine tilt/trim deg", 1),
    (0x1803, 0x82, "engine oil temperature degC", 1),
    (0x1804, 0x82, "engine temperature degC", 1),
    (0x1806, 0x82, "fuel rate m^3/h", 1),
    (0x1807, 0x82, "engine hours s", 1),
    (0x180A, 0x82, "engine load %", 1),
    (0x180B, 0x82, "engine torque %", 1),
    (0x180F, 0x82, "transmission oil temperature degC", 10),
)


def step_name(index):
    """Return the name of a step index, or a plain label for a spare slot."""
    if 0 <= index < len(STEP_NAMES):
        return STEP_NAMES[index]
    if index == SHORE_LINK_SLOT:
        return "shore operator link"
    return "spare %d" % index


def display_state_name(value):
    if 0 <= value < len(DISPLAY_STATES):
        return DISPLAY_STATES[value]
    return "UNDEFINED %d" % value


def event_type_name(value):
    if 0 <= value < len(EVENT_TYPES):
        return EVENT_TYPES[value]
    return "UNDEFINED %d" % value


def actuate_target_name(value):
    """Name the hatch an actuate event drives. Byte 5."""
    if 0 <= value < len(ACTUATE_TARGETS):
        return ACTUATE_TARGETS[value]
    return "UNDEFINED %d" % value


def event_detail(event_type, event_param):
    """Name one event, with its target when it carries one.

    Only the three actuate events use byte 5. Every other event leaves it 0,
    so showing it on them would invent a meaning the wire does not have.
    """
    name = event_type_name(event_type)
    if event_type in ACTUATE_EVENTS:
        return "%s %s" % (name, actuate_target_name(event_param))
    return name


def hs_result_name(value):
    if 0 <= value < len(HS_RESULTS):
        return HS_RESULTS[value]
    return "UNDEFINED %d" % value


#: Byte 4 of the handshake request. EVERY BIT MEANS SOMETHING IS WRONG WITH
#: THE IMAGE ON THE UNIT.
#:
#: The display was flashed from a working tree that was not origin. Nothing
#: on the wire said so, and three unrelated-looking symptoms all came from
#: that one cause. This byte is what makes it one line instead of a day.
BUILD_DIRTY = 0x01
BUILD_BEHIND = 0x02
BUILD_ID_UNKNOWN = 0x04
BUILD_REMOTE_STALE = 0x08

#: Bit 7. THIS IMAGE CARRIES AN IDENTITY AT ALL.
#:
#: WITHOUT IT, ZERO IS AMBIGUOUS. An image built before the identity existed
#: sends 0x00 because these bytes were reserved, and 0x00 also means "clean
#: and current". The bench read a four-day-old image as CLEAN, which is the
#: exact false reassurance this mechanism exists to prevent.
BUILD_PRESENT = 0x80

_BUILD_FLAGS = (
    (BUILD_DIRTY, "DIRTY"),
    (BUILD_BEHIND, "BEHIND"),
    (BUILD_ID_UNKNOWN, "ID UNKNOWN"),
    (BUILD_REMOTE_STALE, "REMOTE STALE"),
)


def build_flag_names(flags):
    """Name every build flag set. Empty means the image is clean AND current.

    An image with no presence bit predates the identity, so it can say
    nothing about itself. That is reported as NO IDENTITY, never as clean.
    """
    if not flags & BUILD_PRESENT:
        return ["NO IDENTITY: THIS IMAGE PREDATES THE BUILD STAMP"]
    return [name for bit, name in _BUILD_FLAGS if flags & bit]


def build_identity_short(commit_low, flags):
    """The build line for the DISPLAY FRAME, which carries one commit byte.

    The commit reads `..ef`. THE DOTS ARE DELIBERATE: they say the rest is
    not on this frame, rather than padding it with zeros that would read as
    a real commit.
    """
    commit = "..%02x" % commit_low
    if (flags & BUILD_ID_UNKNOWN) or not (flags & BUILD_PRESENT):
        commit = "unknown"
    names = build_flag_names(flags)
    if not names:
        return "commit %s CLEAN" % commit
    return "commit %s *** %s ***" % (commit, ", ".join(names))


def build_identity_text(major, minor, flags):
    """One line naming the image on the unit and what is wrong with it.

    READ THIS BEFORE BELIEVING ANY SYMPTOM ON THE DISPLAY. A stale image
    explains all of them at once, and chasing them one at a time does not.
    """
    commit = "%02x%02x" % (major, minor)
    if (flags & BUILD_ID_UNKNOWN) or not (flags & BUILD_PRESENT):
        commit = "unknown"
    names = build_flag_names(flags)
    if not names:
        return "commit %s CLEAN" % commit
    return "commit %s *** %s ***" % (commit, ", ".join(names))


def decode_hs_request(data):
    """Decode the handshake request, 0x240320.

    Bytes 1 and 2 carry the firmware identity: the low 16 bits of the git
    commit the image was built from. 0x0000 means the image was built with no
    identity and cannot say where it came from.
    """
    if data is None or len(data) != 8:
        return None
    fw = (data[1] << 8) | data[2]
    flags = data[4]
    return {
        "protocol_version": data[0],
        "firmware_id": fw,
        "firmware_text": ("none" if fw == 0 else "0x%04X" % fw),
        "step_capacity": data[3],
        # BYTE 4 IS THE BUILD FLAGS. It was reserved and always sent 0.
        "build_flags": flags,
        "build_text": build_identity_text(data[1], data[2], flags),
        "build_clean": flags == BUILD_PRESENT,
        # LinkState reads these on every frame. The handshake pair
        # carries no counter and no step states.
        "sequence": None,
        "states": None,
    }


def decode_hs_response(data):
    """Decode the handshake response, 0x248121."""
    if data is None or len(data) != 8:
        return None
    return {
        "protocol_version": data[0],
        "result": data[1],
        "result_text": hs_result_name(data[1]),
        "checklist_id": data[2] | (data[3] << 8),
        "session_id": data[4] | (data[5] << 8),
        "step_count": data[6],
        # LinkState reads these on every frame. The handshake pair
        # carries no counter and no step states.
        "sequence": None,
        "states": None,
    }

NOMINAL_PERIOD_S = 0.5           # the display frame. The status frame is 0.2
NODE_PERIOD_S = 0.2              # the status frame, 200 ms
STALE_S = 2.0                    # no frame for this long = STALE
PERIOD_WINDOW = 40               # samples kept for avg/min/max

PENDING, ACTIVE, GOOD, FAULT = 0, 1, 2, 3
STATE_NAMES = {PENDING: "PENDING", ACTIVE: "ACTIVE", GOOD: "GOOD",
               FAULT: "FAULT"}
STATE_INITIALS = {PENDING: "P", ACTIVE: "A", GOOD: "G", FAULT: "F"}

#: THE AUTOMATIC SYSTEM TEST, packed slots 14 to 17. NOT checklist steps.
#:
#: Slot 13 set the precedent: a slot that is never walked, never started and
#: never confirmed. The checklist is still 13 steps, 0 to 12.
SYSTEM_TEST_SLOT_BASE = 14
SYSCHECK_NAMES = ("TOCAN", "ROS", "MAVLINK", "EGES")
SYSTEM_TEST_STEP = 0             # the step these four results belong to
GROUP_LETTERS = ("S", "P", "E1", "C", "T", "E2", "R")


STEPS_IN_USE = 13                # steps 0 to 12 on the wire
STEP_CAPACITY = 24               # packed slots the status frame carries
SHORE_LINK_SLOT = 13             # not a step. The shore link only

# Each group covers a fixed set of step indices. The seven sets partition
# 0 to 12 exactly once, so every step belongs to one group and no step
# belongs to two. This is a rollup for the strip. It is not on the wire.
GROUP_STEPS = (
    (1, 2, 3),
    (4,),
    (0, 6, 12),
    (8,),
    (9,),
    (7,),
    (5, 10, 11),
)


def unpack_steps(data):
    """Unpack the 24 packed step states from a whole status frame."""
    return [(data[1 + (n >> 2)] >> ((n & 3) * 2)) & 0x03
            for n in range(STEP_CAPACITY)]


def roll_up(steps):
    """Roll the 13 steps up into the seven group states.

    A group is GOOD when every step under it is GOOD. It is FAULT when any
    step under it is FAULT. If neither holds it shows the state of the
    LEAST ADVANCED step under it, running PENDING, ACTIVE, GOOD.
    """
    out = []
    for covered_indices in GROUP_STEPS:
        covered = [steps[i] for i in covered_indices]
        if all(state == GOOD for state in covered):
            out.append(GOOD)
        elif any(state == FAULT for state in covered):
            out.append(FAULT)
        else:
            out.append(min(covered))
    return out


def decode_display(data):
    """Decode the display frame, 0x240220. Return every field or None."""
    if data is None or len(data) != 8:
        return None
    return {
        "liveness_counter": data[0],
        "sequence": data[0],          # the counter the screen tracks
        "event_sequence": data[1] & 0x0F,
        "event_type": data[2],
        "step_index": data[3],
        "display_state": data[4],
        # BYTES 6 AND 7 ARE THE BUILD IDENTITY. The handshake request carries
        # it too, but that goes out only WHILE THE DISPLAY IS BLOCKED. A unit
        # sat blocked for days and sent none, so nothing on the bus said
        # which image it ran. THIS frame goes out in every state.
        "build_flags": data[6],
        "build_commit_low": data[7],
        "build_text": build_identity_short(data[7], data[6]),
        "build_clean": data[6] == BUILD_PRESENT,
        # BYTE 5 IS THE EVENT PARAMETER. It names which payload hatch an
        # actuate event drives: 0 both, 1 port, 2 starboard. Every other
        # event leaves it 0.
        "event_param": data[5],
        "states": None,               # the display frame carries no steps
    }


def decode_node(data):
    """Decode the status frame, 0x248021. Return every field or None."""
    if data is None or len(data) != 8:
        return None
    flags = data[0]
    steps = unpack_steps(data)
    return {
        "flags": flags,
        "veto": flags & 0x01,
        "preflight_incomplete": flags & 0x01,   # the veto, by its old name
        "operator_input_requested": (flags >> 1) & 0x01,
        "vessel_mode": (flags >> 2) & 0x03,
        "event_echo": (flags >> 4) & 0x0F,
        "sequence": None,             # the status frame carries no counter
        "steps": steps[:STEPS_IN_USE],
        "shore_link": steps[SHORE_LINK_SLOT],
        # THE FOUR AUTOMATIC SYSTEM TEST RESULTS. They were unpacked and then
        # thrown away, so the one screen a person watches while debugging the
        # system test showed nothing about it at all.
        "system_test": steps[SYSTEM_TEST_SLOT_BASE:SYSTEM_TEST_SLOT_BASE + 4],
        "states": roll_up(steps),
    }


def syscheck_fault(system_test):
    """Name the FIRST failing subsystem, or None when none has failed.

    First in slot order, so the panel names the same one every time.

    IT DOES NOT BUILD THE OPERATOR LINE. The words the display shows live in
    the firmware, in `specter_system_test.c`, because no string crosses the
    bus. A second copy of them here would drift from the glass, and the whole
    point of this panel is to say what the DISPLAY is being told.
    """
    for index, value in enumerate(system_test or ()):
        if value == FAULT:
            return SYSCHECK_NAMES[index]
    return None


def syscheck_reported(system_test):
    """True when every check has reported, pass or fail.

    A check still PENDING is not a pass. This is the same rule the node and
    the firmware both obey, and it is the reason the panel must never read
    "ready" off an absence.
    """
    if not system_test:
        return False
    return all(value != PENDING for value in system_test)


DECODERS = {
    DISPLAY_CAN_ID: decode_display,
    NODE_CAN_ID: decode_node,
    HS_REQUEST_CAN_ID: decode_hs_request,
    HS_RESPONSE_CAN_ID: decode_hs_response,
}


def decode(data, can_id=NODE_CAN_ID):
    """Decode one frame by its id. Return every field, or None.

    The two ids no longer share a layout, so the id chooses the decoder.
    """
    decoder = DECODERS.get(can_id)
    if decoder is None:
        return None
    return decoder(data)


class LinkState:
    """Liveness and continuity for ONE periodic id.

    The reader thread is the only writer; the screen only ever calls
    snapshot(), so rendering can never block reception.
    """

    def __init__(self, can_id, label):
        self.can_id = can_id
        self.label = label
        self._lock = threading.Lock()
        self.frames = 0
        self.bad_dlc = 0
        self.gaps = 0
        self.repeats = 0
        self.first_rx = None
        self.last_rx = None
        self.last_seq = None
        self.last_data = None
        self.last_fields = None
        self._periods = collections.deque(maxlen=PERIOD_WINDOW)

    def ingest(self, ts, data):
        fields = decode(data, self.can_id)
        if fields is None:
            with self._lock:
                self.bad_dlc += 1
            return
        with self._lock:
            if self.last_rx is not None:
                self._periods.append((ts - self.last_rx) * 1000.0)
            else:
                self.first_rx = ts
            seq = fields["sequence"]
            # The status frame carries no counter, so it reports None. Do
            # not count a gap against a frame that has nothing to count.
            if seq is not None:
                if self.last_seq is not None:
                    if seq == self.last_seq:
                        self.repeats += 1
                    elif seq != (self.last_seq + 1) & 0xFF:
                        self.gaps += 1
                self.last_seq = seq
            self.last_rx = ts
            self.frames += 1
            self.last_data = bytes(data)
            self.last_fields = fields

    def snapshot(self, now=None):
        """A JSON-safe view. `alive` is the whole point of the screen."""
        now = time.time() if now is None else now
        with self._lock:
            periods = list(self._periods)
            last_rx = self.last_rx
            fields = dict(self.last_fields) if self.last_fields else None
            data = self.last_data
            out = {
                "can_id": self.can_id,
                "label": self.label,
                "frames": self.frames,
                "bad_dlc": self.bad_dlc,
                "gaps": self.gaps,
                "repeats": self.repeats,
                "sequence": self.last_seq,
            }
        age = None if last_rx is None else max(0.0, now - last_rx)
        out["age"] = age
        out["alive"] = age is not None and age < STALE_S
        out["period_ms"] = periods[-1] if periods else None
        out["period_avg_ms"] = sum(periods) / len(periods) if periods else None
        out["period_min_ms"] = min(periods) if periods else None
        out["period_max_ms"] = max(periods) if periods else None
        out["fields"] = fields
        out["data"] = data
        out["states"] = fields["states"] if fields else None
        return out


class SpecterReader:
    """Background RX loop: one raw SocketCAN socket, filtered to the two
    SPECTER ids, recv-only, feeding two LinkStates. Never sends.

    Dies cleanly (with .error set) when the iface drops; the screen restarts
    it when the iface returns — the same contract as busmon.RxReader.

    The kernel filter mask deliberately omits CAN_ERR_FLAG. A filter that
    sets it is moved to the error-frame list, where it never matches a data
    frame — the bench lost an afternoon to that once already.
    """

    def __init__(self, iface, display_link, node_link,
                 hs_request_link=None, hs_response_link=None):
        self.iface = iface
        self.hs_request = hs_request_link or LinkState(HS_REQUEST_CAN_ID,
                                                       "HS-REQ")
        self.hs_response = hs_response_link or LinkState(HS_RESPONSE_CAN_ID,
                                                         "HS-RSP")
        self.display = display_link
        self.node = node_link
        self.error = None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    @property
    def alive(self):
        return self._t.is_alive()

    def stop(self):
        self._stop.set()
        self._t.join(timeout=1.5)

    def _filter(self):
        mask = CAN_EFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG
        return struct.pack("=IIIIIIII",
                           DISPLAY_CAN_ID | CAN_EFF_FLAG, mask,
                           NODE_CAN_ID | CAN_EFF_FLAG, mask,
                           HS_REQUEST_CAN_ID | CAN_EFF_FLAG, mask,
                           HS_RESPONSE_CAN_ID | CAN_EFF_FLAG, mask)

    def _run(self):
        try:
            s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        except (AttributeError, OSError) as e:
            self.error = f"socket: {e}"
            return
        try:
            try:
                s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                             self._filter())
            except OSError as e:
                self.error = f"filter: {e}"
                return
            s.settimeout(0.25)
            s.bind((self.iface,))
            while not self._stop.is_set():
                try:
                    buf = s.recv(FRAME_SIZE)
                except socket.timeout:
                    continue
                except OSError as e:
                    self.error = f"{self.iface}: {e.strerror or e}"
                    return
                parsed = parse_frame(buf)
                if not parsed:
                    continue
                cid, _ext, rtr, data = parsed
                if rtr:
                    continue
                if cid == DISPLAY_CAN_ID:
                    self.display.ingest(time.time(), data)
                elif cid == NODE_CAN_ID:
                    self.node.ingest(time.time(), data)
                elif cid == HS_REQUEST_CAN_ID:
                    self.hs_request.ingest(time.time(), data)
                elif cid == HS_RESPONSE_CAN_ID:
                    self.hs_response.ingest(time.time(), data)
        except OSError as e:
            self.error = f"{self.iface}: {e.strerror or e}"
        finally:
            s.close()


_INT = re.compile(r"\d+")


def parse_can_link(text):
    """Pull the bus-health fields out of `ip -details -statistics link` text.

    Split from read_can_link so it is testable without an interface.
    """
    info = {"present": False, "if_state": None, "can_state": None,
            "berr_tx": None, "berr_rx": None, "bitrate": None,
            "restarts": None, "bus_errors": None, "arbit_lost": None,
            "error_warn": None, "error_pass": None, "bus_off": None,
            "rx": None, "tx": None}
    lines = (text or "").splitlines()
    if not lines:
        return info
    info["present"] = True

    m = re.search(r"\bstate ([A-Z_-]+)", lines[0])
    info["if_state"] = m.group(1) if m else None
    m = re.search(r"can state (\S+) \(berr-counter tx (\d+) rx (\d+)\)", text)
    if m:
        info["can_state"] = m.group(1)
        info["berr_tx"] = int(m.group(2))
        info["berr_rx"] = int(m.group(3))
    m = re.search(r"\bbitrate (\d+)", text)
    if m:
        info["bitrate"] = int(m.group(1))

    for i, line in enumerate(lines):
        if "re-started" in line and "bus-errors" in line and i + 1 < len(lines):
            nums = [int(v) for v in _INT.findall(lines[i + 1])]
            if len(nums) >= 6:
                (info["restarts"], info["bus_errors"], info["arbit_lost"],
                 info["error_warn"], info["error_pass"],
                 info["bus_off"]) = nums[:6]
            break

    for i, line in enumerate(lines):
        head = line.strip()
        if head.startswith("RX:") and i + 1 < len(lines):
            nums = [int(v) for v in _INT.findall(lines[i + 1])]
            if len(nums) >= 6:
                info["rx"] = nums[:6]      # bytes pkts errs drop missed mcast
        elif head.startswith("TX:") and i + 1 < len(lines):
            nums = [int(v) for v in _INT.findall(lines[i + 1])]
            if len(nums) >= 6:
                info["tx"] = nums[:6]      # bytes pkts errs drop carrier coll
    return info


def read_can_link(iface):
    """Read one interface's health. Never raises — a missing iface reads as
    absent, which is a state the screen draws, not an error it handles."""
    try:
        r = subprocess.run(["ip", "-details", "-statistics", "link", "show",
                            iface], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return parse_can_link("")
    if r.returncode != 0:
        return parse_can_link("")
    return parse_can_link(r.stdout)
