"""Synthetic discovery overlay. The native snapshot is not mutated."""

import copy
import tempfile
import unittest

from firstpane.app import App
from firstpane.config import Config
from tests.lab_overlay import mismatch_snapshot, overlay_snapshot, verify_shell
from tests.support import Clock, FakeTerminal, FakeTransport


SHELL = {
    "pane_id": "w1:p2",
    "terminal_id": "term_agent",
    "workspace_id": "w1",
    "tab_id": "w1:t2",
    "cwd": "/tmp/lab-cwd",
}


class OverlayTests(unittest.TestCase):
    def test_overlay_binds_only_the_verified_shell_and_leaves_the_raw_snapshot(self):
        # Break: the overlay edits the native snapshot, or the synthetic row points at another pane.
        raw = {"panes": [dict(SHELL, label="synthetic-lab")], "agents": []}
        saved = copy.deepcopy(raw)
        facts = verify_shell(SHELL, "/tmp/lab-cwd")
        snap = overlay_snapshot(raw, facts)
        self.assertEqual(raw, saved)
        self.assertEqual(snap["agents"][0]["pane_id"], "w1:p2")
        self.assertEqual(snap["agents"][0]["terminal_id"], "term_agent")
        self.assertEqual(snap["panes"][0]["name"], "synthetic-lab-agent")
        with self.assertRaises(ValueError):
            verify_shell(SHELL, "/tmp/other")

    def test_stale_pane_id_is_not_a_live_binding(self):
        # Break: a meta whose pane id is not the owned shell is treated as live.
        state = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        app = App(Config(socket_path="/tmp/sessions/fm-lab-x/herdr.sock"), FakeTransport(), state, {}, Clock(), FakeTerminal())
        shell = dict(SHELL, name="synthetic-lab-agent")
        panes = {"w1:p2": shell}
        agents = [{"pane_id": "w1:p2", "name": "synthetic-lab-agent"}]
        meta = {
            "herdr_pane_id": "stale-not-owned",
            "herdr_session": "fm-lab-x",
            "herdr_workspace_id": "w1",
            "herdr_tab_id": "w1:t2",
        }
        self.assertFalse(app._binding_is_live(meta, panes, agents, {"stale-not-owned": 1}, "w1:p1"))
        self.assertEqual(mismatch_snapshot({"panes": [dict(SHELL)], "agents": []}, SHELL)["agents"][0]["pane_id"], "stale-not-owned")
