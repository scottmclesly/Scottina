"""SPECTER — the preflight bench link, both directions at a glance.

The tile that lets the bench prove itself with no laptop attached. It
watches the two periodic frames of the SPECTER preflight link and shows,
for each direction, whether it is ALIVE or STALE, how fast it is really
arriving, whether its sequence is continuous, and the seven decoded group
states:

  DISPLAY  0x240220   the Screen talks   (the Screen is alive)
  NODE     0x248021   the boat side talks (the simulator/node is alive)

Groups are S P E1 C T E2 R; states are PENDING / ACTIVE / GOOD / FAULT.

Scope (CAN-N2K-Split-TODO, hard constraint): **this screen constructs no
TX frames**; its socket (specterlink.SpecterReader, opened on entry, closed
on leave — no shared RX daemon) only ever recv()s. It watches both
directions and speaks neither: the node status shown is what another node
actually put on the wire, observed, never echoed. The bench simulator that
publishes 0x248021 is separate bench equipment, imported by no Scottina
package. tests/test_txscan.py enforces this tree-wide, and the runtime TX
answer stays exactly the GNSS carve-out.

It does carry one *indirect* TX affordance, and the distinctions are the
whole point. The NODE button starts and stops specter-sim.service, which
runs that bench rig, so this screen causes transmission without performing
it — the same shape as the NMEA2K tile's "Source GNSS → bus" control,
except the frames are built by bench equipment in another process rather
than by an allow-listed runtime module. Two gates keep that honest:

  * the unit carries no [Install] section, so the bench node can never
    start itself — it reaches the bus only after a deliberate act, and a
    reboot leaves the bus quiet;
  * the button is declared to the panel only. This tile still exports no
    model_buttons(), so the web mirror cannot press it. WEB-PROTOCOL.md
    §10 says a hostile actor on the LAN cannot transmit on the vehicle bus
    "because no code path exists that would let them" — that stays true,
    because the only path to this control is a finger on the glass.

Presentation follows the ship-instrument look (Cobb's Semiotic Standard):
a hard-edged state banner per direction with a service-state glyph, caps-
mono readouts, and a seven-cell group strip where colour carries state and
the letter carries identity — filled = reached, hollow = not yet. Red is
reserved for genuine faults: a STALE link on a bench whose whole job is to
prove that link, and a group the checklist itself reports as FAULT.
"""

import time

from .. import specterlink as SL, system, theme as T
from ..widgets import brackets, spaced, Button
from .base import Screen, HEADER_H

IFACE = "can0"
IDLE_TICK = 1.0
FAST_TICK = 0.5
BUS_POLL_S = 2.0          # ip link is a subprocess; keep it off every tick

# The bench rig this screen may start and stop. It is bench equipment, not
# runtime (it is named in test_txscan.BENCH_CAN_TX): the frames are built
# there, in another process, never here.
SIM_SERVICE = "specter-sim.service"
SVC_POLL_S = 2.0          # systemctl is a subprocess; same treatment as the bus
BTN_H = 38                # a finger, not a cursor


