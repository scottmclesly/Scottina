#!/usr/bin/env python3
"""SPECTER bench test tile. It shows the live link state on one screen.

This file is a wrapper. It does not replace specter_sim.py. It imports
specter_sim and it reuses:
  SimState            the authoritative step states and the frame builder
  open_rx_socket      the receive socket and the tested filter mask
  open_tx_socket      the transmit socket
  send_frame          the frame writer
  decode_heartbeat    the heartbeat decoder
  parse_group         the console group parser
It adds a terminal tile that refreshes in place.

The tile shows three blocks:

RECEIVE   the display heartbeat 0x240220. Link state ALIVE or STALE,
          frame count, measured period, sequence gaps and repeats,
          the decoded flags, and the seven group states.

TRANSMIT  the node status 0x248021. Frame count, send errors, the seven
          group states this simulator publishes, and the raw eight bytes
          of the last frame sent.

BUS       the can0 state, the berr-counter, and the interface counters.

Four threads run at the same time:
  rx        receives and decodes. It only writes counters. It never
            renders. Rendering can not block it.
  tx        sends the node status every 500 ms.
  health    reads ip link once a second in a subprocess.
  render    paints the tile every 200 ms from a snapshot of the counters.
The console runs on the main thread.

Console commands are the same as specter_sim.py:
  3 GOOD        set group 3 to GOOD
  g3 GOOD       the same
  E1 GOOD       the same, by group letter
  all PENDING   set every group
  show          put the next transmit frame in the log
  help          put the command list in the log
  quit          stop and close the sockets

Use: python3 specter_tile.py can0
"""

import argparse
import collections
import errno
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import specter_sim

REFRESH_S = 0.2
STALE_LIMIT_S = 2.0
HEALTH_PERIOD_S = 1.0
PERIOD_WINDOW = 40
WIDTH = 78

RX_CAN_ID = specter_sim.RX_CAN_ID
TX_CAN_ID = specter_sim.TX_CAN_ID
GROUP_LETTERS = specter_sim.GROUP_LETTERS
STEP_STATES = specter_sim.STEP_STATES
STATE_BY_NAME = specter_sim.STATE_BY_NAME
PENDING, ACTIVE, GOOD, FAULT = 0, 1, 2, 3


class Palette:
    """ANSI colour. It turns itself off when the output is not a terminal."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[1;32m"
    RED = "\033[1;31m"
    YELLOW = "\033[1;33m"
    CYAN = "\033[1;36m"
    GREY = "\033[0;37m"

    STATE_COLOUR = {PENDING: GREY, ACTIVE: YELLOW, GOOD: GREEN, FAULT: RED}

    def __init__(self, enabled):
        self.enabled = enabled

    def paint(self, text, colour):
        if not self.enabled or not colour:
            return text
        return colour + text + self.RESET

    def state(self, text, value):
        return self.paint(text, self.STATE_COLOUR.get(value, ""))


class RxStats:
    """Counters for the receive side. The rx thread is the only writer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames = 0
        self.bad_dlc = 0
        self.first_rx = None
        self.last_rx = None
        self.periods = collections.deque(maxlen=PERIOD_WINDOW)
        self.last_period_ms = None
        self.last_sequence = None
        self.gaps = 0
        self.repeats = 0
        self.last_data = None
        self.last_fields = None
        self.stale_events = 0
        self.errors = 0
        self.last_error = ""

    def record(self, data, fields, now):
        with self.lock:
            self.frames += 1
            if self.first_rx is None:
                self.first_rx = now
            if self.last_rx is not None:
                delta_ms = (now - self.last_rx) * 1000.0
                self.last_period_ms = delta_ms
                self.periods.append(delta_ms)
                if delta_ms >= STALE_LIMIT_S * 1000.0:
                    self.stale_events += 1
            self.last_rx = now
            sequence = fields["sequence"]
            if self.last_sequence is not None:
                expected = (self.last_sequence + 1) & 0xFF
                if sequence == self.last_sequence:
                    self.repeats += 1
                elif sequence != expected:
                    self.gaps += 1
            self.last_sequence = sequence
            self.last_data = data
            self.last_fields = fields

    def record_bad_dlc(self):
        with self.lock:
            self.bad_dlc += 1

    def record_error(self, text):
        with self.lock:
            self.errors += 1
            self.last_error = text

    def snapshot(self):
        with self.lock:
            periods = list(self.periods)
            return {
                "frames": self.frames,
                "bad_dlc": self.bad_dlc,
                "first_rx": self.first_rx,
                "last_rx": self.last_rx,
                "last_period_ms": self.last_period_ms,
                "periods": periods,
                "sequence": self.last_sequence,
                "gaps": self.gaps,
                "repeats": self.repeats,
                "data": self.last_data,
                "fields": self.last_fields,
                "stale_events": self.stale_events,
                "errors": self.errors,
                "last_error": self.last_error,
            }


