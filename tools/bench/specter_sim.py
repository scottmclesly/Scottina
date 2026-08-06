#!/usr/bin/env python3
"""SPECTER bench simulator. It is the boat side of the bench bus.

It does two things at the same time on one SocketCAN interface:

RECEIVE  the display heartbeat, TID 0x2402 from source 0x20.
         can_id = 0x240220, extended, 8 bytes.
         It decodes and prints every field.
         It warns if no heartbeat arrives for 3 seconds.

TRANSMIT the node state summary, TID 0x2480 from source 0x21.
         can_id = 0x248021, extended, 8 bytes.
         It sends every 500 ms.

The simulator holds the authoritative step states. Every group starts at
PENDING.

The decode logic and the socket filter mask come from
specter_hb_decode.py. Do not put CAN_ERR_FLAG in the filter mask. The
kernel moves such a filter to the error-frame list. Then it never matches
a data frame.

This file is standalone. It holds its own constants. It does not import
specter_pkg. It does not read or write tocan_ids.py or ros2_topics.py.

Console commands:
  3 GOOD        set group 3 to GOOD
  g3 GOOD       the same
  E1 GOOD       the same, by group letter
  all PENDING   set every group
  show          print the state and the next frame
  help          print the commands
  quit          stop and close the sockets

Use: python3 specter_sim.py can0
"""

import argparse
import errno
import select
import signal
import socket
import struct
import sys
import threading
import time

DISPLAY_SOURCE = 0x20
NODE_SOURCE = 0x21

TID_DISPLAY_HEARTBEAT = 0x2402
TID_NODE_STATE_SUMMARY = 0x2480

RX_CAN_ID = (TID_DISPLAY_HEARTBEAT << 8) | DISPLAY_SOURCE
TX_CAN_ID = (TID_NODE_STATE_SUMMARY << 8) | NODE_SOURCE

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_EFF_MASK = 0x1FFFFFFF

CAN_FRAME_FMT = "<IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FMT)

PENDING, ACTIVE, GOOD, FAULT = 0, 1, 2, 3
STEP_STATES = {0: "PENDING", 1: "ACTIVE", 2: "GOOD", 3: "FAULT"}
STATE_BY_NAME = {name: value for value, name in STEP_STATES.items()}

GROUP_KEYS = ["g1", "g2", "g3", "g4", "g5", "g6", "g7"]
GROUP_LETTERS = ["S", "P", "E1", "C", "T", "E2", "R"]

TX_PERIOD_S = 0.5
STALE_LIMIT_S = 3.0

print_lock = threading.Lock()


def say(text):
    """Print one block of text. Keep threads from mixing their output."""
    with print_lock:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def group_label(index):
    """Give the label for group index 1 to 7."""
    return "%s (%s)" % (GROUP_KEYS[index - 1], GROUP_LETTERS[index - 1])


class SimState:
    """The authoritative step states and the transmit header fields."""

    def __init__(self, protocol_version, session_id, checklist_version):
        self.lock = threading.Lock()
        self.states = [PENDING] * 7
        self.protocol_version = protocol_version
        self.session_id = session_id
        self.checklist_version = checklist_version
        self.sequence = 0

    def snapshot(self):
        with self.lock:
            return list(self.states)

    def set_group(self, index, value):
        """Set group index 1 to 7. Return True if the value changed."""
        with self.lock:
            if self.states[index - 1] == value:
                return False
            self.states[index - 1] = value
            return True

    def set_all(self, value):
        """Set every group. Return True if any value changed."""
        with self.lock:
            changed = any(state != value for state in self.states)
            self.states = [value] * 7
            return changed

    def build_frame(self, advance):
        """Build the 8 transmit bytes. Return the bytes and the fields."""
        with self.lock:
            states = list(self.states)
            if advance:
                self.sequence = (self.sequence + 1) & 0xFF
            sequence = self.sequence
            protocol_version = self.protocol_version
            session_id = self.session_id
            checklist_version = self.checklist_version

        step_states_lo = (states[0] | (states[1] << 2)
                          | (states[2] << 4) | (states[3] << 6))
        step_states_hi = (states[4] | (states[5] << 2) | (states[6] << 4))

        preflight_incomplete = 0 if all(s == GOOD for s in states) else 1

        active_group = 0
        for index, state in enumerate(states, start=1):
            if state == ACTIVE:
                active_group = index
                break

        session_active = 1

        flags = (preflight_incomplete
                 | (active_group << 1)
                 | (session_active << 4))

        data = bytes([protocol_version, session_id, sequence, flags,
                      step_states_lo, step_states_hi, 0, checklist_version])

        fields = {
            "protocol_version": protocol_version,
            "session_id": session_id,
            "sequence": sequence,
            "flags": flags,
            "preflight_incomplete": preflight_incomplete,
            "active_group": active_group,
            "session_active": session_active,
            "step_states_lo": step_states_lo,
            "step_states_hi": step_states_hi,
            "reserved": 0,
            "checklist_version": checklist_version,
            "states": states,
        }
        return data, fields