class SpecterScreen(Screen):
    title = "SPECTER"
    tile_id = "specter"
    glyph = "specter"
    device_key = "can"
    icon = ""

    def __init__(self, app):
        super().__init__(app)
        self.tick_interval = IDLE_TICK
        self.display = SL.LinkState(SL.DISPLAY_CAN_ID, "DISPLAY")
        self.node = SL.LinkState(SL.NODE_CAN_ID, "NODE")
        self.reader = None
        self.bus = SL.parse_can_link("")
        self._bus_at = 0.0
        self._snap = None
        self._btns = {}
        self._sim = "unknown"     # systemctl is-active for SIM_SERVICE
        self._sim_at = 0.0
        self._sim_busy = False    # a tap is in flight; hold the label steady

    # ---- lifecycle ----
    def on_enter(self):
        self._start_reader()
        self._poll_bus(force=True)
        self._poll_sim(force=True)
        self.tick()

    def on_leave(self):
        if self.reader:
            self.reader.stop()
            self.reader = None

    def _start_reader(self):
        if self.reader and self.reader.alive:
            return
        self.reader = SL.SpecterReader(IFACE, self.display, self.node).start()

    def _poll_bus(self, force=False):
        now = time.monotonic()
        if force or now - self._bus_at >= BUS_POLL_S:
            self._bus_at = now
            self.bus = SL.read_can_link(IFACE)

    # ---- the bench node (a control, not a TX path) ----
    # The rig deliberately outlives this screen: leaving the tile must not
    # drop the node the Screen is watching, exactly as the GNSS node keeps
    # its claim when the NMEA2K tile closes. Only a tap stops it.
    def _poll_sim(self, force=False):
        now = time.monotonic()
        if force or now - self._sim_at >= SVC_POLL_S:
            self._sim_at = now
            self._sim = system.run(["systemctl", "is-active",
                                    SIM_SERVICE]) or "unknown"

    @property
    def _sim_on(self):
        return self._sim == "active"

    def _toggle_sim(self):
        """Start or stop the bench rig. This screen builds no frame."""
        stopping = self._sim_on
        self._sim_busy = True
        system.run(["systemctl", "stop" if stopping else "start",
                    SIM_SERVICE], timeout=15)
        self._sim_busy = False
        self._poll_sim(force=True)
        if stopping:
            self.app.toast("Node stopped — the Screen will show it STALE")
        elif self._sim_on:
            self.app.toast("Node started — 0x248021 every 500 ms")
        else:
            self.app.toast("Node did not start — check %s" % SIM_SERVICE)

    def tick(self):
        self._poll_bus()
        self._poll_sim()
        # A dead reader means the iface dropped; retry while the tile is up so
        # the link comes back on its own when the adapter returns.
        if self.reader and not self.reader.alive:
            self.reader = None
        if self.reader is None and self.bus.get("if_state") == "UP":
            self._start_reader()
        now = time.time()
        d = self.display.snapshot(now)
        n = self.node.snapshot(now)
        self._snap = (d, n)
        if self.reader is not None:
            self._hs = (self.reader.hs_request.snapshot(now),
                        self.reader.hs_response.snapshot(now))
        else:
            self._hs = (None, None)
        # Fast refresh only while something is actually flowing.
        self.tick_interval = FAST_TICK if (d["alive"] or n["alive"]) \
            else IDLE_TICK
        return True

    # ---- web mirror ----
    def model_rows(self):
        """Reads tick()'s cached snapshot; never re-polls the bus."""
        if not self._snap:
            return []
        rows = []
        for s in self._snap:
            rows.append({
                "label": "%s 0x%06X" % (s["label"], s["can_id"]),
                "value": ("ALIVE %s" % self._period_text(s)) if s["alive"]
                         else self._stale_text(s),
                "state": "ok" if s["alive"] else "fault",
            })
            rows.append({
                "label": "%s FRAMES" % s["label"],
                "value": "%d  gaps %d  repeats %d"
                         % (s["frames"], s["gaps"], s["repeats"]),
                "state": "caution" if (s["gaps"] or s["repeats"]) else None,
            })
            if s["states"]:
                rows.append({
                    "label": "%s GROUPS" % s["label"],
                    "value": "  ".join(
                        "%s:%s" % (g, SL.STATE_INITIALS[v])
                        for g, v in zip(SL.GROUP_LETTERS, s["states"])),
                    "state": "fault" if SL.FAULT in s["states"] else None,
                })
        rows.extend(self.specter_rows())
        b = self.bus
        rows.append({
            "label": "BUS %s" % IFACE,
            "value": self._bus_text(),
            "state": ("ok" if b.get("can_state") == "ERROR-ACTIVE"
                      else "fault" if b.get("present") else None),
        })
        return rows

    def specter_rows(self):
        """The full SPECTER state, as the display reports it.

        Everything here is DECODED FROM THE WIRE. The panel constructs no
        frame and echoes nothing of its own: what it shows for the display is
        what the display actually put on the bus.
        """
        rows = []
        disp, node = self._snap
        df = disp.get("fields") or {}
        nf = node.get("fields") or {}
        hs_req, hs_rsp = getattr(self, "_hs", (None, None))

        # ---- what the display says it is doing ----
        if df:
            rows.append({
                "label": "DISPLAY STATE",
                "value": SL.display_state_name(df.get("display_state", 0)),
                "state": "ok" if df.get("display_state") == 2 else "caution",
            })
            seq = df.get("event_sequence", 0)
            echo = nf.get("event_echo")
            if seq:
                cleared = (echo == seq)
                rows.append({
                    "label": "EVENT",
                    "value": "seq %d  %s  step %s  echo %s"
                             % (seq,
                                SL.event_detail(df.get("event_type", 0),
                                                df.get("event_param", 0)),
                                ("none" if df.get("step_index") == 0xFF
                                 else SL.step_name(df.get("step_index", 0))),
                                ("cleared" if cleared else "outstanding")),
                    "state": None if cleared else "caution",
                })
            else:
                rows.append({"label": "EVENT", "value": "none outstanding",
                             "state": None})

        # ---- the veto, which is the whole point of the checklist ----
        if nf:
            veto = bool(nf.get("veto"))
            good = sum(1 for s in nf.get("steps", []) if s == SL.GOOD)
            rows.append({
                "label": "VETO",
                "value": ("SET  %d of %d steps GOOD" % (good, SL.STEPS_IN_USE)
                          if veto else "CLEAR  checklist complete"),
                "state": "caution" if veto else "ok",
            })
            if nf.get("operator_input_requested"):
                rows.append({"label": "NODE", "value": "waiting for operator",
                             "state": "caution"})

            # ---- all 13 steps, by name ----
            for index, value in enumerate(nf.get("steps", [])):
                rows.append({
                    "label": "%2d %s" % (index, SL.step_name(index)),
                    "value": SL.STATE_NAMES[value],
                    "state": ("ok" if value == SL.GOOD
                              else "fault" if value == SL.FAULT
                              else "caution" if value == SL.ACTIVE else None),
                })

            # ---- slot 13, which is NOT a step ----
            shore = nf.get("shore_link")
            if shore is not None:
                rows.append({
                    "label": "13 %s" % SL.step_name(SL.SHORE_LINK_SLOT),
                    "value": "%s  (not a checklist step)"
                             % SL.STATE_NAMES[shore],
                    "state": "ok" if shore == SL.GOOD else None,
                })

        # ---- the handshake, and which build the display is ----
        if hs_req and hs_req.get("fields"):
            f = hs_req["fields"]
            rows.append({
                "label": "FIRMWARE",
                "value": ("%s  protocol 0x%02X  capacity %d"
                          % (f["firmware_text"], f["protocol_version"],
                             f["step_capacity"])),
                "state": "caution" if f["firmware_id"] == 0 else None,
            })
            # THE BUILD LINE. Read it before any symptom on the display: a
            # dirty or behind image explains every one of them at once.
            rows.append({
                "label": "BUILD",
                "value": f.get("build_text", "unknown"),
                "state": None if f.get("build_clean") else "fault",
            })
        if hs_rsp and hs_rsp.get("fields"):
            f = hs_rsp["fields"]
            rows.append({
                "label": "HANDSHAKE",
                "value": ("%s  checklist 0x%04X  session %d  steps %d"
                          % (f["result_text"], f["checklist_id"],
                             f["session_id"], f["step_count"])),
                "state": "ok" if f["result"] == 0 else "fault",
            })

        # ---- what this rig is putting on the wire ----
        rows.extend(self.telemetry_rows())
        return rows

    def telemetry_rows(self):
        """The telemetry the RIG is sending.

        The rig is the only thing on a bench bus that sends these, so this
        says plainly what the display is being told and from which address.
        It reads a description, never the rig module: importing that at draw
        time would run its codec import, which exits on failure, and a panel
        render must never be able to take the tile down.
        """
        if not self._sim_on:
            return [{"label": "TELEMETRY", "value": "NODE off, bus quiet",
                     "state": None}]
        rows = [{"label": "TELEMETRY",
                 "value": "%d messages, vessel ids" % len(SL.TELEMETRY_SENT),
                 "state": "ok"}]
        for tid, source, label, hz in SL.TELEMETRY_SENT:
            rows.append({
                "label": "0x%06X" % ((tid << 8) | source),
                "value": "%-32s %2d Hz" % (label, hz),
                "state": None,
            })
        return rows

    @staticmethod
    def _period_text(s):
        return "—" if s["period_ms"] is None else "%.0f ms" % s["period_ms"]

    @staticmethod
    def _stale_text(s):
        if s["age"] is None:
            return "STALE (never seen)"
        return "STALE %.1f s" % s["age"]

    def _bus_text(self):
        b = self.bus
        if not b.get("present"):
            return "%s absent" % IFACE
        rate = b.get("bitrate")
        return "%s  berr %s/%s  %s" % (
            b.get("can_state") or b.get("if_state") or "?",
            b.get("berr_tx"), b.get("berr_rx"),
            "%d kbps" % (rate // 1000) if rate else "? kbps")

    # ---- rendering ----
    def _state_colour(self, th, value):
        return {SL.PENDING: th.muted, SL.ACTIVE: th.warn,
                SL.GOOD: th.ok, SL.FAULT: th.bad}.get(value, th.muted)

    def _banner(self, d, th, y, snap):
        """One direction's state banner: glyph + ALIVE/STALE + who talks."""
        w = self.app.w
        alive = snap["alive"]
        col = th.ok if alive else th.bad
        box = (14, y, w - 14, y + 52)
        d.rectangle(box, fill=th.card, outline=th.card_hi, width=1)
        # a stale link on a bench built to prove that link is a real fault
        d.rectangle((14, y, 18, y + 52), fill=col)

        cx, cy = 44, y + 26
        self._service_glyph(d, "up" if alive else "fault", cx, cy, 13, col)

        f = T.font(21, bold=True, mono=True)
        d.text((68, y + 8), spaced("ALIVE" if alive else "STALE"),
               font=f, fill=col)
        sub = self._period_text(snap) if alive else self._stale_text(snap)
        d.text((68, y + 33), sub, font=T.font(T.SUB, mono=True), fill=th.muted)

        f2 = T.font(10, bold=True, mono=True)
        tag = "0x%06X" % snap["can_id"]
        tw = d.textlength(tag, font=f2)
        d.text((w - 24 - tw, y + 10), tag, font=f2, fill=th.muted)
        cnt = "n %d" % snap["frames"]
        tw = d.textlength(cnt, font=f2)
        d.text((w - 24 - tw, y + 24), cnt, font=f2, fill=th.fg)
        if snap["gaps"] or snap["repeats"]:
            warn = "gap %d rpt %d" % (snap["gaps"], snap["repeats"])
            tw = d.textlength(warn, font=f2)
            d.text((w - 24 - tw, y + 38), warn, font=f2, fill=th.warn)
        return y + 52

    @staticmethod
    def _service_glyph(d, key, cx, cy, r, c):
        """Ringed service-state mark, kin to widgets.state_glyph — inlined so
        the banner can size it independently of the list idiom."""
        lw = max(2, round(r / 5))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=c, width=lw)
        if key == "up":
            s = r * 0.5
            d.ellipse((cx - s, cy - s, cx + s, cy + s), fill=c)
        else:
            d.rectangle((cx - lw / 2, cy - r * 0.55, cx + lw / 2, cy + r * 0.1),
                        fill=c)
            d.rectangle((cx - lw / 2, cy + r * 0.3, cx + lw / 2, cy + r * 0.55),
                        fill=c)

    def _groups(self, d, th, y, snap):
        """The seven-cell group strip: colour = state, letter = identity."""
        w = self.app.w
        states = snap["states"]
        n = len(SL.GROUP_LETTERS)
        gap = 4
        cw = (w - 28 - gap * (n - 1)) / n
        h = 40
        for i, letter in enumerate(SL.GROUP_LETTERS):
            x0 = 14 + i * (cw + gap)
            box = (x0, y, x0 + cw, y + h)
            value = states[i] if states else None
            if value is None:
                d.rectangle(box, outline=th.card_hi, width=1)
                d.text((x0 + cw / 2 - 3, y + 12), "-",
                       font=T.font(13, bold=True, mono=True), fill=th.muted)
                continue
            col = self._state_colour(th, value)
            if value == SL.PENDING:
                # hollow = not reached yet (extinguished, not absent)
                d.rectangle(box, outline=col, width=1)
                lc, sc = th.muted, th.muted
            else:
                d.rectangle(box, fill=col)
                lc, sc = th.ink, th.ink
            lf = T.font(15, bold=True, mono=True)
            lw_ = d.textlength(letter, font=lf)
            d.text((x0 + cw / 2 - lw_ / 2, y + 5), letter, font=lf, fill=lc)
            sf = T.font(10, bold=True, mono=True)
            si = SL.STATE_INITIALS[value]
            sw = d.textlength(si, font=sf)
            d.text((x0 + cw / 2 - sw / 2, y + 24), si, font=sf, fill=sc)
        return y + h

    def _steps_strip(self, d, th, y):
        """All 13 steps as one strip: colour is state, number is the step.

        Thirteen cells across 480 pixels is 33 each, so the cell carries the
        step NUMBER and the state colour. The name of the step under the
        cursor is written under the strip, because a number alone means
        nothing across a bench.
        """
        w = self.app.w
        _disp, node = self._snap
        nf = node.get("fields") or {}
        steps = nf.get("steps") or []
        n = SL.STEPS_IN_USE
        gap = 2
        cw = (w - 28 - gap * (n - 1)) / n
        h = 26

        for i in range(n):
            x0 = 14 + i * (cw + gap)
            box = (x0, y, x0 + cw, y + h)
            value = steps[i] if i < len(steps) else None
            if value is None:
                d.rectangle(box, outline=th.card_hi, width=1)
                continue
            col = self._state_colour(th, value)
            if value == SL.PENDING:
                d.rectangle(box, outline=col, width=1)
                tc = th.muted
            else:
                d.rectangle(box, fill=col)
                tc = th.ink
            f = T.font(11, bold=True, mono=True)
            label = "%d" % i
            lw_ = d.textlength(label, font=f)
            d.text((x0 + cw / 2 - lw_ / 2, y + 6), label, font=f, fill=tc)
        return y + h

    def _specter_lines(self, d, th, y):
        """The lines that say what the checklist is doing, in words."""
        w = self.app.w
        disp, node = self._snap
        df = disp.get("fields") or {}
        nf = node.get("fields") or {}
        hs_req, hs_rsp = getattr(self, "_hs", (None, None))
        f = T.font(T.HINT, mono=True)

        def line(text, colour):
            nonlocal y
            d.text((14, y), text[:62], font=f, fill=colour)
            y += 12

        if df:
            line("STATE  %s" % SL.display_state_name(df.get("display_state", 0)),
                 th.fg)
        else:
            line("STATE  no display frame", th.muted)

        if nf:
            good = sum(1 for s in nf.get("steps", []) if s == SL.GOOD)
            veto = bool(nf.get("veto"))
            line("VETO   %s   %d of %d steps GOOD"
                 % ("SET" if veto else "CLEAR", good, SL.STEPS_IN_USE),
                 th.warn if veto else th.ok)
            shore = nf.get("shore_link")
            if shore is not None:
                line("SHORE  slot 13 %s   (not a checklist step)"
                     % SL.STATE_NAMES[shore],
                     th.ok if shore == SL.GOOD else th.muted)

        seq = df.get("event_sequence", 0) if df else 0
        if seq:
            echo = nf.get("event_echo") if nf else None
            step = df.get("step_index", 0xFF)
            line("EVENT  %s  %s  echo %s"
                 % (SL.event_detail(df.get("event_type", 0),
                                    df.get("event_param", 0)),
                    "step %s" % ("none" if step == 0xFF else SL.step_name(step)),
                    "cleared" if echo == seq else "OUT"),
                 th.fg)
        else:
            line("EVENT  none outstanding", th.muted)

        if hs_rsp and hs_rsp.get("fields"):
            g = hs_rsp["fields"]
            line("HSHAKE %s  checklist 0x%04X  session %d"
                 % (g["result_text"], g["checklist_id"], g["session_id"]),
                 th.ok if g["result"] == 0 else th.bad)
        if hs_req and hs_req.get("fields"):
            g = hs_req["fields"]
            line("BUILD  %s" % g.get("build_text", "unknown"),
                 th.fg if g.get("build_clean") else th.bad)
        return y

    def _section(self, d, th, y, snap, who):
        w = self.app.w
        d.text((14, y), spaced(snap["label"]),
               font=T.font(10, bold=True, mono=True), fill=th.muted)
        f = T.font(9, mono=True)
        tw = d.textlength(who, font=f)
        d.text((w - 14 - tw, y + 1), who, font=f, fill=th.muted)
        y += 14
        y = self._banner(d, th, y, snap)
        y += 6
        y = self._groups(d, th, y, snap)
        raw = snap["data"]
        if raw:
            hexes = " ".join("%02X" % b for b in raw)
            d.text((14, y + 4), hexes, font=T.font(10, mono=True),
                   fill=th.muted)
        return y + 18

    def draw_content(self, d, th):
        w, h = self.app.w, self.app.h
        if not self._snap:
            self.tick()
        disp, node = self._snap

        y = HEADER_H + 8
        y = self._section(d, th, y, disp, "the Screen talks")
        y += 8
        y = self._section(d, th, y, node, "the boat side talks")
        y += 8

        # ---- the SPECTER state: the strip, then the words ----
        y = self._steps_strip(d, th, y)
        y += 4
        y = self._specter_lines(d, th, y)
        y += 4

        # ---- bus card ----
        b = self.bus
        box = (14, y, w - 14, y + 58)
        d.rectangle(box, fill=th.card, outline=th.card_hi, width=1)
        brackets(d, (18, y + 4, w - 18, y + 54), th.card_hi, arm=8, width=1)
        d.text((26, y + 8), spaced("BUS"),
               font=T.font(10, bold=True, mono=True), fill=th.muted)
        state = b.get("can_state") or b.get("if_state") or "ABSENT"
        scol = th.ok if state == "ERROR-ACTIVE" else th.bad
        f = T.font(13, bold=True, mono=True)
        tw = d.textlength(state, font=f)
        d.text((w - 26 - tw, y + 6), state, font=f, fill=scol)
        rate = b.get("bitrate")
        d.text((26, y + 24), "%s   berr tx %s rx %s"
               % ("%d kbps" % (rate // 1000) if rate else "? kbps",
                  b.get("berr_tx"), b.get("berr_rx")),
               font=T.font(T.SUB, mono=True), fill=th.fg)
        rx, tx = b.get("rx"), b.get("tx")
        if rx and tx:
            d.text((26, y + 40), "RX %d  TX %d  err %d/%d  drop %d/%d"
                   % (rx[1], tx[1], rx[2], tx[2], rx[3], tx[3]),
                   font=T.font(T.HINT, mono=True), fill=th.muted)
        y += 58

        # ---- the node control, bottom-anchored so a finger always finds it ----
        btn_top = h - BTN_H - 16
        self._draw_node_button(d, th, btn_top)

        # ---- legend + reader health ----
        y += 8
        if y + 12 < btn_top:
            keys = ((SL.GOOD, "GOOD"), (SL.ACTIVE, "ACTIVE"),
                    (SL.FAULT, "FAULT"), (SL.PENDING, "PENDING"))
            x = 14
            lf = T.font(9, bold=True, mono=True)
            for value, name in keys:
                col = self._state_colour(th, value)
                if value == SL.PENDING:
                    d.rectangle((x, y + 2, x + 8, y + 10), outline=col,
                                width=1)
                else:
                    d.rectangle((x, y + 2, x + 8, y + 10), fill=col)
                d.text((x + 12, y + 1), name, font=lf, fill=th.muted)
                x += 14 + d.textlength(name, font=lf) + 10

        err = self.reader.error if self.reader else None
        if err or self.reader is None:
            msg = err or "no reader — %s is down" % IFACE
            d.text((14, btn_top - 14), msg[:46],
                   font=T.font(T.HINT, mono=True), fill=th.warn)

    def _draw_node_button(self, d, th, y):
        """Start/stop the bench node. Amber to stop, accent to start —
        red stays reserved for a genuine fault, per this tile's palette."""
        w = self.app.w
        on = self._sim_on
        if self._sim_busy:
            label, kind, col = "WORKING…", "normal", None
        elif on:
            label, kind, col = "NODE ■ STOP", "normal", th.warn
        else:
            label, kind, col = "START NODE → BUS", "primary", None
        b = Button((14, y, w - 14, y + BTN_H), label, kind=kind, color=col,
                   font_size=15)
        b.draw(d, th)
        self._btns["node"] = b.box

    def _in(self, key, x, y):
        box = self._btns.get(key)
        return box and box[0] <= x <= box[2] and box[1] <= y <= box[3]

    def handle_tap(self, x, y):
        if self._in("node", x, y):
            self._toggle_sim()
            return True
        return False