class TxStats:
    """Counters for the transmit side. The tx thread is the only writer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames = 0
        self.errors = 0
        self.last_error = ""
        self.last_data = None

    def record_sent(self, data):
        with self.lock:
            self.frames += 1
            self.last_data = data

    def record_error(self, text):
        with self.lock:
            self.errors += 1
            self.last_error = text

    def snapshot(self):
        with self.lock:
            return {
                "frames": self.frames,
                "errors": self.errors,
                "last_error": self.last_error,
                "data": self.last_data,
            }


class Health:
    """The last reading of ip link. The health thread is the only writer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.info = {"error": "no reading yet"}
        self.stamp = None

    def update(self, info, stamp):
        with self.lock:
            self.info = info
            self.stamp = stamp

    def snapshot(self):
        with self.lock:
            return dict(self.info), self.stamp


class LogRing:
    """The last few messages. The tile paints them. Nothing scrolls."""

    def __init__(self, size):
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=size)
        self.size = size

    def add(self, text):
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            for part in text.split("\n"):
                self.lines.append("%s  %s" % (stamp, part))

    def snapshot(self):
        with self.lock:
            return list(self.lines)


def read_link(interface):
    """Read ip link for the interface. Return a dict of the fields."""
    info = {"error": None}
    try:
        result = subprocess.run(
            ["ip", "-details", "-statistics", "link", "show", interface],
            capture_output=True, text=True, timeout=3.0)
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": str(error)}
    if result.returncode != 0:
        return {"error": result.stderr.strip() or "ip exit %d" % result.returncode}

    text = result.stdout
    lines = text.splitlines()
    if not lines:
        return {"error": "ip gave no output"}

    match = re.search(r"<([^>]*)>", lines[0])
    info["flags"] = match.group(1) if match else "?"
    match = re.search(r"\bstate ([A-Z_-]+)", lines[0])
    info["if_state"] = match.group(1) if match else "?"

    match = re.search(r"can state (\S+) \(berr-counter tx (\d+) rx (\d+)\)", text)
    if match:
        info["can_state"] = match.group(1)
        info["berr_tx"] = int(match.group(2))
        info["berr_rx"] = int(match.group(3))
    else:
        info["can_state"] = "?"
        info["berr_tx"] = None
        info["berr_rx"] = None

    match = re.search(r"\bbitrate (\d+)", text)
    info["bitrate"] = int(match.group(1)) if match else None

    for index, line in enumerate(lines):
        if "re-started" in line and "bus-errors" in line and index + 1 < len(lines):
            numbers = [int(value) for value in re.findall(r"\d+", lines[index + 1])]
            if len(numbers) >= 6:
                (info["restarts"], info["bus_errors"], info["arbit_lost"],
                 info["error_warn"], info["error_pass"],
                 info["bus_off"]) = numbers[:6]
            break

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("RX:") and index + 1 < len(lines):
            numbers = [int(value) for value in re.findall(r"\d+", lines[index + 1])]
            if len(numbers) >= 6:
                info["rx_counters"] = numbers[:6]
        if stripped.startswith("TX:") and index + 1 < len(lines):
            numbers = [int(value) for value in re.findall(r"\d+", lines[index + 1])]
            if len(numbers) >= 6:
                info["tx_counters"] = numbers[:6]
    return info


