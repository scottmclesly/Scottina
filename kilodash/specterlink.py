"""SPECTER bench-link model for the SPECTER screen (RX-only, headless,
testable).

Everything the SPECTER screen shows lives here, decoupled from PIL and from
the socket: the two periodic frames of the preflight link, their liveness,
their measured period, their sequence continuity, and the seven decoded
group states.

The link is one bench bus with two periodic 8-byte frames, both 500 ms,
both little-endian, both the same layout:

    can_id 0x240220   display heartbeat   the Screen talks
    can_id 0x248021   node status         the boat side talks

    byte 0 protocol_version   byte 1 session_id   byte 2 sequence
    byte 3 flags   bit0 preflight_incomplete, bits1-3 active_group,
                   bit4 session_active
    byte 4 step_states_lo     g1 bits0-1, g2 bits2-3, g3 bits4-5, g4 bits6-7
    byte 5 step_states_hi     g5 bits0-1, g6 bits2-3, g7 bits4-5
    byte 6 reserved           byte 7 checklist_version

    states  0 PENDING  1 ACTIVE  2 GOOD  3 FAULT
    groups  g1..g7 = S P E1 C T E2 R

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

NOMINAL_PERIOD_S = 0.5
STALE_S = 2.0                    # no frame for this long = STALE
PERIOD_WINDOW = 40               # samples kept for avg/min/max

PENDING, ACTIVE, GOOD, FAULT = 0, 1, 2, 3
STATE_NAMES = {PENDING: "PENDING", ACTIVE: "ACTIVE", GOOD: "GOOD",
               FAULT: "FAULT"}
STATE_INITIALS = {PENDING: "P", ACTIVE: "A", GOOD: "G", FAULT: "F"}
GROUP_LETTERS = ("S", "P", "E1", "C", "T", "E2", "R")


def decode(data):
    """Decode the 8 shared bytes. Return every field, or None if not 8 bytes.

    Both frames carry the same layout, so one decoder serves both
    directions — which is the point of the format and the reason the screen
    can show them side by side.
    """
    if data is None or len(data) != 8:
        return None
    flags = data[3]
    lo, hi = data[4], data[5]
    return {
        "protocol_version": data[0],
        "session_id": data[1],
        "sequence": data[2],
        "flags": flags,
        "preflight_incomplete": flags & 0x01,
        "active_group": (flags >> 1) & 0x07,
        "session_active": (flags >> 4) & 0x01,
        "step_states_lo": lo,
        "step_states_hi": hi,
        "states": [lo & 0x03, (lo >> 2) & 0x03, (lo >> 4) & 0x03,
                   (lo >> 6) & 0x03, hi & 0x03, (hi >> 2) & 0x03,
                   (hi >> 4) & 0x03],
        "reserved": data[6],
        "checklist_version": data[7],
    }


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
        fields = decode(data)
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
