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
    byte 4 display_state      bytes 5 to 7 reserved

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

DISPLAY_CAN_ID = 0x240220        # the Screen's display heartbeat
NODE_CAN_ID = 0x248021           # the boat side's node status

NOMINAL_PERIOD_S = 0.5           # the display frame. The status frame is 0.2
NODE_PERIOD_S = 0.2              # the status frame, 200 ms
STALE_S = 2.0                    # no frame for this long = STALE
PERIOD_WINDOW = 40               # samples kept for avg/min/max

PENDING, ACTIVE, GOOD, FAULT = 0, 1, 2, 3
STATE_NAMES = {PENDING: "PENDING", ACTIVE: "ACTIVE", GOOD: "GOOD",
               FAULT: "FAULT"}
STATE_INITIALS = {PENDING: "P", ACTIVE: "A", GOOD: "G", FAULT: "F"}
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
        "states": roll_up(steps),
    }


DECODERS = {
    DISPLAY_CAN_ID: decode_display,
    NODE_CAN_ID: decode_node,
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

    def __init__(self, iface, display_link, node_link):
        self.iface = iface
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
        return struct.pack("=IIII",
                           DISPLAY_CAN_ID | CAN_EFF_FLAG, mask,
                           NODE_CAN_ID | CAN_EFF_FLAG, mask)

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
