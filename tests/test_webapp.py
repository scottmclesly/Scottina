"""Unit tests for kilodash/webapp.py state machine, focused on the
service-launch paths that back the Tables tile.

Regression guard for the Tables-tile FAULT bug: a service whose systemd unit
is not installed must read as STOPPED with a "not installed" message (the
screen then names the installer remedy), NOT as ERROR pointing at a journal
that cannot exist. Installed-but-failed stays ERROR. Stdlib + mock only; no
real systemctl or sockets.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kilodash import webapp  # noqa: E402


def _app():
    # a bogus port nothing listens on; probe() will return False
    return webapp.WebApp("Tables", 65533, service="kilodash-tables.service")


class TestLaunchStates(unittest.TestCase):
    def test_unit_not_installed_reads_as_stopped_not_error(self):
        w = _app()
        with mock.patch.object(w, "installed", return_value=False), \
                mock.patch.object(w, "probe", return_value=False), \
                mock.patch("kilodash.webapp.subprocess.run") as run:
            w.launch()
        self.assertEqual(w.state, webapp.STOPPED)          # not ERROR/FAULT
        self.assertIn("not installed", w.message)
        run.assert_not_called()          # never attempts to start a ghost unit

    def test_installed_but_start_fails_is_error(self):
        w = _app()
        fake = types.SimpleNamespace(returncode=1, stderr="Job failed to run")
        with mock.patch.object(w, "installed", return_value=True), \
                mock.patch.object(w, "probe", return_value=False), \
                mock.patch("kilodash.webapp.subprocess.run", return_value=fake):
            w.launch()
        self.assertEqual(w.state, webapp.ERROR)            # a real fault
        self.assertIn("failed", w.message.lower())

    def test_already_serving_is_adopted(self):
        w = _app()
        with mock.patch.object(w, "probe", return_value=True), \
                mock.patch("kilodash.webapp.subprocess.run") as run:
            w.launch()
        self.assertEqual(w.state, webapp.UP)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
