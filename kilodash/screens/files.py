"""Files — USB offload of capture logs + decode-table exchange (DBC / NMEA).

The tile appears only while a USB stick is plugged in (device_key
"usbstick"). Everything Scottina records lands in /opt/kilodash/captures;
this screen copies those files onto the stick — one per tap, or the lot with
Copy all — and exchanges CAN decode tables (DBC, NMEA/canboat) between the
stick and /opt/kilodash/tables, the canonical place decoding tools (the
Node-RED DBC flow, Signal K/canboatjs) read them from.

Mount discipline: the stick mounts at devices.USB_MOUNT on entry and is
sync'd + unmounted on Eject and on leaving the screen, so a yanked stick
never strands a mount (lazy-detach fallback). Copies never delete the
originals — captures/ stays authoritative until you clear it yourself.

Presentation follows the ship-instrument look (Cobb's Semiotic Standard):
a hard-edged USB-state banner with a per-state glyph — hazard caps and red
on actual I/O faults only (mount failed, copy failed); ejected/read-only
are stand-by/caution — terse caps EJECT/OFFLOAD controls, a segmented
offload gauge (captures aboard the stick), and hard-edged capture rows
keyed by a square status glyph (lit = aboard, hollow = not yet).
"""

import os
import shutil
import subprocess

from PIL import Image, ImageDraw

from .. import devices, system, theme as T
from ..widgets import (Button, hazard, seg_row, spaced, state_glyph,
                       status_square)
from .base import Screen, HEADER_H

CAP_DIR = "/opt/kilodash/captures"
TABLE_DIR = "/opt/kilodash/tables"
MOUNT = devices.USB_MOUNT
DEST_SUB = "scottina"               # <stick>/scottina/{captures,tables}
# Decode-table types picked up from the stick (root or scottina/tables/):
# .dbc/.kcd/.sym = CAN signal databases, .n2k/.json/.xml = NMEA2000 (canboat)
TABLE_EXTS = (".dbc", ".kcd", ".sym", ".n2k", ".json", ".xml")

# Fixed vertical bands; x-coordinates derive from app.w at draw time
# (the panel is 320×480 portrait — never hardcode widths).
INFO_Y = HEADER_H + 6            # 50  stick card + Eject/Mount button
INFO_H = 54
ACT_Y = INFO_Y + INFO_H + 6      # 110 full-width Copy-all button
ACT_H = 48
TBL_Y = ACT_Y + ACT_H + 6        # 164 tables import | export
TBL_H = 44
STAT_Y = TBL_Y + TBL_H + 6       # 214 status strip
STAT_H = 26
LIST_TOP = STAT_Y + STAT_H + 6   # 246 scrollable capture list
ROW_H = 44                       # widgets.MIN_TOUCH — rows are tap targets


def _sh(*cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)


