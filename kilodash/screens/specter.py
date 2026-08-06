"""SPECTER — the preflight bench link, both directions at a glance.

The tile that lets the bench prove itself with no laptop attached. It
watches the two periodic frames of the SPECTER preflight link and shows,
for each direction, whether it is ALIVE or STALE, how fast it is really
arriving, whether its sequence is continuous, and the seven decoded group
states:

  DISPLAY  0x240220   the Screen talks   (the Screen is alive)
  NODE     0x248021   the boat side talks (the simulator/node is alive)

Groups are S P E1 C T E2 R; states are PENDING / ACTIVE / GOOD / FAULT.

Scope (CAN-N2K-Split-TODO, hard constraint): diagnostics only — **this
screen constructs no TX frames and has no TX surface**; its socket
(specterlink.SpecterReader, opened on entry, closed on leave — no shared RX
daemon) only ever recv()s. It watches both directions but speaks neither:
the node status shown is what another node actually put on the wire,
observed, never echoed. The bench simulator that publishes 0x248021 is
separate bench equipment, run by hand and imported by no Scottina package.
tests/test_txscan.py enforces this tree-wide.

Presentation follows the ship-instrument look (Cobb's Semiotic Standard):
a hard-edged state banner per direction with a service-state glyph, caps-
mono readouts, and a seven-cell group strip where colour carries state and
the letter carries identity — filled = reached, hollow = not yet. Red is
reserved for genuine faults: a STALE link on a bench whose whole job is to
prove that link, and a group the checklist itself reports as FAULT.
"""

import time

from .. import specterlink as SL, theme as T
from ..widgets import brackets, spaced
from .base import Screen, HEADER_H

IFACE = "can0"
IDLE_TICK = 1.0
FAST_TICK = 0.5
BUS_POLL_S = 2.0          # ip link is a subprocess; keep it off every tick


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

    # ---- lifecycle ----
    def on_enter(self):
        self._start_reader()
        self._poll_bus(force=True)
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

    def tick(self):
        self._poll_bus()
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
        b = self.bus
        rows.append({
            "label": "BUS %s" % IFACE,
            "value": self._bus_text(),
            "state": ("ok" if b.get("can_state") == "ERROR-ACTIVE"
                      else "fault" if b.get("present") else None),
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

        # ---- legend + reader health ----
        y += 8
        keys = ((SL.GOOD, "GOOD"), (SL.ACTIVE, "ACTIVE"),
                (SL.FAULT, "FAULT"), (SL.PENDING, "PENDING"))
        x = 14
        lf = T.font(9, bold=True, mono=True)
        for value, name in keys:
            col = self._state_colour(th, value)
            if value == SL.PENDING:
                d.rectangle((x, y + 2, x + 8, y + 10), outline=col, width=1)
            else:
                d.rectangle((x, y + 2, x + 8, y + 10), fill=col)
            d.text((x + 12, y + 1), name, font=lf, fill=th.muted)
            x += 14 + d.textlength(name, font=lf) + 10

        err = self.reader.error if self.reader else None
        if err or self.reader is None:
            msg = err or "no reader — %s is down" % IFACE
            d.text((14, h - 18), msg[:46], font=T.font(T.HINT, mono=True),
                   fill=th.warn)

    def handle_tap(self, x, y):
        return False
