"""Runtime settings, persisted to JSON so they survive reboots and can be
hand-edited over SSH if the touch panel is ever mis-calibrated.

Everything the Settings screen exposes lives here. Adding a new tunable is a
one-line change to DEFAULTS; the Settings screen renders whatever it finds.
"""

import copy
import json
import os

import microkvm

from . import cantick

CONFIG_PATH = os.environ.get("KILODASH_CONFIG", "/opt/kilodash/config.json")

# Each key maps to a spec the Settings screen knows how to render:
#   type: "bool" | "int" | "choice"
#   for int:    min, max, step, unit
#   for choice: options list
DEFAULTS = {
    # --- touch mapping (defaults target the rotate=90 panel orientation) ---
    "touch_swap_xy": {"value": True,  "type": "bool",
                      "label": "Touch swap X/Y", "group": "Touch"},
    "touch_invert_x": {"value": False, "type": "bool",
                       "label": "Touch invert X", "group": "Touch"},
    "touch_invert_y": {"value": False, "type": "bool",
                       "label": "Touch invert Y", "group": "Touch"},
    "touch_calibrated": {"value": False, "type": "hidden",
                         "label": "", "group": "Touch"},

    # --- display ---
    "flip_180": {"value": False, "type": "bool",
                 "label": "Flip vertically", "group": "Display"},
    "dim_enabled": {"value": True, "type": "bool",
                    "label": "Screen dimming", "group": "Display"},
    "dim_timeout_sec": {"value": 600, "type": "int", "min": 30, "max": 1800,
                        "step": 30, "unit": "s",
                        "label": "Dim after", "group": "Display"},
    "dim_level": {"value": 8, "type": "int", "min": 0, "max": 60, "step": 4,
                  "unit": "%", "label": "Dim brightness", "group": "Display"},

    # --- behaviour ---
    "poll_sec": {"value": 3, "type": "int", "min": 1, "max": 30, "step": 1,
                 "unit": "s", "label": "Status refresh", "group": "System"},
    "theme": {"value": "green", "type": "choice",
              "options": ["green", "amber", "light"],
              "label": "Theme", "group": "System"},
    "show_clock": {"value": True, "type": "bool",
                   "label": "Show clock", "group": "System"},
    "show_fps": {"value": False, "type": "bool",
                 "label": "FPS meter (perf tuning)", "group": "System"},

    # --- app panels (not shown in Settings; edited from their own screens) ---
    "ais_own_mmsi": {"value": "", "type": "hidden",
                     "label": "Own AIS MMSI", "group": "System"},
    "signalk_token": {"value": "", "type": "hidden",
                      "label": "Signal K access token", "group": "System"},
    # CanTick WiFi-CAN bridge (see PROTOCOL.md; defaults are the contract
    # values). Managed from the CAN screen; cantick.block() merges these
    # defaults under a partially-saved block after upgrades.
    "cantick": {"value": dict(cantick.CONFIG_DEFAULTS), "type": "hidden",
                "label": "CanTick bridge", "group": "System"},
    # Light Dock (DOCK-PROTOCOL.md): the one costly sync step is optional.
    # Also toggleable from the Light Dock screen itself.
    "lightdock_pull_logs": {"value": True, "type": "bool",
                            "label": "Light Dock: auto-pull logs",
                            "group": "System"},
    # Micro KVM off-grid command plane (MICROKVM-PROTOCOL.md). Hand-edited
    # over SSH for now; not secret-bearing — the command-channel PSK lives in
    # the Meshtastic node (docs/LORAMESH.md), never here.
    "microkvm": {"value": dict(microkvm.CONFIG_DEFAULTS), "type": "hidden",
                 "label": "Micro KVM command plane", "group": "System"},
}


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.specs = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in self.specs:
                    self.specs[k]["value"] = v
        except (OSError, ValueError):
            pass

    def save(self):
        data = {k: s["value"] for k, s in self.specs.items()}
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def __getitem__(self, key):
        return self.specs[key]["value"]

    def set(self, key, value):
        self.specs[key]["value"] = value
        self.save()

    def groups(self):
        """Ordered {group_name: [(key, spec), ...]} for the Settings screen."""
        out = {}
        for k, s in self.specs.items():
            out.setdefault(s.get("group", "Other"), []).append((k, s))
        return out