def open_rx_socket(interface):
    """Open a receive socket. Filter for the display heartbeat only."""
    sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    can_filter = struct.pack(
        "=II",
        RX_CAN_ID | CAN_EFF_FLAG,
        CAN_EFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG,
    )
    sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, can_filter)
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


def decode_heartbeat(data):
    """Decode the 8 heartbeat bytes. Return a dict of every field."""
    flags = data[3]
    step_states_lo = data[4]
    step_states_hi = data[5]
    return {
        "protocol_version": data[0],
        "session_id": data[1],
        "sequence": data[2],
        "flags": flags,
        "preflight_incomplete": flags & 0x01,
        "active_group": (flags >> 1) & 0x07,
        "session_active": (flags >> 4) & 0x01,
        "flags_reserved": (flags >> 5) & 0x07,
        "step_states_lo": step_states_lo,
        "step_states_hi": step_states_hi,
        "states": [
            step_states_lo & 0x03,
            (step_states_lo >> 2) & 0x03,
            (step_states_lo >> 4) & 0x03,
            (step_states_lo >> 6) & 0x03,
            step_states_hi & 0x03,
            (step_states_hi >> 2) & 0x03,
            (step_states_hi >> 4) & 0x03,
        ],
        "states_spare": (step_states_hi >> 6) & 0x03,
        "reserved": data[6],
        "checklist_version": data[7],
    }


def format_fields(title, can_id, data, fields):
    """Format every field of one frame as text."""
    raw = " ".join("%02X" % b for b in data)
    lines = ["=" * 66, title]
    lines.append("  can_id             : 0x%08X extended (tid 0x%04X, source 0x%02X)"
                 % (can_id, (can_id >> 8) & 0xFFFF, can_id & 0xFF))
    lines.append("  raw bytes          : %s" % raw)
    lines.append("  byte 0 protocol_version  : %d" % fields["protocol_version"])
    lines.append("  byte 1 session_id        : %d (0x%02X)"
                 % (fields["session_id"], fields["session_id"]))
    lines.append("  byte 2 sequence          : %d" % fields["sequence"])
    lines.append("  byte 3 flags             : 0x%02X (0b%s)"
                 % (fields["flags"], format(fields["flags"], "08b")))
    lines.append("           bit0  preflight_incomplete : %d"
                 % fields["preflight_incomplete"])
    lines.append("           bit1-3 active_group        : %d"
                 % fields["active_group"])
    if 1 <= fields["active_group"] <= 7:
        lines.append("                  active group label  : %s"
                     % group_label(fields["active_group"]))
    else:
        lines.append("                  active group label  : none (value 0)")
    lines.append("           bit4  session_active       : %d"
                 % fields["session_active"])
    lines.append("  byte 4 step_states_lo    : 0x%02X" % fields["step_states_lo"])
    lines.append("  byte 5 step_states_hi    : 0x%02X" % fields["step_states_hi"])
    lines.append("  group states:")
    for index, value in enumerate(fields["states"], start=1):
        lines.append("           %-8s : %d %s"
                     % (group_label(index), value, STEP_STATES[value]))
    lines.append("  byte 6 reserved          : %d" % fields["reserved"])
    lines.append("  byte 7 checklist_version : %d" % fields["checklist_version"])
    return "\n".join(lines)