def rx_worker(sock, stop_event, stats, log):
    """Receive and decode the display heartbeat. Write counters only.

    This loop never renders and never takes the output lock. The render
    thread can not block it.

    A receive error does not stop the tile. The tile exists to show a
    fault. If the interface goes down or the adapter is pulled, the loop
    counts the error, waits, and tries again. The tile then shows STALE.
    Only a closed socket at shutdown ends the loop.
    """
    last_report = 0.0
    while not stop_event.is_set():
        try:
            packet = sock.recv(specter_sim.CAN_FRAME_SIZE)
        except socket.timeout:
            continue
        except OSError as error:
            if stop_event.is_set() or error.errno == errno.EBADF:
                return
            stats.record_error(str(error))
            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                log.add("RX error: %s" % error)
            stop_event.wait(0.2)
            continue

        can_id, dlc, payload = struct.unpack(specter_sim.CAN_FRAME_FMT, packet)
        data = payload[:dlc]
        if len(data) != 8:
            stats.record_bad_dlc()
            continue
        stats.record(data, specter_sim.decode_heartbeat(data), time.monotonic())


def tx_worker(sock, state, stop_event, stats, log):
    """Send the node status every 500 ms. A send error must not stop it."""
    last_report = 0.0
    while not stop_event.is_set():
        data, _fields = state.build_frame(advance=True)
        try:
            specter_sim.send_frame(sock, TX_CAN_ID, data)
            stats.record_sent(data)
        except OSError as error:
            stats.record_error(str(error))
            if error.errno in (errno.EBADF, errno.ENOTCONN, errno.ENODEV):
                log.add("TX stopped. Socket error: %s" % error)
                return
            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                log.add("TX error: %s" % error)
        stop_event.wait(specter_sim.TX_PERIOD_S)


def health_worker(interface, stop_event, health):
    """Read ip link once a second in a subprocess. Keep it off the rx path."""
    while not stop_event.is_set():
        health.update(read_link(interface), time.monotonic())
        stop_event.wait(HEALTH_PERIOD_S)


def rule(character):
    return character * WIDTH


def states_header():
    row = "   %-9s" % "group"
    for letter in GROUP_LETTERS:
        row += letter.rjust(8)
    return row


def states_row(label, states, palette):
    row = "   %-9s" % label
    for value in states:
        row += palette.state(STEP_STATES[value].rjust(8), value)
    return row


def format_raw(data):
    if not data:
        return "(none yet)"
    return " ".join("%02X" % byte for byte in data)


def format_uptime(seconds):
    seconds = int(seconds)
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds // 60) % 60, seconds % 60)


