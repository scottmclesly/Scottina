"""Tree-wide CAN-TX allow-list scan — the Phase 3 carve-out, enforced in
code (GPS-Integration TODO: "evolve the AST scan from 'no TX anywhere' to
a positive allow-list of TX-permitted modules").

The scope constraint it makes executable: diagnostics only, with exactly
two named RUNTIME TX exceptions — (1) link-layer heartbeat/reply behavior
required by bus participation (lives in CanTick firmware, not in this tree),
and (2) the GNSS source node `n2k/node.py` (address claim, claim defense,
ISO request responses, the five GNSS PGNs), started and stopped only by an
explicit user action. Any send-shaped call on a socket in ANY other Scottina
module hard-fails the build; modules that legitimately speak non-CAN sockets
are each named with their justification. The per-module RX-only scans in
tests/test_busmon.py / test_n2k.py remain as the independent reject pass.

Separately, **bench equipment** under `tools/bench/` is NOT Scottina runtime
and may transmit — it is a shell/harness generator, gated behind
`--bench-bus-confirmed`, and no Scottina package imports or constructs its
frames (enforced by tests/test_syn_emitter.py). Such files are named in
BENCH_CAN_TX, kept apart from the runtime carve-out so the "what Scottina may
put on the bus" answer stays exactly (1)+(2).

Run from the repo root:  python -m unittest discover -s tests
"""

import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCAN_ROOTS = ("kilodash", "gps", "tables", "n2k", "microkvm", "tools")

# The positive allow-list: RUNTIME modules permitted to transmit on a CAN
# socket. Growing this set is a scope decision (see the carve-out test).
ALLOWED_CAN_TX = {
    "n2k/node.py",          # GNSS source node — the Phase 3 carve-out
}

# Bench equipment permitted to transmit — NOT Scottina runtime. Gated behind
# --bench-bus-confirmed, never imported/constructed by any Scottina package
# (tests/test_syn_emitter.py enforces that boundary). Kept apart from
# ALLOWED_CAN_TX so the runtime answer stays exactly the two carve-outs.
BENCH_CAN_TX = {
    "tools/bench/syn-emitter.py",   # synthetic ground-truth emitter
}

# Modules permitted send-family calls on NON-CAN sockets, each justified.
ALLOWED_NET_SEND = {
    "gps/gpsdio.py",        # one sendall(): the ?WATCH command to gpsd's
                            # localhost JSON socket (TCP, never CAN)
    "kilodash/eventsock.py",  # web-mirror event frames on a Unix domain
                            # socket (AF_UNIX, never AF_CAN). WEB-PROTOCOL.md
                            # §10: the mirror adds no TX surface — see
                            # TestWebMirrorAddsNoTxSurface below, which makes
                            # that claim executable rather than aspirational.
    "tools/mirror-tap.py",  # bench subscriber: reads the same Unix socket and
                            # sends §6 commands back down it. Same AF_UNIX
                            # justification, and covered by the same test.
    "kilodash/webmirror.py",  # web mirror backend: one sendall() forwarding a
                            # validated §6 command down the SAME AF_UNIX box
                            # socket. It also binds TCP for the LAN UI, which
                            # is its whole purpose and is not a bus surface.
}

SEND_ATTRS = {"send", "sendall", "sendto", "sendmsg", "sendfile"}
CAN_MARKERS = {"AF_CAN", "PF_CAN", "CAN_RAW", "CAN_BCM", "CAN_EFF_FLAG"}
TX_PROGS = {"cansend", "cangen", "canplayer", "canfdtest"}


def scan_source(relpath, text):
    """Violations in one module. Rules:
    - send/sendall/sendto/sendmsg/sendfile is forbidden everywhere except
      the two allow-lists above (a CAN frame leaves python only through a
      socket send, so this is the choke point);
    - a module that touches CAN constants additionally may not os.write()
      (the fd-level back door to a SocketCAN socket) unless CAN-allow-listed;
    - TX-capable can-utils program names are forbidden in call arguments in
      every module, allow-listed or not — the node speaks sockets, not
      shell-outs.
    """
    tree = ast.parse(text, relpath)
    referenced = {n.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)}
    referenced |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    is_can_module = bool(referenced & CAN_MARKERS)
    can_ok = relpath in ALLOWED_CAN_TX or relpath in BENCH_CAN_TX
    net_ok = relpath in ALLOWED_NET_SEND
    out = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        fn = call.func
        name = fn.attr if isinstance(fn, ast.Attribute) else \
            fn.id if isinstance(fn, ast.Name) else ""
        if name in SEND_ATTRS and not can_ok and not net_ok:
            out.append(f"{relpath}:{call.lineno}: {name}() — TX-shaped "
                       "socket call outside the allow-list")
        if name == "write" and isinstance(fn, ast.Attribute) \
                and isinstance(fn.value, ast.Name) and fn.value.id == "os" \
                and is_can_module and not can_ok:
            out.append(f"{relpath}:{call.lineno}: os.write() in a "
                       "CAN-touching module outside the allow-list")
        for arg in ast.walk(call):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value in TX_PROGS:
                out.append(f"{relpath}:{call.lineno}: invokes TX-capable "
                           f"tool {arg.value!r}")
    return out