def rx_worker(sock, stop_event, link):
    """Receive, decode, and print the display heartbeat. Watch for stale."""
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
                        say("WARNING: stale link. No heartbeat 0x%06X for %.1f s."
                            % (RX_CAN_ID, now - link["started"]))
            elif now - link["last_rx"] >= STALE_LIMIT_S:
                if now - link["last_warn"] >= STALE_LIMIT_S:
                    link["last_warn"] = now
                    say("WARNING: stale link. No heartbeat 0x%06X for %.1f s."
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
        count += 1

        if was_stale:
            say("Link restored. Heartbeat 0x%06X is back." % RX_CAN_ID)

        say(format_fields("RX display heartbeat #%d  t=%.3f"
                          % (count, time.time()),
                          can_id_clean, data, decode_heartbeat(data)))
    link["rx_count"] = count


def tx_worker(sock, state, stop_event, link):
    """Send the node state summary every 500 ms.

    A transmit error must not stop the simulator. If no other node
    acknowledges, the driver queue fills and send() raises ENOBUFS. The
    thread reports the error, waits, and tries again. It only stops when
    the socket is closed.
    """
    count = 0
    errors = 0
    last_error_report = 0.0
    while not stop_event.is_set():
        data, _fields = state.build_frame(advance=True)
        try:
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


HELP_TEXT = "\n".join([
    "Commands:",
    "  <group> <state>   set one group. Example: 3 GOOD",
    "                    group is 1 to 7, g1 to g7, or S P E1 C T E2 R",
    "                    state is PENDING, ACTIVE, GOOD, or FAULT",
    "  all <state>       set every group. Example: all PENDING",
    "  show              print the state and the next transmit frame",
    "  help              print this text",
    "  quit              stop and close the sockets",
])


def show_outgoing(state, reason):
    """Print the transmit bytes that go out after a change."""
    data, fields = state.build_frame(advance=False)
    raw = " ".join("%02X" % b for b in data)
    lines = ["-" * 66,
             "%s" % reason,
             "  next TX can_id 0x%08X : %s" % (TX_CAN_ID, raw),
             "  step_states_lo 0x%02X   step_states_hi 0x%02X"
             % (fields["step_states_lo"], fields["step_states_hi"]),
             "  flags 0x%02X  preflight_incomplete %d  active_group %d  session_active %d"
             % (fields["flags"], fields["preflight_incomplete"],
                fields["active_group"], fields["session_active"])]
    for index, value in enumerate(fields["states"], start=1):
        lines.append("  %-8s : %d %s"
                     % (group_label(index), value, STEP_STATES[value]))
    lines.append("  cansend form: %08X#%s" % (TX_CAN_ID, "".join("%02X" % b for b in data)))
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
            say("Console input closed. The simulator keeps running.")
            return

        command = line.strip()
        if not command:
            continue

        parts = command.split()
        head = parts[0].lower()

        if head in ("quit", "exit", "q"):
            say("Quit. Close the sockets.")
            stop_event.set()
            return

        if head == "help":
            say(HELP_TEXT)
            continue

        if head in ("show", "status"):
            show_outgoing(state, "Current state.")
            continue

        if len(parts) != 2:
            say("Bad command: %r. Type help." % command)
            continue

        state_name = parts[1].upper()
        if state_name not in STATE_BY_NAME:
            say("Bad state: %r. Use PENDING, ACTIVE, GOOD, or FAULT."
                % parts[1])
            continue
        value = STATE_BY_NAME[state_name]

        if head == "all":
            changed = state.set_all(value)
            if changed:
                show_outgoing(state, "Change: all groups to %s." % state_name)
            else:
                say("No change. Every group is already %s." % state_name)
            continue

        index = parse_group(parts[0])
        if index is None:
            say("Bad group: %r. Use 1 to 7, g1 to g7, or S P E1 C T E2 R."
                % parts[0])
            continue

        changed = state.set_group(index, value)
        if changed:
            show_outgoing(state, "Change: %s to %s."
                          % (group_label(index), state_name))
        else:
            say("No change. %s is already %s."
                % (group_label(index), state_name))


def main():
    parser = argparse.ArgumentParser(
        description="SPECTER bench simulator. Receive 0x2402. Transmit 0x2480.")
    parser.add_argument("interface", help="SocketCAN interface, for example can0")
    parser.add_argument("--protocol-version", type=int, default=1,
                        help="Byte 0 of the transmit frame. Bench default 1.")
    parser.add_argument("--session-id", type=int, default=1,
                        help="Byte 1 of the transmit frame. Bench default 1.")
    parser.add_argument("--checklist-version", type=int, default=1,
                        help="Byte 7 of the transmit frame. Bench default 1.")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Stop after S seconds. 0 means run until quit.")
    args = parser.parse_args()

    try:
        rx_sock = open_rx_socket(args.interface)
        tx_sock = open_tx_socket(args.interface)
    except OSError as error:
        say("ERROR: cannot open %s: %s" % (args.interface, error))
        return 1

    state = SimState(args.protocol_version & 0xFF,
                     args.session_id & 0xFF,
                     args.checklist_version & 0xFF)

    stop_event = threading.Event()
    link = {"last_rx": None, "started": time.monotonic(),
            "last_warn": 0.0, "rx_count": 0, "tx_count": 0, "tx_errors": 0}

    say("SPECTER bench simulator.")
    say("interface : %s" % args.interface)
    say("RX filter : can_id 0x%06X (tid 0x%04X, source 0x%02X) display heartbeat"
        % (RX_CAN_ID, TID_DISPLAY_HEARTBEAT, DISPLAY_SOURCE))
    say("TX frame  : can_id 0x%06X (tid 0x%04X, source 0x%02X) node state summary"
        % (TX_CAN_ID, TID_NODE_STATE_SUMMARY, NODE_SOURCE))
    say("TX period : %d ms" % int(TX_PERIOD_S * 1000))
    say("Stale limit: %.0f s" % STALE_LIMIT_S)
    say("Every group starts at PENDING.")
    show_outgoing(state, "Start state.")

    def handle_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    rx_thread = threading.Thread(target=rx_worker,
                                 args=(rx_sock, stop_event, link),
                                 name="rx", daemon=True)
    tx_thread = threading.Thread(target=tx_worker,
                                 args=(tx_sock, state, stop_event, link),
                                 name="tx", daemon=True)
    rx_thread.start()
    tx_thread.start()

    if args.duration > 0:
        def stop_later():
            stop_event.wait(args.duration)
            stop_event.set()
        threading.Thread(target=stop_later, daemon=True).start()

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
        say("Stopped. TX frames accepted: %d. TX errors: %d. "
            "RX heartbeats decoded: %d."
            % (link["tx_count"], link["tx_errors"], link["rx_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