def build_tile(interface, started, state, rx_stats, tx_stats, health, log,
               palette):
    """Build every line of the tile. Take snapshots, then format."""
    rx = rx_stats.snapshot()
    tx = tx_stats.snapshot()
    info, health_stamp = health.snapshot()
    log_lines = log.snapshot()
    tx_states = state.snapshot()
    now = time.monotonic()

    lines = []
    lines.append(rule("="))
    title = " SPECTER BENCH TILE   %s" % interface
    right = "up %s   %s " % (format_uptime(now - started),
                             time.strftime("%Y-%m-%d %H:%M:%S"))
    pad = WIDTH - len(title) - len(right)
    lines.append(palette.paint(title + " " * max(1, pad) + right, Palette.BOLD))
    lines.append(rule("="))

    lines.append(palette.paint(" RECEIVE   display heartbeat 0x%06X" % RX_CAN_ID,
                               Palette.CYAN))
    if rx["last_rx"] is None:
        age = now - started
        alive = False
        age_text = "no frame since start, %.1f s" % age
    else:
        age = now - rx["last_rx"]
        alive = age < STALE_LIMIT_S
        age_text = "last frame %.2f s ago" % age
    if alive:
        badge = palette.paint("[  ALIVE  ]", Palette.GREEN)
    else:
        badge = palette.paint("[  STALE  ]", Palette.RED)
    lines.append("   %-9s%s   %s" % ("LINK", badge, age_text))

    periods = rx["periods"]
    if periods:
        period_text = ("last %6.1f ms   avg %6.1f   min %6.1f   max %6.1f"
                       % (rx["last_period_ms"],
                          sum(periods) / len(periods),
                          min(periods), max(periods)))
    else:
        period_text = "no period yet"
    lines.append("   %-9s%-10d %s" % ("frames", rx["frames"], period_text))

    sequence = rx["sequence"]
    sequence_text = "-" if sequence is None else "%3d (0x%02X)" % (sequence, sequence)
    error_text = "bad dlc %d  rx err %d" % (rx["bad_dlc"], rx["errors"])
    if rx["errors"]:
        error_text = palette.paint(error_text, Palette.RED)
    lines.append("   %-9sseq %-11s gaps %-6d repeats %-6d %s"
                 % ("sequence", sequence_text, rx["gaps"], rx["repeats"],
                    error_text))

    fields = rx["fields"]
    if fields:
        group = fields["active_group"]
        if 1 <= group <= 7:
            group_text = "%d %s" % (group, GROUP_LETTERS[group - 1])
        else:
            group_text = "0 none"
        lines.append("   %-9sproto %d  session %d  checklist %d  reserved %d"
                     % ("header", fields["protocol_version"], fields["session_id"],
                        fields["checklist_version"], fields["reserved"]))
        lines.append("   %-9s0x%02X  preflight_incomplete %d  active_group %s  "
                     "session_active %d"
                     % ("flags", fields["flags"], fields["preflight_incomplete"],
                        group_text, fields["session_active"]))
        lines.append("   %-9s%s" % ("raw", format_raw(rx["data"])))
        lines.append(states_header())
        lines.append(states_row("state", fields["states"], palette))
    else:
        lines.append("   %-9s-" % "header")
        lines.append("   %-9s-" % "flags")
        lines.append("   %-9s%s" % ("raw", "(none yet)"))
        lines.append(states_header())
        lines.append("   %-9s%s" % ("state", "no heartbeat decoded yet"))

    lines.append(rule("-"))
    lines.append(palette.paint(" TRANSMIT  node status 0x%06X   every %d ms"
                               % (TX_CAN_ID, int(specter_sim.TX_PERIOD_S * 1000)),
                               Palette.CYAN))
    error_text = "errors %d" % tx["errors"]
    if tx["errors"]:
        error_text = palette.paint(error_text, Palette.RED)
        error_text += "   last: %s" % tx["last_error"]
    lines.append("   %-9s%-10d %s" % ("frames", tx["frames"], error_text))
    lines.append(states_header())
    lines.append(states_row("state", tx_states, palette))
    lines.append("   %-9s%s" % ("raw", format_raw(tx["data"])))

    lines.append(rule("-"))
    lines.append(palette.paint(" BUS       %s" % interface, Palette.CYAN))
    if info.get("error"):
        lines.append("   %-9s%s" % ("state", palette.paint(
            "ip link error: %s" % info["error"], Palette.RED)))
        lines.append("   %-9s-" % "errors")
        lines.append("   %-9s-" % "RX")
        lines.append("   %-9s-" % "TX")
    else:
        can_state = info.get("can_state", "?")
        colour = Palette.GREEN if can_state == "ERROR-ACTIVE" else Palette.RED
        bitrate = info.get("bitrate")
        lines.append("   %-9s%s   berr tx %s rx %s   bitrate %s   link %s"
                     % ("state", palette.paint(can_state, colour),
                        info.get("berr_tx"), info.get("berr_rx"),
                        "%d" % bitrate if bitrate else "?",
                        info.get("if_state", "?")))
        lines.append("   %-9sbus-err %d  arb-lost %d  warn %d  passive %d  "
                     "bus-off %d  restarts %d"
                     % ("errors", info.get("bus_errors", 0),
                        info.get("arbit_lost", 0), info.get("error_warn", 0),
                        info.get("error_pass", 0), info.get("bus_off", 0),
                        info.get("restarts", 0)))
        rx_counters = info.get("rx_counters")
        tx_counters = info.get("tx_counters")
        if rx_counters:
            lines.append("   %-9spkts %-9d bytes %-10d errors %-6d dropped %-6d "
                         "missed %d"
                         % ("RX", rx_counters[1], rx_counters[0], rx_counters[2],
                            rx_counters[3], rx_counters[4]))
        else:
            lines.append("   %-9s-" % "RX")
        if tx_counters:
            lines.append("   %-9spkts %-9d bytes %-10d errors %-6d dropped %-6d "
                         "carrier %d"
                         % ("TX", tx_counters[1], tx_counters[0], tx_counters[2],
                            tx_counters[3], tx_counters[4]))
        else:
            lines.append("   %-9s-" % "TX")

    lines.append(rule("-"))
    lines.append(palette.paint(" LOG", Palette.CYAN))
    for index in range(log.size):
        if index < len(log_lines):
            lines.append("   %s" % log_lines[index][:WIDTH - 4])
        else:
            lines.append("")
    lines.append(rule("="))
    return lines


