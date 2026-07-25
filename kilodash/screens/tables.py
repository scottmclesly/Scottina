"""Tables — remote control + mirror for the decode-table store.

Thin by design (TABLES.md §4): this tile performs **no conversion and no
parsing beyond reading manifests** — validate/ingest/manage live in the
converter service (kilodash/tableconv.py); the tile starts/stops that
service and mirrors the store, so the two views can never disagree.

Presentation follows the ship-instrument look ratified on the Pomodoro
refactor (Cobb's Semiotic Standard): a hard-edged converter-state banner
with a per-state glyph — hazard end-caps on faults only — a bracket-framed
REMOTE UPLINK instrument area carrying the URL **plus QR code** for the
advertised address (net.advertise_addr, eth0-preferred), a segmented
idle-out gauge that extinguishes as the converter burns toward its
timeout, and hard-edged inventory rows keyed by a square status glyph
(lit = enabled, hollow = disabled, slashed amber = unverified). Red stays
reserved for faults; stopping the service is an amber stand-down, not red.

Inventory taps: row toggles enable (the single sanctioned non-converter
write: an atomic manifest flip), ✕ strikes the table after a confirm
re-tap.

Always visible — no hardware gate; tables are software. Entering the tile
starts the service (on-demand lifecycle); leaving does NOT stop it — the
converter idles itself out after N minutes so a Mac-side session survives
kilodash navigation.
"""

import math
import time

from PIL import Image, ImageDraw

from .. import net, theme as T, webapp
from ..widgets import (Button, brackets, hazard, seg_row, spaced,
                       state_glyph, status_square)
from .base import Screen, HEADER_H

# the table store lives at the repo root (TABLES.md §1)
from tables import store

SERVICE = "kilodash-tables.service"
IDLE_DEFAULT_MIN = 15                # mirrors tableconv.IDLE_DEFAULT_MIN

# instrument geometry (320×480 portrait, header above)
BANNER_Y, BANNER_H = HEADER_H + 8, 38        # 52..90  converter-state banner
FRAME = (20, 98, None, 256)                  # uplink frame; x1 filled at draw
GAUGE_Y = 262                                # idle-out segment gauge band
GAUGE_SEGS = IDLE_DEFAULT_MIN                # one segment per idle minute
BTN_Y, BTN_H = 284, 44
SECT_Y = 336                                 # INVENTORY section label
LIST_TOP = 352
ROW_H = 40
GAUGE_BAND = (258, 278)                      # dirty band for idle-only ticks

STATES = {
    webapp.UP:       {"label": "CONVERTER UP", "col": "ok",     "glyph": "up"},
    webapp.STARTING: {"label": "SPINNING UP",  "col": "accent", "glyph": "spin"},
    webapp.ERROR:    {"label": "FAULT",        "col": "bad",    "glyph": "fault"},
    webapp.STOPPED:  {"label": "STANDING BY",  "col": "muted",  "glyph": "standby"},
}