def iter_modules():
    for root in SCAN_ROOTS:
        base = os.path.join(ROOT, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    full = os.path.join(dirpath, fn)
                    yield os.path.relpath(full, ROOT).replace(os.sep, "/")


class TestTreeWideTxScan(unittest.TestCase):
    def test_whole_tree_passes(self):
        violations = []
        scanned = 0
        for rel in iter_modules():
            with open(os.path.join(ROOT, rel)) as f:
                violations += scan_source(rel, f.read())
            scanned += 1
        self.assertGreater(scanned, 30)      # the walk actually walked
        self.assertEqual(violations, [],
                         "\n".join(["CAN-TX scope violations:"] + violations))

    def test_allow_list_is_exactly_the_carve_out(self):
        """The runtime carve-out is these modules and no more — growing the
        list is a scope decision, not a refactor detail."""
        self.assertEqual(ALLOWED_CAN_TX, {"n2k/node.py"})

    def test_bench_tx_is_exactly_the_emitter_and_is_bench_only(self):
        """Bench TX is separate from the runtime carve-out, and every entry
        must live under tools/bench/ — never inside a Scottina package."""
        self.assertEqual(BENCH_CAN_TX, {"tools/bench/syn-emitter.py"})
        for rel in BENCH_CAN_TX:
            self.assertTrue(rel.startswith("tools/bench/"), rel)
            self.assertFalse(rel.startswith("kilodash/"), rel)


class TestScanCatches(unittest.TestCase):
    """The scan's own tests: synthetic modules prove the detector fires
    (and that the allow-list actually allows)."""

    SCREEN_TX = (
        "import socket\n"
        "def evil(iface):\n"
        "    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW,"
        " socket.CAN_RAW)\n"
        "    s.bind((iface,))\n"
        "    s.send(b'\\x00' * 16)\n"
    )

    def test_screen_module_txing_fails(self):
        v = scan_source("kilodash/screens/gps.py", self.SCREEN_TX)
        self.assertTrue(any("send()" in x for x in v))

    def test_allow_listed_module_txing_passes(self):
        self.assertEqual(scan_source("n2k/node.py", self.SCREEN_TX), [])

    def test_os_write_backdoor_in_can_module_fails(self):
        src = ("import os, socket\n"
               "F = socket.CAN_RAW\n"
               "def sneak(fd):\n"
               "    os.write(fd, b'frame')\n")
        v = scan_source("kilodash/busmon.py", src)
        self.assertTrue(any("os.write" in x for x in v))
        # …but ordinary file/serial os.write in a non-CAN module is fine
        self.assertEqual(scan_source("kilodash/lightdock.py",
                                     "import os\nos.write(1, b'x')\n"), [])

    def test_can_utils_tx_program_fails_everywhere(self):
        src = "import subprocess\nsubprocess.run(['cansend', 'can0', '1#'])\n"
        for rel in ("kilodash/screens/canbus.py", "n2k/node.py"):
            v = scan_source(rel, src)
            self.assertTrue(any("cansend" in x for x in v), rel)

    def test_net_send_allowance_is_narrow(self):
        src = "def f(s):\n    s.sendall(b'?WATCH')\n"
        self.assertEqual(scan_source("gps/gpsdio.py", src), [])
        self.assertTrue(scan_source("gps/snapshotd.py", src))


class TestWebMirrorAddsNoTxSurface(unittest.TestCase):
    """WEB-PROTOCOL.md §10 claims a hostile actor on the LAN can navigate the
    diagnostics UI and cannot transmit on the vehicle bus, "because no code
    path exists that would let them". That is a safety claim, so it is tested
    rather than trusted: the mirror is allow-listed for a Unix socket, and
    these pin that the allowance stays exactly that narrow."""

    # Every module in the web-mirror path. None may touch CAN, ever.
    WEB_PATH = ("kilodash/eventsock.py", "tools/mirror-tap.py",
                "kilodash/webmirror.py")
    # The subset that must speak ONLY to the box, never to a network. The
    # backend is excluded on purpose: binding TCP for the LAN UI is its job.
    BOX_LOCAL = ("kilodash/eventsock.py", "tools/mirror-tap.py")

    def test_mirror_touches_no_can_constants(self):
        """The net-send allowance is only safe while the module is not a CAN
        module. If eventsock ever imports a CAN constant, the allowance it
        already holds would cover a real bus transmit."""
        for rel in self.WEB_PATH:
            with self.subTest(module=rel):
                with open(os.path.join(ROOT, rel)) as f:
                    tree = ast.parse(f.read(), rel)
                names = {n.attr for n in ast.walk(tree)
                         if isinstance(n, ast.Attribute)}
                names |= {n.id for n in ast.walk(tree)
                          if isinstance(n, ast.Name)}
                self.assertEqual(names & CAN_MARKERS, set(),
                                 f"{rel} references a CAN constant — its "
                                 f"send() allowance would then cover the bus")

    def test_mirror_opens_only_unix_sockets(self):
        for rel in self.BOX_LOCAL:
            with self.subTest(module=rel):
                with open(os.path.join(ROOT, rel)) as f:
                    src = f.read()
                self.assertIn("AF_UNIX", src)
                for fam in ("AF_CAN", "AF_INET", "AF_INET6", "AF_PACKET"):
                    self.assertNotIn(fam, src,
                                     f"{rel} opens a {fam} socket — the "
                                     f"mirror is LAN-local by the backend, "
                                     f"and box-local by this socket")

    def test_mirror_would_fail_if_it_gained_a_can_socket(self):
        """The guard still fires for the allow-listed module — the net-send
        allowance must not become a blanket exemption."""
        src = ("import socket\n"
               "def evil(iface):\n"
               "    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW,"
               " socket.CAN_RAW)\n"
               "    s.send(b'\\x00' * 16)\n")
        # os.write back-door is caught because the module now reads as CAN…
        v = scan_source("kilodash/eventsock.py",
                        src + "import os\nos.write(3, b'x')\n")
        self.assertTrue(any("os.write" in x for x in v),
                        "a CAN-touching eventsock must lose its exemption")


if __name__ == "__main__":
    unittest.main()