def render_worker(stop_event, painter, dirty):
    """Paint the tile every 200 ms. This thread does all of the output."""
    while not stop_event.is_set():
        painter()
        dirty.wait(REFRESH_S)
        dirty.clear()
    painter()


class Screen:
    """The output. It paints in place on a terminal, in blocks otherwise."""

    def __init__(self, in_place, palette, prompt):
        self.in_place = in_place
        self.palette = palette
        self.prompt = prompt
        self.lock = threading.Lock()
        self.buffer = ""
        self.started = False

    def set_buffer(self, text):
        with self.lock:
            self.buffer = text

    def paint(self, lines):
        with self.lock:
            buffer = self.buffer
        if self.in_place:
            out = ["\033[H"]
            for line in lines:
                out.append(line)
                out.append("\033[K\n")
            out.append(self.prompt)
            out.append(buffer)
            out.append("\033[K\033[J")
            text = "".join(out)
        else:
            text = "\n".join(lines) + "\n%s%s\n\n" % (self.prompt, buffer)
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def start(self):
        if self.in_place and not self.started:
            self.started = True
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

    def finish(self, lines_used):
        if self.in_place:
            sys.stdout.write("\033[%d;1H\n" % max(1, lines_used + 2))
            sys.stdout.flush()


HELP_LINES = [
    "<group> <state>  set one group. Example: 3 GOOD",
    "  group 1 to 7, g1 to g7, or S P E1 C T E2 R",
    "  state PENDING, ACTIVE, GOOD, or FAULT",
    "all <state>      set every group.  show  help  quit",
]


def apply_command(command, state, log):
    """Run one console command. Put the result in the log."""
    parts = command.split()
    head = parts[0].lower()

    if head in ("quit", "exit", "q"):
        return False

    if head == "help":
        for line in HELP_LINES:
            log.add(line)
        return True

    if head in ("show", "status"):
        data, fields = state.build_frame(advance=False)
        log.add("next TX %08X#%s  flags 0x%02X  lo 0x%02X hi 0x%02X"
                % (TX_CAN_ID, "".join("%02X" % byte for byte in data),
                   fields["flags"], fields["step_states_lo"],
                   fields["step_states_hi"]))
        return True

    if len(parts) != 2:
        log.add("Bad command: %r. Type help." % command)
        return True

    state_name = parts[1].upper()
    if state_name not in STATE_BY_NAME:
        log.add("Bad state: %r. Use PENDING, ACTIVE, GOOD, or FAULT." % parts[1])
        return True
    value = STATE_BY_NAME[state_name]

    if head == "all":
        if state.set_all(value):
            log.add("Change: all groups to %s." % state_name)
        else:
            log.add("No change. Every group is already %s." % state_name)
        return True

    index = specter_sim.parse_group(parts[0])
    if index is None:
        log.add("Bad group: %r. Use 1 to 7, g1 to g7, or S P E1 C T E2 R."
                % parts[0])
        return True

    if state.set_group(index, value):
        log.add("Change: %s to %s." % (specter_sim.group_label(index), state_name))
    else:
        log.add("No change. %s is already %s."
                % (specter_sim.group_label(index), state_name))
    return True


def console_raw(state, stop_event, screen, log, dirty):
    """Read the keyboard one character at a time. Keep the tile clean.

    The terminal echo is off. The tile paints the prompt and the typed
    text. This stops the keyboard from writing over the tile.
    """
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    buffer = ""
    try:
        while not stop_event.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                char = os.read(fd, 1)
            except OSError:
                return
            if not char:
                return
            code = char[0]

            if code in (10, 13):
                command = buffer.strip()
                buffer = ""
                screen.set_buffer(buffer)
                if command:
                    log.add("cmd> %s" % command)
                    if not apply_command(command, state, log):
                        log.add("Quit. Close the sockets.")
                        stop_event.set()
                        return
                dirty.set()
                continue
            if code in (127, 8):
                buffer = buffer[:-1]
            elif code == 21:
                buffer = ""
            elif code == 3:
                stop_event.set()
                return
            elif code == 4:
                if not buffer:
                    stop_event.set()
                    return
            elif 32 <= code < 127:
                buffer += chr(code)
            else:
                continue
            screen.set_buffer(buffer)
            dirty.set()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def console_lines(state, stop_event, screen, log, dirty):
    """Read whole lines. This runs when the input is not a terminal."""
    while not stop_event.is_set():
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        except (OSError, ValueError):
            return
        if not ready:
            continue
        try:
            line = sys.stdin.readline()
        except (EOFError, ValueError, OSError):
            return
        if line == "":
            log.add("Console input closed. The tile keeps running.")
            return
        command = line.strip()
        if not command:
            continue
        log.add("cmd> %s" % command)
        if not apply_command(command, state, log):
            log.add("Quit. Close the sockets.")
            stop_event.set()
            return
        dirty.set()


