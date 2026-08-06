"""Semiotic-standard-inspired pictograms for the launcher tiles.

Bold, geometric, monochrome glyphs in the spirit of Ron Cobb's "Semiotic
Standard" (the Alien corridor icons): circles, triangles, bars and hard
diagonals — one symbol per subsystem, readable at arm's length. Screens name
their glyph via a `glyph` class attribute; unknown/absent keys fall back to
the original filled dot so new screens degrade gracefully.

All glyphs draw centered on (cx, cy) inside radius r, single colour, PIL
primitives only.
"""

import math


def _lw(r):
    return max(2, round(r / 5))


def _pt(cx, cy, r, deg):
    """Point on the circle at PIL-style angle (0° = 3 o'clock, clockwise)."""
    a = math.radians(deg)
    return (cx + r * math.cos(a), cy + r * math.sin(a))


def _dot(d, x, y, rr, color):
    d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=color)


def _circle(d, cx, cy, rr, color, lw):
    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=color, width=lw)


# ---------------------------------------------------------------- glyphs ----
def _lan(d, cx, cy, r, c):
    """Node mesh: hub with three linked stations."""
    lw = _lw(r)
    sat = [_pt(cx, cy, r * 0.72, a) for a in (270, 30, 150)]
    for x, y in sat:
        d.line((cx, cy, x, y), fill=c, width=lw)
    _dot(d, cx, cy, r * 0.28, c)
    for x, y in sat:
        _dot(d, x, y, r * 0.2, c)


def _wifi(d, cx, cy, r, c):
    """Point source with two expanding arcs."""
    lw = _lw(r)
    ox, oy = cx, cy + r * 0.55
    _dot(d, ox, oy, r * 0.18, c)
    for f in (0.55, 1.0):
        rr = r * f
        d.arc((ox - rr, oy - rr, ox + rr, oy + rr), 215, 325, fill=c, width=lw)


def _sdr(d, cx, cy, r, c):
    """Mast radiating both sides."""
    lw = _lw(r)
    tx, ty = cx, cy - r * 0.45
    d.line((cx, cy + r, tx, ty), fill=c, width=lw)
    _dot(d, tx, ty, r * 0.16, c)
    for f in (0.5, 0.9):
        rr = r * f
        d.arc((tx - rr, ty - rr, tx + rr, ty + rr), 145, 215, fill=c, width=lw)
        d.arc((tx - rr, ty - rr, tx + rr, ty + rr), 325, 35, fill=c, width=lw)


def _wifisniff(d, cx, cy, r, c):
    """Receiver arcs under the hard diagonal hazard bar (passive/monitor)."""
    _wifi(d, cx, cy, r, c)
    lw = _lw(r) + 1
    a = r * 0.95
    d.line((cx - a, cy + a, cx + a, cy - a), fill=c, width=lw)


def _can(d, cx, cy, r, c):
    """Differential bus pair with tapped nodes."""
    lw = _lw(r)
    y0, y1 = cy - r * 0.3, cy + r * 0.3
    d.line((cx - r, y0, cx + r, y0), fill=c, width=lw)
    d.line((cx - r, y1, cx + r, y1), fill=c, width=lw)
    for fx, up in ((-0.55, True), (0.0, False), (0.55, True)):
        x = cx + r * fx
        y = y0 - r * 0.45 if up else y1 + r * 0.45
        d.line((x, y0 if up else y1, x, y), fill=c, width=lw)
        _dot(d, x, y, r * 0.16, c)


def _specter(d, cx, cy, r, c):
    """Two nodes answering each other: opposed arrowheads on one link, with
    the preflight checklist ticks stacked beside them. The link is the
    subject, so both directions are drawn — that is the whole tile."""
    lw = _lw(r)
    y0, y1 = cy - r * 0.5, cy + r * 0.5
    a = r * 0.34
    # upper lane, pointing right; lower lane, pointing left
    d.line((cx - r, y0, cx + r * 0.75, y0), fill=c, width=lw)
    d.polygon(((cx + r, y0), (cx + r * 0.75, y0 - a * 0.5),
               (cx + r * 0.75, y0 + a * 0.5)), fill=c)
    d.line((cx + r, y1, cx - r * 0.75, y1), fill=c, width=lw)
    d.polygon(((cx - r, y1), (cx - r * 0.75, y1 - a * 0.5),
               (cx - r * 0.75, y1 + a * 0.5)), fill=c)
    # the two endpoints
    _dot(d, cx - r, y0, r * 0.17, c)
    _dot(d, cx + r, y1, r * 0.17, c)


def _n2k(d, cx, cy, r, c):
    """Decoded gauge over the bus pair: raw wire below, meaning above."""
    lw = _lw(r)
    gy = cy + r * 0.65
    d.line((cx - r, gy, cx + r, gy), fill=c, width=lw)
    d.line((cx - r, gy + r * 0.3, cx + r, gy + r * 0.3), fill=c, width=lw)
    gr = r * 0.85
    d.arc((cx - gr, cy - r * 0.9, cx + gr, cy - r * 0.9 + 2 * gr),
          180, 360, fill=c, width=lw)
    nx, ny = _pt(cx, cy - r * 0.9 + gr, gr * 0.75, 235)
    d.line((cx, cy - r * 0.9 + gr, nx, ny), fill=c, width=lw)
    _dot(d, cx, cy - r * 0.9 + gr, r * 0.14, c)