def _wrap(text, width, max_lines):
    """Word-wrap into at most max_lines of `width` chars (tail dropped)."""
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
            if len(lines) == max_lines:
                return lines
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def _qr_matrix(text):
    """QR module matrix for a URL, or None if qrcode isn't available."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1,
                           error_correction=qrcode.ERROR_CORRECT_M)
        qr.add_data(text)
        qr.make(fit=True)
        return qr.get_matrix()
    except Exception:               # noqa: BLE001 — QR is a nicety
        return None


class TablesScreen(Screen):
    title = "Tables"
    tile_id = "tables"
    glyph = "tables"
    tile_color_key = "accent"
    scrollable = True

    def __init__(self, app):
        super().__init__(app)
        self.tick_interval = 1.0
        self.web = webapp.WebApp("Tables", net.TABLECONV_PORT,
                                 service=SERVICE)
        self.inv = []
        self.idle_secs = None            # from the service's /status
        self._status_t = 0.0
        self.confirm = None              # (name, until) remove confirm state
        self._qr_cache = (None, None)    # (url, PIL image)
        self._btns = {}
        self._sig = None
        self._idle_sig = None

    # --------------------------------------------------------------- lifecycle
    def on_enter(self):
        # tile-open starts the service (spec); leaving never stops it —
        # the converter's own idle timeout owns shutdown
        if not self.web.running:
            self.web.launch()
        self.inv = store.list_tables()
        self.idle_secs = None
        self.confirm = None
        self._sig = None
        self._idle_sig = None

    def tick(self):
        changed = self.web.poll()
        now = time.monotonic()
        if self.web.state == webapp.UP and now - self._status_t >= 5.0:
            self._status_t = now
            st = webapp.http_json(
                f"http://127.0.0.1:{self.web.port}/status", timeout=0.8)
            self.idle_secs = st.get("idle_secs") if st else None
        inv = store.list_tables()
        if self.confirm and now > self.confirm[1]:
            self.confirm = None
            changed = True
        sig = (self.web.state,
               [(t["name"], t["enabled"], t["verified"]) for t in inv])
        idle_sig = self.idle_secs and int(self.idle_secs // 10)
        self.inv = inv
        if sig != self._sig:
            self._sig = sig
            self._idle_sig = idle_sig
            return True
        if idle_sig != self._idle_sig:
            # only the idle-out gauge moved: repaint just its band
            self._idle_sig = idle_sig
            if not changed:
                self.report_dirty((0, GAUGE_BAND[0], self.app.w,
                                   GAUGE_BAND[1]))
            return True
        return changed

    # --------------------------------------------------------------- drawing
    def content_area(self):
        return (0, LIST_TOP, self.app.w, self.app.h - LIST_TOP)


    def model_rows(self):
        """Converter service state plus the table inventory tick() caches.
        Never calls web.url() — it composes via lan_ip(), which opens a
        socket."""
        w = self.web
        st = getattr(w, "state", "?")
        inv = self.inv or []
        rows = [
            {"label": "SERVICE", "value": str(st).upper(),
             "state": {"up": "ok", "starting": "caution",
                       "error": "fault"}.get(st)},
            {"label": "PORT", "value": str(getattr(w, "port", "—")),
             "state": None},
            {"label": "TABLES", "value": str(len(inv)), "state": None},
            {"label": "ENABLED",
             "value": str(sum(1 for t in inv if t.get("enabled"))),
             "state": None},
            {"label": "VERIFIED",
             "value": str(sum(1 for t in inv if t.get("verified"))),
             "state": None},
        ]
        if self.idle_secs is not None:
            rows.append({"label": "IDLE OUT",
                         "value": f"{int(self.idle_secs)}S", "state": None})
        return rows


    def model_buttons(self):
        """`power` stops/starts the converter service. Stop is confirmed here
        even though the panel does not confirm it, for the same reason the
        rest of the destructive set is (§10): a POST is not a physical tap."""
        up = getattr(self.web, "state", None) == webapp.UP
        return [{"id": "power", "label": "STOP" if up else "START",
                 "enabled": True, "confirm": bool(up)}]

    def handle_button(self, bid):
        if bid == "power":
            if getattr(self.web, "state", None) == webapp.UP:
                self.web.stop()
            else:
                self.web.launch()
            return True
        return False

    def draw_content(self, d, th):
        w, h = self.app.w, self.app.h
        self._btns = {}
        self._draw_inventory(d, th, w, h)
        d.rectangle((0, HEADER_H, w, LIST_TOP), fill=th.bg)

        st = STATES[self.web.state]
        col = getattr(th, st["col"])
        self._draw_banner(d, th, st, col)
        self._draw_frame(d, th)
        self._draw_gauge(d, th)

        running = self.web.running
        b = Button((14, BTN_Y, w - 14, BTN_Y + BTN_H),
                   "STOP SERVICE" if running else "START SERVICE",
                   kind="primary", color=th.warn if running else None,
                   font_size=16)
        b.enabled = self.web.installed()
        b.draw(d, th)
        self._btns["power"] = b

        lab = spaced("INVENTORY") + f" · {len(self.inv)}"
        d.text((20, SECT_Y), lab, font=T.font(10, bold=True, mono=True),
               fill=th.muted)

    def _draw_banner(self, d, th, st, col):
        w = self.app.w
        y0, y1 = BANNER_Y, BANNER_Y + BANNER_H
        d.rectangle((12, y0, w - 12, y1), fill=th.card, outline=col, width=2)
        if self.web.state == webapp.ERROR:   # faults wear the caution caps
            for zx in (50, w - 16 - 46):
                hazard(d, (zx, y0 + 4, zx + 46, y1 - 4), col)
        state_glyph(d, st["glyph"], 34, (y0 + y1) // 2, 11, col)
        f = T.font(17, bold=True, mono=True)
        lw = d.textlength(st["label"], font=f)
        d.text(((w - lw) / 2, y0 + (BANNER_H - 17) / 2 - 2), st["label"],
               font=f, fill=col)

    def _draw_frame(self, d, th):
        """Bracket-framed REMOTE UPLINK instrument: URL + QR while the
        converter is reachable; standby orders or the fault report otherwise."""
        w = self.app.w
        x0, y0, x1, y1 = FRAME[0], FRAME[1], w - FRAME[0], FRAME[3]
        brackets(d, (x0, y0, x1, y1), th.muted)
        d.text((x0 + 10, y0 + 8), spaced("REMOTE UPLINK"),
               font=T.font(10, bold=True, mono=True), fill=th.muted)
        state = self.web.state
        if state in (webapp.UP, webapp.STARTING):
            url = f"http://{net.advertise_addr()}:{self.web.port}/"
            d.text((x0 + 10, y0 + 26), url[:32],
                   font=T.font(13, bold=True, mono=True),
                   fill=th.accent if state == webapp.UP else th.muted)
            self._draw_qr(d, th, x0, x1, y0 + 46, y1 - (y0 + 46) - 6, url)
        elif state == webapp.ERROR and not self.web.installed():
            # installed-but-failed is the only real fault; if the unit is
            # absent, never point at a journal that cannot exist — name the
            # remedy instead (mirrors the launch()-side guard, TABLES.md §4).
            d.text((x0 + 10, y0 + 32), spaced("CONVERTER NOT INSTALLED"),
                   font=T.font(11, bold=True, mono=True), fill=th.warn)
            f = T.font(T.SUB, mono=True)
            for i, line in enumerate((
                    "The tables converter service is",
                    "not on this box yet. Install it:")):
                d.text((x0 + 10, y0 + 56 + i * 15), line, font=f, fill=th.fg)
            d.text((x0 + 10, y1 - 24), "sudo setup/install-tables.sh",
                   font=T.font(T.SUB, mono=True), fill=th.warn)
        elif state == webapp.ERROR:
            d.text((x0 + 10, y0 + 32), spaced("CHECK SYSTEMD LOG"),
                   font=T.font(11, bold=True, mono=True), fill=th.bad)
            f = T.font(T.SUB, mono=True)
            for i, line in enumerate(_wrap(self.web.message or "", 40, 3)):
                d.text((x0 + 10, y0 + 56 + i * 15), line, font=f, fill=th.fg)
            d.text((x0 + 10, y1 - 24), f"journalctl -u {SERVICE}"[:40],
                   font=T.font(T.SUB, mono=True), fill=th.muted)
        else:
            f = T.font(13, bold=True, mono=True)
            lab = spaced("NO UPLINK")
            lw = d.textlength(lab, font=f)
            d.text(((w - lw) / 2, y0 + 36), lab, font=f, fill=th.muted)
            fp = T.font(T.SUB, mono=True)
            for i, line in enumerate((
                    "START THE CONVERTER TO TURN",
                    "VENDOR PDFS INTO PGN DECODE",
                    "TABLES — REVIEW IN A BROWSER",
                    "(TABLES.md CONTRACT).")):
                tw = d.textlength(line, font=fp)
                d.text(((w - tw) / 2, y0 + 66 + i * 15), line, font=fp,
                       fill=th.muted)
            if not self.web.installed():
                m = "not installed — sudo setup/install-tables.sh"
                fw = T.font(T.SUB, mono=True)
                tw = d.textlength(m, font=fw)
                d.text(((w - tw) / 2, y1 - 22), m, font=fw, fill=th.warn)

    def _draw_qr(self, d, th, x0, x1, top, avail, url):
        """QR of the advertised URL — white card, black modules, so any
        phone camera reads it off the panel regardless of theme."""
        cached_url, img = self._qr_cache
        if cached_url != url:
            m = _qr_matrix(url)
            if m is None:
                self._qr_cache = (url, None)
            else:
                n = len(m)
                scale = max(1, int(avail // n))
                img = Image.new("RGB", (n * scale + 8, n * scale + 8),
                                (255, 255, 255))
                qd = ImageDraw.Draw(img)
                for ry, row in enumerate(m):
                    for rx, mod in enumerate(row):
                        if mod:
                            qx = 4 + rx * scale
                            qy = 4 + ry * scale
                            qd.rectangle((qx, qy, qx + scale - 1,
                                          qy + scale - 1), fill=(0, 0, 0))
                self._qr_cache = (url, img)
        _, img = self._qr_cache
        if img is None:
            d.text((x0 + 10, top + 8), "qrcode lib missing",
                   font=T.font(11, mono=True), fill=th.muted)
            return
        self._img.paste(img, (x1 - 8 - img.width,
                              top + max(0, (avail - img.height) // 2)))
        d.text((x0 + 10, top + avail // 2 - 6), spaced("SCAN") + " →",
               font=T.font(11, bold=True, mono=True), fill=th.muted)

    def _draw_gauge(self, d, th):
        """Idle-out chronometer: one segment per minute left before the
        converter stands itself down; extinguishes left to right."""
        if self.web.state != webapp.UP or self.idle_secs is None:
            return
        w = self.app.w
        left = max(0, IDLE_DEFAULT_MIN * 60 - self.idle_secs)
        col = th.warn if left <= 120 else th.ok
        d.text((20, GAUGE_Y + 1), spaced("IDLE-OUT"),
               font=T.font(9, bold=True, mono=True), fill=th.muted)
        d.text((108, GAUGE_Y), f"{int(left // 60)}:{int(left % 60):02d}",
               font=T.font(10, bold=True, mono=True), fill=th.fg)
        lit = min(GAUGE_SEGS, math.ceil(left / 60))
        x = w - 20 - GAUGE_SEGS * 10 + 2
        seg_row(d, x, GAUGE_Y, lit, GAUGE_SEGS, col, th.card_hi)

    def _draw_inventory(self, d, th, w, h):
        pane_h = h - LIST_TOP
        if not self.inv:
            self.content_h = pane_h
            d.rectangle((0, LIST_TOP, w, h), fill=th.bg)
            d.text((20, LIST_TOP + 10), spaced("NO TABLES LOADED"),
                   font=T.font(12, bold=True, mono=True), fill=th.muted)
            d.text((20, LIST_TOP + 32),
                   "convert a PDF, or drop JSON via the Files tile",
                   font=T.font(T.SUB, mono=True), fill=th.muted)
            return
        self.content_h = max(len(self.inv) * ROW_H + 4, pane_h)
        surf = Image.new("RGB", (w, self.content_h), th.bg)
        sd = ImageDraw.Draw(surf)
        fn = T.font(13, bold=True, mono=True)
        fs = T.font(T.SUB, mono=True)
        for i, t in enumerate(self.inv):
            y = i * ROW_H
            confirm = self.confirm and self.confirm[0] == t["name"]
            sd.rectangle((14, y + 1, w - 14, y + ROW_H - 1),
                         fill=th.card_hi if t["enabled"] else th.card)
            # square status glyph: lit / hollow / slashed amber
            gb = (24, y + 14, 36, y + 26)
            if not t["verified"]:
                status_square(sd, gb, "slash", th.warn)
            elif t["enabled"]:
                status_square(sd, gb, "lit", th.ok)
            else:
                status_square(sd, gb, "hollow", th.muted)
            sd.text((46, y + 4), t["name"][:22].upper(), font=fn,
                    fill=th.fg if t["enabled"] else th.muted)
            n = t["pgn_count"]
            if not t["verified"]:
                sub, sc = "UNVERIFIED — REINGEST", th.warn
            elif t["enabled"]:
                sub, sc = f"{n} PGN · ENABLED", th.ok
            else:
                sub, sc = f"{n} PGN · DISABLED", th.muted
            sd.text((46, y + 23), sub, font=fs, fill=sc)
            # strike zone (right edge): ✕, or the confirm state
            if confirm:
                sd.rectangle((w - 84, y + 4, w - 18, y + ROW_H - 4),
                             fill=th.bad)
                fc = T.font(12, bold=True)
                tw = sd.textlength("SURE?", font=fc)
                sd.text((w - 51 - tw / 2, y + 12), "SURE?", font=fc,
                        fill=th.ink)
            else:
                sd.text((w - 40, y + 8), "✕", font=T.font(18, bold=True),
                        fill=th.muted)
        self.paste_list(LIST_TOP, pane_h, surf)

    # ------------------------------------------------------------------ input
    def handle_tap(self, x, y):
        b = self._btns.get("power")
        if b and b.hit(x, y):
            if self.web.running:
                self.web.stop()
            else:
                self.web.launch()
            return True
        if y < LIST_TOP or not self.inv:
            return False
        i = int((y - LIST_TOP + self.scroll) // ROW_H)
        if not 0 <= i < len(self.inv):
            return False
        t = self.inv[i]
        remove_zone = x > self.app.w - 82
        if remove_zone:
            if self.confirm and self.confirm[0] == t["name"]:
                store.remove(t["name"])
                self.confirm = None
                self.app.toast(f"Struck {t['name']} from inventory")
            else:
                self.confirm = (t["name"], time.monotonic() + 3.0)
            self.inv = store.list_tables()
            return True
        # enable/disable: the manifest-only atomic flip (TABLES.md §4)
        if not t["verified"]:
            self.app.toast("Unverified — re-ingest via the converter")
            return True
        new = store.set_enabled(t["name"], not t["enabled"])
        if new is None:
            self.app.toast("No manifest — re-ingest via the converter")
        self.inv = store.list_tables()
        return True