def _human(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def _mount_state():
    """(source device, mounted-rw) for MOUNT per /proc/mounts, else
    (None, False). rw is the mount flag, not a permission check."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                fields = line.split()
                if fields[1] == MOUNT:
                    return fields[0], "rw" in fields[3].split(",")
    except OSError:
        pass
    return None, False


def _writable():
    return _mount_state()[1]


def _mount(parts):
    """Mount the stick: try each candidate partition and keep the first that
    mounts writable; fall back to a read-only one (tables can still be
    imported from it). Returns (mounted, writable, message)."""
    os.makedirs(MOUNT, exist_ok=True)
    if os.path.ismount(MOUNT):
        return True, _writable(), "already mounted"
    ro_dev = None
    for dev in parts:
        if _sh("mount", dev, MOUNT).returncode != 0:
            continue
        if _writable():
            return True, True, f"{dev} mounted"
        if ro_dev is None:
            ro_dev = dev
        _sh("umount", MOUNT)                # keep looking for a rw partition
    if ro_dev and _sh("mount", ro_dev, MOUNT).returncode == 0:
        return True, False, f"{ro_dev} read-only"
    return False, False, "mount failed"


def _unmount():
    """Sync then unmount; a yanked stick gets a lazy detach so the mount
    point never wedges."""
    _sh("sync", timeout=30)
    if not os.path.ismount(MOUNT):
        return True, "not mounted"
    r = _sh("umount", MOUNT, timeout=30)
    if r.returncode != 0:
        _sh("umount", "-l", MOUNT)
    return True, "Ejected — safe to remove"


def _copy_captures(names):
    """Background worker: copy capture files onto the stick, then sync so
    Eject is instant. Returns (n_copied, message)."""
    dest = os.path.join(MOUNT, DEST_SUB, "captures")
    os.makedirs(dest, exist_ok=True)
    n = 0
    for name in names:
        shutil.copy2(os.path.join(CAP_DIR, name), os.path.join(dest, name))
        n += 1
    _sh("sync", timeout=60)
    return n, f"Copied {n} file{'s' if n != 1 else ''} → USB"


def _import_tables():
    """Background worker: pull decode tables (TABLE_EXTS) from the stick's
    root and scottina/tables/ into /opt/kilodash/tables."""
    os.makedirs(TABLE_DIR, exist_ok=True)
    n = 0
    for src_dir in (MOUNT, os.path.join(MOUNT, DEST_SUB, "tables")):
        try:
            names = sorted(os.listdir(src_dir))
        except OSError:
            continue
        for name in names:
            src = os.path.join(src_dir, name)
            if (name.startswith(".") or not os.path.isfile(src)
                    or not name.lower().endswith(TABLE_EXTS)):
                continue
            shutil.copy2(src, os.path.join(TABLE_DIR, name))
            n += 1
    return n, (f"Imported {n} table{'s' if n != 1 else ''}" if n
               else "No .dbc/.n2k/… tables on stick")


def _export_tables():
    """Background worker: push /opt/kilodash/tables onto the stick — the
    flat SD-export shape of TABLES.md §5, so one conversion effort feeds
    Scottina Light too: loose root files AND the converter-installed
    pgn/ store (tables + manifests), all into one flat dir.

    Alongside the flat tables, emit Light's browse-then-decode artifacts
    (TABLES.md §5b/§5c) — the index + per-PGN detail files — from the same
    shared writer the web download uses, so the two export paths agree byte
    for byte. These are derived from the store, never the loose copies."""
    dest = os.path.join(MOUNT, DEST_SUB, "tables")
    os.makedirs(dest, exist_ok=True)
    n = 0
    for src_dir in (TABLE_DIR, os.path.join(TABLE_DIR, "pgn"),
                    os.path.join(TABLE_DIR, "dbc")):
        try:
            names = sorted(os.listdir(src_dir))
        except OSError:
            continue
        for name in names:
            src = os.path.join(src_dir, name)
            # decode-table data only — tables/ also hosts the contract
            # code (validate.py, store.py), which never leaves the box
            if (name.startswith(".") or not os.path.isfile(src)
                    or not name.lower().endswith(TABLE_EXTS)):
                continue
            shutil.copy2(src, os.path.join(dest, name))
            n += 1
    idx = _export_light_index(dest)
    _sh("sync", timeout=60)
    if not n:
        return 0, "tables/ is empty — import first"
    msg = f"Exported {n} table{'s' if n != 1 else ''} → USB"
    if idx:
        msg += f" (+{idx} Light index)"
    return n, msg


def _export_light_index(dest):
    """Emit Light's index + per-PGN detail (TABLES.md §5b/§5c) for every
    verified store table into the flat export dir. Sorted by table name so
    two runs from the same store produce byte-identical output. Returns the
    count of indexed tables."""
    from tables import store, validate, lightindex
    n = 0
    for t in sorted(store.list_tables(), key=lambda t: t["name"]):
        if not t["verified"]:
            continue
        try:
            if lightindex.write_table_artifacts(dest, t["name"]):
                n += 1
        except validate.TableInvalid:
            continue          # a table that no longer validates isn't indexed
    return n


class FilesScreen(Screen):
    title = "Files"
    tile_id = "files"
    glyph = "files"
    tile_color_key = "accent"
    device_key = "usbstick"
    scrollable = True

    def __init__(self, app):
        super().__init__(app)
        self.tick_interval = 2.0
        self.mounted = False
        self.rw = False
        self.parts = []
        self.files = []                  # (name, size) newest first
        self.on_stick = set()            # names already on the stick
        self.task = None                 # one background job at a time
        self.status = ""
        self._btns = {}

    # ------------------------------------------------------------- mount/media
    def on_enter(self):
        self.scroll = 0
        self.parts = devices.usb_stick_partitions()
        if self.parts:
            self.mounted, self.rw, self.status = _mount(self.parts)
        else:
            self.mounted = self.rw = False
            self.status = "No USB stick found"
        self._refresh_lists()

    def on_leave(self):
        # leaving mid-copy: umount goes EBUSY → lazy detach; the worker
        # thread errors out harmlessly and the partial file stays on the
        # stick (source in captures/ is untouched either way)
        if self.mounted:
            _unmount()
            self.mounted = False

    def _eject(self):
        if self._busy():
            self.app.toast("Wait for the copy to finish")
            return
        if self.mounted:
            self.mounted = self.rw = False
            _, self.status = _unmount()
            self.app.toast(self.status)
        elif self.parts:
            self.mounted, self.rw, self.status = _mount(self.parts)
            self._refresh_lists()

    def _free_space(self):
        try:
            st = os.statvfs(MOUNT)
            return st.f_bavail * st.f_frsize
        except OSError:
            return None

    # ------------------------------------------------------------------- data
    def _refresh_lists(self):
        try:
            names = [n for n in os.listdir(CAP_DIR)
                     if not n.startswith(".") and n != ".gitkeep"
                     and os.path.isfile(os.path.join(CAP_DIR, n))]
        except OSError:
            names = []
        entries = []
        for n in names:
            st = os.stat(os.path.join(CAP_DIR, n))
            entries.append((n, st.st_size, st.st_mtime))
        entries.sort(key=lambda e: e[2], reverse=True)
        self.files = [(n, sz) for n, sz, _ in entries]
        self.on_stick = set()
        dest = os.path.join(MOUNT, DEST_SUB, "captures")
        if self.mounted:
            for n, sz in self.files:
                try:
                    if os.stat(os.path.join(dest, n)).st_size == sz:
                        self.on_stick.add(n)
                except OSError:
                    continue

    # ------------------------------------------------------------------- jobs
    def _busy(self):
        return self.task is not None and not self.task.done

    def _start(self, fn, *args, label="Working…", writes=True):
        if self._busy():
            self.app.toast("Already copying")
            return
        if not self.mounted:
            self.app.toast("Stick not mounted")
            return
        if writes and not self.rw:
            self.app.toast("Stick is read-only")
            return
        self.status = label
        self.task = system.Task(fn, *args)

    def tick(self):
        if self.task and self.task.done:
            res = self.task.result or (0, f"Failed: {self.task.error}")
            self.task = None
            _, self.status = res
            self.app.toast(self.status)
            self._refresh_lists()
            return True
        return False

    # --------------------------------------------------------------- drawing
    def content_area(self):
        return (0, LIST_TOP, self.app.w, self.app.h - LIST_TOP)


    def model_rows(self):
        """Stick state and capture inventory. Never calls _refresh_lists() —
        it stats every file; tick()/on_enter already populated the caches."""
        busy = self._busy()
        status = str(self.status or "—")
        if busy:
            mount, mstate = "TRANSFERRING", "caution"
        elif not self.mounted:
            mount, mstate = "STANDING BY", None
        elif self.rw:
            mount, mstate = "MOUNTED RW", "ok"
        else:
            mount, mstate = "READ-ONLY", "caution"
        bad = status.startswith("Failed") or status == "mount failed"
        return [
            {"label": "USB STICK", "value": mount,
             "state": "fault" if bad else mstate},
            {"label": "PARTITIONS", "value": str(len(self.parts or [])),
             "state": None},
            {"label": "CAPTURES", "value": str(len(self.files or [])),
             "state": None},
            {"label": "ON STICK", "value": str(len(self.on_stick or set())),
             "state": None},
            {"label": "STATUS", "value": status,
             "state": "fault" if bad else None},
        ]


    def model_buttons(self):
        """The panel gates these by storing None in the hit-box during draw;
        handle_button never hit-tests, so the gates are restated here."""
        busy = self._busy()
        todo = [n for n, _ in (self.files or []) if n not in (self.on_stick or ())]
        return [
            {"id": "eject", "label": "EJECT" if self.mounted else "MOUNT",
             "enabled": bool(self.parts) and not busy, "confirm": False},
            {"id": "offload_all", "label": "OFFLOAD ALL",
             "enabled": bool(self.rw) and bool(todo) and not busy,
             "confirm": False},
            {"id": "tables_import", "label": "IMPORT TABLES",
             "enabled": bool(self.mounted) and not busy, "confirm": False},
            {"id": "tables_export", "label": "EXPORT TABLES",
             "enabled": bool(self.rw) and not busy, "confirm": False},
        ]

    def handle_button(self, bid):
        # _eject()/_start() re-guard busy, mounted and read-only internally
        # and toast on refusal, so routing straight to them is safe.
        if bid == "eject":
            self._eject(); return True
        if bid == "offload_all":
            todo = [n for n, _ in (self.files or [])
                    if n not in (self.on_stick or ())]
            if todo:
                self._start(_copy_captures, todo, label="OFFLOAD ALL")
            return True
        if bid == "tables_import":
            self._start(_import_tables, label="IMPORT TABLES", writes=False)
            return True
        if bid == "tables_export":
            self._start(_export_tables, label="EXPORT TABLES")
            return True
        return False

    def draw_content(self, d, th):
        w, h = self.app.w, self.app.h
        self._btns = {}
        self._draw_list(d, th, w, h)
        d.rectangle((0, HEADER_H, w, LIST_TOP), fill=th.bg)

        # ---- USB state banner + EJECT/MOUNT. Red + hazard caps only for
        #      actual I/O faults; ejected/read-only are stand-by/caution. ----
        busy = self._busy()
        fault = (self.status.startswith("Failed")
                 or self.status == "mount failed")
        if busy:
            label, col, glyph = "TRANSFERRING", th.accent, "spin"
        elif fault:
            label, col, glyph = "FAULT", th.bad, "fault"
        elif self.mounted and self.rw:
            label, col, glyph = "USB READY", th.ok, "up"
        elif self.mounted:
            label, col, glyph = "READ-ONLY", th.warn, "standby"
        else:
            label, col, glyph = "STANDING BY", th.muted, "standby"
        y0, y1 = INFO_Y, INFO_Y + INFO_H
        d.rectangle((12, y0, w - 12, y1), fill=th.card, outline=col, width=2)
        if fault:                        # faults wear the caution band
            hazard(d, (14, y0 + 3, w - 14, y0 + 9), col, step=8, width=2)
        state_glyph(d, glyph, 32, (y0 + y1) // 2, 11, col)
        cx = (48 + w - 104) / 2          # centre of the glyph..button gap
        f = T.font(14, bold=True, mono=True)
        lw = d.textlength(label, font=f)
        d.text((cx - lw / 2, y0 + 12), label, font=f, fill=col)
        if self.mounted:
            src, _ = _mount_state()
            free = self._free_space()
            sub = os.path.basename(src or "?").upper()
            if not self.rw:
                sub += " · RO"
            elif free is not None:
                sub += f" · {_human(free)} FREE"
        elif fault:
            sub = self.status[:24].upper()
        elif self.parts:
            sub = "TAP MOUNT TO ATTACH"
        else:
            sub = "NO USB STICK"
        fs = T.font(9, bold=True, mono=True)
        sw = d.textlength(sub, font=fs)
        d.text((cx - sw / 2, y0 + 34), sub, font=fs, fill=th.muted)
        # eject is a stand-down, not a fault: amber, never red
        ej = Button((w - 100, INFO_Y + 8, w - 22, INFO_Y + INFO_H - 8),
                    "EJECT" if self.mounted else "MOUNT",
                    kind="primary", color=th.warn if self.mounted else None,
                    font_size=15)
        ej.enabled = bool(self.parts) and not busy
        ej.draw(d, th)
        self._btns["eject"] = ej.box if ej.enabled else None

        # offload all
        todo = [n for n, _ in self.files if n not in self.on_stick]
        label = ("TRANSFERRING" if busy
                 else f"OFFLOAD {len(todo)} → USB" if todo
                 else "ALL LOGS ON STICK")
        all_btn = Button((14, ACT_Y, w - 14, ACT_Y + ACT_H), label,
                         kind="primary", font_size=17)
        all_btn.enabled = self.rw and bool(todo) and not busy
        all_btn.draw(d, th)
        self._btns["all"] = all_btn.box if all_btn.enabled else None

        # decode tables
        for key, x0, x1, label, ok in (
                ("t_in", 14, w // 2 - 4, "TABLES ← USB", self.mounted),
                ("t_out", w // 2 + 4, w - 14, "TABLES → USB", self.rw)):
            b = Button((x0, TBL_Y, x1, TBL_Y + TBL_H), label, font_size=14)
            b.enabled = ok and not busy
            b.draw(d, th)
            self._btns[key] = b.box if b.enabled else None

        # status strip + segmented offload gauge (captures aboard / total)
        d.rectangle((14, STAT_Y, w - 14, STAT_Y + STAT_H), fill=th.card,
                    outline=th.card_hi, width=1)
        d.text((22, STAT_Y + 7), self.status[:30].upper(),
               font=T.font(T.SUB, mono=True), fill=th.muted)
        if self.files:
            segs = 8
            lit = round(segs * len(self.on_stick) / len(self.files))
            if self.on_stick and lit == 0:
                lit = 1
            seg_row(d, w - 22 - segs * 10 + 2, STAT_Y + 8, lit, segs,
                    th.ok, th.card_hi, seg_h=10)

    def _draw_list(self, d, th, w, h):
        pane_h = h - LIST_TOP
        if not self.files:
            self.content_h = pane_h
            d.rectangle((0, LIST_TOP, w, h), fill=th.bg)
            d.text((24, LIST_TOP + 12), spaced("NO CAPTURE LOGS"),
                   font=T.font(11, bold=True, mono=True), fill=th.muted)
            d.text((24, LIST_TOP + 32), "captures/ is empty",
                   font=T.font(T.SUB, mono=True), fill=th.muted)
            return
        self.content_h = max(len(self.files) * ROW_H + 4, pane_h)
        surf = Image.new("RGB", (w, self.content_h), th.bg)
        sd = ImageDraw.Draw(surf)
        f = T.font(13, mono=True)
        fs = T.font(T.SUB, mono=True)
        for i, (name, size) in enumerate(self.files):
            y = i * ROW_H
            copied = name in self.on_stick
            sd.rectangle((14, y + 2, w - 14, y + ROW_H - 2),
                         fill=th.card_hi if copied else th.card)
            # square status glyph: lit = aboard the stick, hollow = not yet
            status_square(sd, (24, y + 16, 36, y + 28),
                          "lit" if copied else "hollow",
                          th.ok if copied else th.muted)
            sd.text((46, y + 6), name[:28], font=f,
                    fill=th.muted if copied else th.fg)
            sub = _human(size) + (" · ABOARD" if copied
                                  else " · TAP TO OFFLOAD")
            sd.text((46, y + 26), sub, font=fs,
                    fill=th.ok if copied else th.muted)
        self.paste_list(LIST_TOP, pane_h, surf)

    # ------------------------------------------------------------------ input
    def _in(self, key, x, y):
        box = self._btns.get(key)
        return box and box[0] <= x <= box[2] and box[1] <= y <= box[3]

    def handle_tap(self, x, y):
        if self._in("eject", x, y):
            self._eject()
            return True
        if self._in("all", x, y):
            todo = [n for n, _ in self.files if n not in self.on_stick]
            self._start(_copy_captures, todo,
                        label=f"Copying {len(todo)} files…")
            return True
        if self._in("t_in", x, y):
            self._start(_import_tables, label="Importing tables…",
                        writes=False)
            return True
        if self._in("t_out", x, y):
            self._start(_export_tables, label="Exporting tables…")
            return True
        if y >= LIST_TOP and self.files:
            i = int((y - LIST_TOP + self.scroll) // ROW_H)
            if 0 <= i < len(self.files):
                name, _ = self.files[i]
                if name in self.on_stick:
                    self.app.toast("Already on stick ✓")
                else:
                    self._start(_copy_captures, [name],
                                label=f"Copying {name[:24]}…")
                return True
        return False