def main():
    parser = argparse.ArgumentParser(
        description="SPECTER bench test tile. It shows the live link state.")
    parser.add_argument("interface", help="SocketCAN interface, for example can0")
    parser.add_argument("--protocol-version", type=int, default=1,
                        help="Byte 0 of the transmit frame. Bench default 1.")
    parser.add_argument("--session-id", type=int, default=1,
                        help="Byte 1 of the transmit frame. Bench default 1.")
    parser.add_argument("--checklist-version", type=int, default=1,
                        help="Byte 7 of the transmit frame. Bench default 1.")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Stop after S seconds. 0 means run until quit.")
    parser.add_argument("--log-lines", type=int, default=3,
                        help="How many log lines the tile shows. Default 3.")
    parser.add_argument("--plain", action="store_true",
                        help="No colour and no cursor moves. For a log file.")
    args = parser.parse_args()

    try:
        rx_sock = specter_sim.open_rx_socket(args.interface)
        tx_sock = specter_sim.open_tx_socket(args.interface)
    except OSError as error:
        sys.stderr.write("ERROR: cannot open %s: %s\n" % (args.interface, error))
        return 1
    rx_sock.settimeout(0.2)

    is_tty = sys.stdout.isatty() and not args.plain
    palette = Palette(is_tty)
    screen = Screen(is_tty, palette, "cmd> ")
    log = LogRing(max(1, args.log_lines))

    state = specter_sim.SimState(args.protocol_version & 0xFF,
                                 args.session_id & 0xFF,
                                 args.checklist_version & 0xFF)
    rx_stats = RxStats()
    tx_stats = TxStats()
    health = Health()
    stop_event = threading.Event()
    dirty = threading.Event()
    started = time.monotonic()

    log.add("Tile started on %s. Type help for the commands." % args.interface)

    def handle_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    line_count = [0]

    def painter():
        lines = build_tile(args.interface, started, state, rx_stats, tx_stats,
                           health, log, palette)
        line_count[0] = len(lines)
        screen.paint(lines)

    threads = [
        threading.Thread(target=rx_worker,
                         args=(rx_sock, stop_event, rx_stats, log),
                         name="rx", daemon=True),
        threading.Thread(target=tx_worker,
                         args=(tx_sock, state, stop_event, tx_stats, log),
                         name="tx", daemon=True),
        threading.Thread(target=health_worker,
                         args=(args.interface, stop_event, health),
                         name="health", daemon=True),
        threading.Thread(target=render_worker,
                         args=(stop_event, painter, dirty),
                         name="render", daemon=True),
    ]

    screen.start()
    for thread in threads:
        thread.start()

    if args.duration > 0:
        def stop_later():
            stop_event.wait(args.duration)
            stop_event.set()
        threading.Thread(target=stop_later, daemon=True).start()

    try:
        if sys.stdin.isatty():
            console_raw(state, stop_event, screen, log, dirty)
        else:
            console_lines(state, stop_event, screen, log, dirty)
        while not stop_event.is_set():
            stop_event.wait(0.2)
    finally:
        stop_event.set()
        dirty.set()
        for thread in threads:
            thread.join(timeout=2.0)
        rx_sock.close()
        tx_sock.close()
        screen.finish(line_count[0])
        rx = rx_stats.snapshot()
        tx = tx_stats.snapshot()
        sys.stdout.write(
            "Stopped. RX heartbeats %d (gaps %d, repeats %d). "
            "TX frames %d (errors %d).\n"
            % (rx["frames"], rx["gaps"], rx["repeats"], tx["frames"],
               tx["errors"]))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
