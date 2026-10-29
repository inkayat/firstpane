"""Board tab on a real pty. Herdr is a fake socket; fm-board is the installed classifier.

The FirstMate home is a temporary directory. The live home is not addressed.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from firstpane.board import fm_board_path
from tests.test_pty import FakeHerdr, Pane

FM_BOARD = fm_board_path(os.environ)


class BoardPtyTests(unittest.TestCase):
    def setUp(self):
        if not FM_BOARD:
            self.skipTest("fm-board is not next to the fm launcher on PATH")
        self.tmp = Path(tempfile.mkdtemp(prefix="fp-board-pty-", dir="/tmp"))
        self.home = self.tmp / "fm-home"
        (self.home / "data" / "board").mkdir(parents=True)
        (self.home / "state").mkdir()
        (self.home / "data" / "projects.md").write_text("- firstpane - pane (added 2026-10-26)\n")
        summary = {
            "schema": "fm-secondmate-home-summary.v1",
            "valid": True,
            "reason": None,
            "omitted": [{"surface": "queued", "count": 2}],
            "generated": "2026-10-26T00:00:00Z",
            "generated_epoch": int(__import__("time").time()) - 30,
            "queued": [{
                "id": "fp-pty-todo",
                "title": "Keyboard shortcuts",
                "repo": "firstpane",
                "kind": "ship",
                "hold_kind": "parked",
                "hold_reason": "filed for later visibility; not authorized to start",
                "captain_actionable": False,
                "unresolved_blocker_ids": [],
            }],
            "active_children": [],
            "endpoints": [{
                "id": "fp-pty-work",
                "state": "working",
                "repo": "firstpane",
                "name": "Herdr sidebar",
            }],
            "landed": [{"id": "fp-pty-done", "title": "Landed row", "repo": "firstpane", "kind": "ship"}],
            "holds": [],
            "decisions_open": [],
        }
        (self.home / "state" / "home-summary.json").write_text(json.dumps(summary))
        stub = self.tmp / "tool"
        (stub / "bin").mkdir(parents=True)
        crew = stub / "bin" / "fm-crew-state.sh"
        crew.write_text("#!/bin/sh\necho 'state: working · source: stub · detail editing files'\n")
        crew.chmod(0o755)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fm = bindir / "fm"
        fm.write_text("#!/bin/sh\nexit 0\n")
        fm.chmod(0o755)
        os.symlink(FM_BOARD, bindir / "fm-board")
        sock = self.tmp / "sessions" / "lab"
        sock.mkdir(parents=True)
        self.herdr = FakeHerdr(str(sock / "herdr.sock"), {"snapshot": {
            "version": "0.9.0", "protocol": 22, "workspaces": [], "tabs": [], "panes": [], "layouts": [], "agents": [],
        }, "panes": {}})
        config = self.tmp / "config"
        config.mkdir()
        (config / "config.json").write_text(json.dumps({
            "firstmate_home": str(self.home),
            "firstmate_root": str(stub),
        }))
        self.env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.tmp / "user"),
            "LANG": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "TMPDIR": str(self.tmp),
            "HERDR_SOCKET_PATH": str(sock / "herdr.sock"),
            "HERDR_PLUGIN_CONFIG_DIR": str(config),
            "HERDR_PLUGIN_STATE_DIR": str(self.tmp / "plugin-state"),
            "HERDR_PANE_ID": "w9:p9",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.panes = []

    def tearDown(self):
        for item in getattr(self, "panes", []):
            item.close()
        if getattr(self, "herdr", None) is not None:
            self.herdr.close()
        evidence = self.tmp / "evidence"
        evidence.mkdir(exist_ok=True)
        if self.panes:
            (evidence / "pty-final.txt").write_text(self.panes[-1].screen.text())

    def open(self, rows, cols):
        item = Pane(self.env, rows, cols)
        self.panes.append(item)
        return item

    def test_wide_and_narrow_board_accepts_keys_and_a_click(self):
        # Break: the real pane cannot show the classifier's columns, or keys 1-4 move.
        term = self.open(32, 120)
        term.wait_for(lambda screen: "Agents" in screen.text())
        term.send(b"3")
        term.wait_for(lambda screen: "Keyboard shortcuts" in screen.text() and "Done (1)" in screen.text())
        text = term.screen.text()
        self.assertIn("Working", text)
        self.assertIn("Waiting Review", text)
        self.assertNotIn("Blocked", text)
        self.assertIn("Done (1)", text)
        self.assertIn("omitted from this snapshot: queued 2", text)
        self.assertIn("Herdr sidebar", text)
        self.assertNotIn("Landed row", text)
        self.assertNotIn("New task", text)
        self.assertNotIn("n New", text)
        evidence = self.tmp / "evidence"
        evidence.mkdir(exist_ok=True)
        (evidence / "pty-wide.txt").write_text(term.screen.text())
        row = next(index + 1 for index, line in enumerate(term.screen.lines()) if "Herdr sidebar" in line)
        column = term.screen.lines()[row - 1].index("Herdr") + 1
        term.send(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())
        term.wait_for(lambda screen: "fp-pty-work \u00b7 Herdr sidebar" in screen.text())
        term.resize(28, 72)
        term.wait_for(lambda screen: "Working 1" in screen.text() and "Herdr sidebar" in screen.text())
        term.send(b"\x1b[D")
        term.wait_for(lambda screen: "TODO 1" in screen.text() and "Keyboard shortcuts" in screen.text())
        (evidence / "pty-narrow.txt").write_text(term.screen.text())
        narrow = term.screen.text()
        self.assertNotIn("Waiting Review 0", narrow)
        term.send(b"n")
        term.send(b"1")
        term.wait_for(lambda screen: "Agents" in screen.lines()[0] and "Board" in screen.lines()[0])
        self.assertFalse((self.home / "data" / "backlog.md").exists())
        self.assertIn("Agents", term.screen.lines()[0])
        shutil.copy(self.tmp / "plugin-state" / "ui-state.json", evidence / "ui-state.json")

    def test_keys_message_draft_and_resize_in_a_real_pane(self):
        # Break: a resize or a lane change in a real pane drops the selected task, the unsent
        # message, or the fixed message block.
        term = self.open(40, 130)
        term.wait_for(lambda screen: "Agents" in screen.text())
        term.send(b"3")
        term.wait_for(lambda screen: "fp-pty-todo \u00b7 Keyboard shortcuts" in screen.text())
        term.send(b"\x1b[C")
        term.wait_for(lambda screen: "fp-pty-work \u00b7 Herdr sidebar" in screen.text())
        term.send(b"\t")
        term.send(b"check this")
        term.wait_for(lambda screen: "check this" in screen.text() and "Herdr sidebar \u00d7" in screen.text())
        for rows, cols in ((24, 80), (30, 110), (40, 130)):
            term.resize(rows, cols)
            term.wait_for(lambda screen: "check this" in screen.text())
            self.assertEqual(len(term.screen.lines()), rows)
        term.send(b"\x1b")
        term.send(b"\x1b[D")
        term.wait_for(lambda screen: "fp-pty-todo \u00b7 Keyboard shortcuts" in screen.text())
        self.assertNotIn("check this", term.screen.text())
        term.send(b"\x1b[C")
        term.wait_for(lambda screen: "check this" in screen.text() and "fp-pty-work" in screen.text())
        self.assertNotIn("New task", term.screen.text())
        self.assertFalse((self.home / "data" / "backlog.md").exists())

    def test_no_color_board_has_no_escape_styling_and_keeps_the_selection_bar(self):
        # Break: NO_COLOR still paints, or the selected card is distinguishable by color alone.
        self.env["NO_COLOR"] = "1"
        term = self.open(32, 120)
        term.wait_for(lambda screen: "Agents" in screen.text())
        mark = len(term.raw)
        term.send(b"3")
        term.wait_for(lambda screen: "Keyboard shortcuts" in screen.text())
        painted = term.raw[mark:]
        self.assertNotRegex(painted, r"\x1b\[[0-9;]*(?:38|48);")
        row = next(line for line in term.screen.lines() if "Keyboard shortcuts" in line)
        self.assertIn("\u258c", row)