def _tables(d, cx, cy, r, c):
    """Decode table: header band + cell grid."""
    lw = _lw(r)
    x0, y0 = cx - r * 0.95, cy - r * 0.8
    x1, y1 = cx + r * 0.95, cy + r * 0.8
    hh = (y1 - y0) * 0.3
    d.rectangle((x0, y0, x1, y0 + hh), fill=c)
    d.rectangle((x0, y0, x1, y1), outline=c, width=lw)
    for f in (1, 2):
        y = y0 + hh + (y1 - y0 - hh) * f / 3
        d.line((x0, y, x1, y), fill=c, width=lw)
    d.line((cx - r * 0.25, y0 + hh, cx - r * 0.25, y1), fill=c, width=lw)


def _i2c(d, cx, cy, r, c):
    """Address matrix with one responding device."""
    step = r * 0.62
    for gr in (-1, 0, 1):
        for gc in (-1, 0, 1):
            x, y = cx + gc * step, cy + gr * step
            if gr == 0 and gc == 1:
                s = r * 0.3
                d.rectangle((x - s, y - s, x + s, y + s), fill=c)
            else:
                _dot(d, x, y, r * 0.12, c)


def _serial(d, cx, cy, r, c):
    """Opposed TX/RX triangles across the gate (airlock homage)."""
    lw = _lw(r)
    d.line((cx, cy - r * 0.9, cx, cy + r * 0.9), fill=c, width=lw)
    h = r * 0.52
    d.polygon([(cx - r, cy - h), (cx - r, cy + h), (cx - r * 0.25, cy)], fill=c)
    d.polygon([(cx + r, cy - h), (cx + r, cy + h), (cx + r * 0.25, cy)], fill=c)


def _logic(d, cx, cy, r, c):
    """Square-wave trace: the analyzer's captured window."""
    lw = _lw(r)
    hi, lo = cy - r * 0.5, cy + r * 0.5
    xs = [cx - r, cx - r * 0.45, cx + r * 0.1, cx + r * 0.55, cx + r]
    d.line([(xs[0], lo), (xs[1], lo), (xs[1], hi), (xs[2], hi),
            (xs[2], lo), (xs[3], lo), (xs[3], hi), (xs[4], hi)],
           fill=c, width=lw, joint="curve")


def _kismet(d, cx, cy, r, c):
    """Sweep scope with a contact."""
    lw = _lw(r)
    _circle(d, cx, cy, r * 0.95, c, lw)
    d.line((cx, cy, *_pt(cx, cy, r * 0.95, 315)), fill=c, width=lw)
    _dot(d, *_pt(cx, cy, r * 0.5, 140), r * 0.16, c)
    _dot(d, cx, cy, r * 0.12, c)


def _nodered(d, cx, cy, r, c):
    """Two stages on a flow wire."""
    lw = _lw(r)
    s = r * 0.3
    ax, ay = cx - r * 0.62, cy - r * 0.55
    bx, by = cx + r * 0.62, cy + r * 0.55
    d.line((ax, ay, ax, cy), fill=c, width=lw)
    d.line((ax, cy, bx, cy), fill=c, width=lw)
    d.line((bx, cy, bx, by), fill=c, width=lw)
    d.rectangle((ax - s, ay - s, ax + s, ay + s), fill=c)
    d.rectangle((bx - s, by - s, bx + s, by + s), fill=c)


def _ais(d, cx, cy, r, c):
    """Vessel silhouette under a masthead beacon."""
    lw = _lw(r)
    hy = cy + r * 0.25
    d.polygon([(cx - r, hy), (cx + r, hy),
               (cx + r * 0.5, cy + r * 0.8), (cx - r * 0.5, cy + r * 0.8)],
              fill=c)
    d.line((cx, hy, cx, cy - r * 0.45), fill=c, width=lw)
    _circle(d, cx, cy - r * 0.62, r * 0.24, c, lw)


def _signalk(d, cx, cy, r, c):
    """Gauge: dial arc + needle (live engine/nav vitals)."""
    lw = _lw(r)
    gy = cy + r * 0.35
    d.arc((cx - r, gy - r, cx + r, gy + r), 180, 360, fill=c, width=lw)
    d.line((cx - r, gy, cx + r, gy), fill=c, width=lw)
    d.line((cx, gy, *_pt(cx, gy, r * 0.8, 300)), fill=c, width=lw)
    _dot(d, cx, gy, r * 0.16, c)


def _pomodoro(d, cx, cy, r, c):
    """Chronometer: dial with the elapsed sector filled."""
    lw = _lw(r)
    _circle(d, cx, cy, r * 0.95, c, lw)
    s = r * 0.72
    d.pieslice((cx - s, cy - s, cx + s, cy + s), 270, 0, fill=c)


