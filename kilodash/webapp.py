"""Launch and supervise third-party web apps (Kismet, Node-RED, AIS-catcher…).

kilodash's Phase-4 role: a *launch terminal* for bigger packages that ship their
own browser UI. A WebApp wraps one such app. It can start it (a background
process or a systemd unit) and — the important part — gives **positive
confirmation** the app is actually serving, by probing its TCP port. That turns
a hopeful "launched" into a real "✓ web UI confirmed", and yields the URL:port
you'd type on your phone or laptop to reach it.

Design notes:
- The readiness probe connects to 127.0.0.1 (the app binds locally on the Pi);
  the *displayed* URL uses the Pi's LAN IP so another device can reach it.
- launch() first probes: if the app is already serving (autostarted at boot, or
  left running from before), we adopt it as UP instead of starting a duplicate.
- Nothing here is app-specific. Per-app controls/feedback live in the screen.
"""

import base64
import json
import shutil
import socket
import subprocess
import time
import urllib.request

from . import system

STOPPED, STARTING, UP, ERROR = "stopped", "starting", "up", "error"


def probe(host, port, timeout=0.4):
    """True if something is accepting TCP connections on host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- tiny HTTP --
# App screens read their app's own REST/JSON endpoints for live feedback. Kept
# to stdlib urllib (no requests dependency) and always best-effort: any failure
# returns None so a screen degrades to "open the web UI" rather than crashing.
def _auth_header(req, auth, token=None):
    if token:                                 # Bearer (e.g. Signal K JWT / device token)
        req.add_header("Authorization", "Bearer " + token)
    elif auth:                                # HTTP basic (user, pass)
        tok = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)


def http_get(url, timeout=1.5, auth=None, token=None):
    try:
        req = urllib.request.Request(url)
        _auth_header(req, auth, token)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:                         # noqa: BLE001
        return None


def http_json(url, timeout=1.5, auth=None, token=None):
    raw = http_get(url, timeout=timeout, auth=auth, token=token)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def http_post(url, data=b"", timeout=2.0, auth=None, content_type=None):
    try:
        req = urllib.request.Request(url, data=data or b"", method="POST")
        _auth_header(req, auth)
        if content_type:
            req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", None) or r.getcode()
    except Exception:                         # noqa: BLE001
        return None


def lan_ip():
    """Best address to reach this box from another device (for the shown URL)."""
    dev = system.primary_iface()
    ifaces = system.get_interfaces()
    if dev:
        for it in ifaces:
            if it["name"] == dev and it["ip"] not in ("", "--"):
                return it["ip"]
    for it in ifaces:
        if it["ip"] not in ("", "--"):
            return it["ip"]
    return "127.0.0.1"


class WebApp:
    """One supervised web app. Poll .state each tick; drive with launch()/stop()."""

    def __init__(self, name, port, *, service=None, start_cmd=None,
                 host="127.0.0.1", url_path="/", ready_timeout=30, log_path=None):
        self.name = name
        self.port = int(port)
        self.service = service          # systemd unit name, e.g. "nodered.service"
        self.start_cmd = start_cmd      # argv list launched via Popen (alt to service)
        self.host = host
        self.url_path = url_path
        self.ready_timeout = ready_timeout
        self.log_path = log_path
        self.state = STOPPED
        self.message = "Not started"
        self.proc = None
        self._log = None
        self._t0 = 0.0
        self._last_probe = -1e9

    # ---------------------------------------------------------- introspection
    def installed(self):
        """Is the backing binary / unit present on this box?"""
        if self.start_cmd:
            return shutil.which(self.start_cmd[0]) is not None
        if self.service:
            out = system.run(["systemctl", "list-unit-files", self.service])
            return self.service in out
        return False

    def probe(self):
        return probe(self.host, self.port)

    def url(self):
        return f"http://{lan_ip()}:{self.port}{self.url_path}"

    @property
    def running(self):
        return self.state in (STARTING, UP)

    # --------------------------------------------------------------- lifecycle
    def launch(self, start_cmd=None):
        """Start the app (or adopt it if already serving). start_cmd overrides
        the constructor default — used when a screen computes argv at runtime
        (e.g. Kismet needs the chosen monitor interface)."""
        if self.probe():                      # already up: adopt, don't duplicate
            self.state = UP
            self.message = "Already running"
            return
        cmd = start_cmd or self.start_cmd
        self.state = STARTING
        self.message = "Launching…"
        self._t0 = time.monotonic()
        try:
            if cmd:
                out = subprocess.DEVNULL
                if self.log_path:
                    self._log = open(self.log_path, "ab")
                    out = self._log
                self.proc = subprocess.Popen(cmd, stdout=out, stderr=out,
                                             stdin=subprocess.DEVNULL)
            elif self.service:
                if not self.installed():
                    # "unit not installed" is NOT a fault — there is no
                    # journal to point at. Stay STOPPED so the screen can
                    # show the remedy (run the installer) instead of a
                    # generic FAULT. Distinct state from installed-but-failed.
                    self.state = STOPPED
                    self.message = f"{self.service} not installed"
                    return
                r = subprocess.run(["systemctl", "start", self.service],
                                   capture_output=True, text=True, timeout=20)
                if r.returncode != 0:
                    self.state = ERROR
                    self.message = (r.stderr.strip().splitlines() or
                                    ["failed to start service"])[-1][:44]
            else:
                self.state = ERROR
                self.message = "No launch method configured"
        except FileNotFoundError:
            self.state = ERROR
            self.message = f"{self.name} not installed"
        except Exception as e:                # noqa: BLE001
            self.state = ERROR
            self.message = str(e)[:44]

    def poll(self):
        """Advance the state machine. Call once per tick — safe at any tick
        rate: the TCP probe is internally throttled. Returns True only when
        state/message changed (i.e. the banner actually needs a repaint)."""
        before = (self.state, self.message)
        now = time.monotonic()
        if self.state == STARTING:
            if now - self._last_probe >= 0.5:
                self._last_probe = now
                if self.probe():
                    self.state = UP
                    self.message = "Web UI confirmed"
                elif self.proc is not None and self.proc.poll() is not None:
                    self.state = ERROR
                    self.message = f"Process exited (code {self.proc.returncode})"
                elif now - self._t0 > self.ready_timeout:
                    self.state = ERROR
                    self.message = "Timed out waiting for web UI"
        elif self.state == UP:
            if now - self._last_probe >= 2.0:
                self._last_probe = now
                if not self.probe():
                    self.state = ERROR
                    self.message = "Web UI stopped responding"
        return (self.state, self.message) != before

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:                 # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:             # noqa: BLE001
                    pass
            self.proc = None
        elif self.service:
            subprocess.run(["systemctl", "stop", self.service],
                           capture_output=True, timeout=20)
        if self._log:
            try:
                self._log.close()
            except Exception:                 # noqa: BLE001
                pass
            self._log = None
        self.state = STOPPED
        self.message = "Stopped"