def _health(d, cx, cy, r, c):
    """Medical cross in a ring."""
    lw = _lw(r)
    _circle(d, cx, cy, r * 0.98, c, lw)
    a, b = r * 0.55, r * 0.2
    d.rectangle((cx - b, cy - a, cx + b, cy + a), fill=c)
    d.rectangle((cx - a, cy - b, cx + a, cy + b), fill=c)


def _files(d, cx, cy, r, c):
    """Offload: arrow descending into an open tray (USB stick exchange)."""
    lw = _lw(r)
    x0, x1 = cx - r * 0.75, cx + r * 0.75
    yt, yb = cy + r * 0.2, cy + r * 0.85
    for seg in ((x0, yt, x0, yb), (x0, yb, x1, yb), (x1, yb, x1, yt)):
        d.line(seg, fill=c, width=lw)
    d.line((cx, cy - r * 0.9, cx, cy + r * 0.1), fill=c, width=lw)
    aw = r * 0.32
    d.polygon((cx - aw, cy + 0.05 * r, cx + aw, cy + 0.05 * r,
               cx, cy + 0.5 * r), fill=c)


def _lightdock(d, cx, cy, r, c):
    """Dock: the big slab and the small slab joined by a live umbilical."""
    lw = _lw(r)
    # Prime (left, larger) and Light (right, smaller) silhouettes
    d.rectangle((cx - r, cy - r * 0.7, cx - r * 0.35, cy + r * 0.7),
                outline=c, width=lw)
    d.rectangle((cx + r * 0.45, cy - r * 0.45, cx + r, cy + r * 0.45),
                outline=c, width=lw)
    d.line((cx - r * 0.35, cy, cx + r * 0.45, cy), fill=c, width=lw)
    _dot(d, cx + r * 0.05, cy, r * 0.14, c)     # the pulse on the wire


def _microkvm(d, cx, cy, r, c):
    """Off-grid command plane: radio mast with a command chevron beside it."""
    lw = _lw(r)
    # mast + ground
    d.line((cx - r * 0.45, cy + r, cx - r * 0.45, cy - r * 0.55), fill=c, width=lw)
    _dot(d, cx - r * 0.45, cy - r * 0.62, r * 0.14, c)
    # two radiating arcs off the mast tip
    for k in (0.45, 0.8):
        d.arc((cx - r * 0.45 - r * k, cy - r * 0.62 - r * k,
               cx - r * 0.45 + r * k, cy - r * 0.62 + r * k),
              start=-45, end=45, fill=c, width=lw)
    # command chevron + return dash (one frame in, one reply back)
    d.line((cx + r * 0.15, cy + r * 0.05, cx + r * 0.5, cy + r * 0.4), fill=c, width=lw)
    d.line((cx + r * 0.5, cy + r * 0.4, cx + r * 0.15, cy + r * 0.75), fill=c, width=lw)
    d.line((cx + r * 0.6, cy + r * 0.75, cx + r * 0.95, cy + r * 0.75), fill=c, width=lw)


def _gps(d, cx, cy, r, c):
    """Sky plot in miniature: horizon ring, elevation ring, crosshair and
    a couple of satellite dots — the across-the-room fix indicator."""
    lw = _lw(r)
    _circle(d, cx, cy, r * 0.95, c, lw)
    _circle(d, cx, cy, r * 0.5, c, max(1, lw - 1))
    d.line((cx - r * 0.95, cy, cx + r * 0.95, cy), fill=c, width=1)
    d.line((cx, cy - r * 0.95, cx, cy + r * 0.95), fill=c, width=1)
    _dot(d, *_pt(cx, cy, r * 0.72, 305), r * 0.13, c)
    _dot(d, *_pt(cx, cy, r * 0.30, 160), r * 0.13, c)
    _dot(d, *_pt(cx, cy, r * 0.78, 60), r * 0.13, c)


def _settings(d, cx, cy, r, c):
    """Maintenance: ring with radial adjustment ticks."""
    lw = _lw(r)
    _circle(d, cx, cy, r * 0.62, c, lw)
    for a in range(0, 360, 45):
        d.line((*_pt(cx, cy, r * 0.62, a), *_pt(cx, cy, r, a)), fill=c, width=lw)


_GLYPHS = {
    "lan": _lan,
    "wifi": _wifi,
    "sdr": _sdr,
    "wifisniff": _wifisniff,
    "can": _can,
    "n2k": _n2k,
    "specter": _specter,
    "tables": _tables,
    "i2c": _i2c,
    "serial": _serial,
    "logic": _logic,
    "files": _files,
    "lightdock": _lightdock,
    "kismet": _kismet,
    "nodered": _nodered,
    "ais": _ais,
    "signalk": _signalk,
    "pomodoro": _pomodoro,
    "health": _health,
    "microkvm": _microkvm,
    "gps": _gps,
    "settings": _settings,
}


def draw(d, key, cx, cy, r, color):
    """Draw glyph `key` centered at (cx, cy) in `color`; unknown keys fall
    back to the launcher's original filled dot."""
    fn = _GLYPHS.get(key)
    if fn:
        fn(d, cx, cy, r, color)
    else:
        _dot(d, cx, cy, r * 0.75, color)
