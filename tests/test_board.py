"""Read-only Board tab.

Breaks named in each test. Nothing here writes a backlog or touches the live FirstMate home.
The earlier add/hold candidate is preserved outside this tree, not exercised here.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from firstpane.app import App
from firstpane.board import fm_board_path, summary_facts, unavailable
from firstpane.config import Config
from tests.support import Clock, CmdResult, FakeTerminal, FakeTransport, pane, strip_sgr, write_meta

FM_BOARD = fm_board_path(os.environ)
READS = {"fm-tasks-axi.sh": {"show"}, "fm-captain-hold.sh": {"open"}}
ALWAYS_WRITERS = ("fm-home-summary-refresh.sh", "fm-spawn.sh")
MUTATIONS = ("add", "hold", "unhold", "start", "done", "answer", "answers", "reconcile")


def plain(frame):
    return strip_sgr(frame)


class ReaderTests(unittest.TestCase):
    def test_missing_home_is_unavailable_without_a_guess(self):
        # Break: an unset home falls through to ~/.firstmate.
        self.assertIn("not configured", unavailable(None, "/tool", {}))
        self.assertIn("not configured", unavailable("/home", None, {}))

    def test_omitted_shape_is_the_summary_list(self):
        # Break: omitted.queued is assumed and a capped snapshot looks empty.
        facts = summary_facts({
            "valid": False,
            "reason": "missing_backlog",
            "omitted": [{"surface": "queued", "count": 3}, {"surface": "landed", "count": 1}],
            "generated_epoch": 1_000,
            "generated": "2026-10-26T00:00:00Z",
        }, 1_400)
        self.assertEqual(facts["omitted"], {"queued": 3, "landed": 1})
        self.assertIn("queued 3", " ".join(facts["lines"]))
        self.assertTrue(any("missing_backlog" in line for line in facts["lines"]))


class BoardFixture(unittest.TestCase):
    """A lab FirstMate home, a fake fm-board list and a fake Herdr; no test methods."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "fm-home"
        self.tool = self.root / "tool"
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        for path in (self.home / "data", self.home / "state", self.tool / "bin", self.bin, self.state):
            path.mkdir(parents=True)
        (self.bin / "fm").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "fm").chmod(0o755)
        (self.bin / "fm-board").write_text("#!/bin/sh\nexit 0\n")
        (self.tool / "bin" / "fm-tasks-axi.sh").write_text("#!/bin/sh\necho should-not-run\nexit 9\n")
        (self.tool / "bin" / "fm-home-summary-refresh.sh").write_text("#!/bin/sh\necho should-not-run\nexit 9\n")
        self.tasks = [{
            "id": "fp-keys-ab12",
            "project": "firstpane",
            "title": "Keyboard shortcuts",
            "state": "todo",
            "agent": None,
            "kind": "ship",
            "source": "firstmate",
            "since": None,
            "running": None,
            "captain_review": False,
            "updated_at": "2026-10-26T00:00:00Z",
        }, {
            "id": "fp-work-cd34",
            "project": "firstpane",
            "title": "Herdr sidebar",
            "state": "in_progress",
            "agent": "pi",
            "kind": "ship",
            "source": "firstmate",
            "since": None,
            "running": "editing",
            "captain_review": False,
            "updated_at": "2026-10-26T00:00:00Z",
        }, {
            "id": "fp-done-ef56",
            "project": "firstpane",
            "title": "Landed row",
            "state": "done",
            "agent": None,
            "kind": "ship",
            "source": "firstmate",
            "since": None,
            "running": None,
            "captain_review": False,
            "updated_at": "2026-10-26T00:00:00Z",
        }]
        (self.home / "state" / "home-summary.json").write_text(json.dumps({
            "schema": "fm-secondmate-home-summary.v1",
            "valid": True,
            "reason": None,
            "omitted": [],
            "generated": "2026-10-26T00:00:00Z",
            "generated_epoch": 1_700_000_000,
        }))
        (self.home / "data" / "board").mkdir()
        (self.home / "data" / "board" / "state.json").write_text(json.dumps({
            "schema": "fm-board.v1",
            "tasks": {"fp-keys-ab12": {"hold_reason": "until later: parked"}},
        }))
        self.transport = FakeTransport()
        self.transport.handlers.append((lambda argv: argv[-2:] == ["list", "--json"], lambda call: CmdResult(0, json.dumps(self.tasks), "")))
        self.env = {"PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(self.root), "LANG": "C.UTF-8"}
        self.clock = Clock()
        self.config = Config(firstmate_home=str(self.home), firstmate_root=str(self.tool), socket_path=str(self.root / "herdr.sock"))

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, rows=32, cols=120):
        app = App(self.config, self.transport, self.state, self.env, self.clock, FakeTerminal(), rows=rows, cols=cols, wall=lambda: 1_700_000_100)
        app.refresh(manual=True)
        return app

    def open_board(self, app):
        app.handle_key("3")
        return plain(app.render())

    def writers(self):
        found = []
        for call in self.transport.calls:
            argv = call["argv"]
            base = os.path.basename(argv[0])
            sub = argv[1] if len(argv) > 1 else ""
            if base in ALWAYS_WRITERS:
                found.append(argv)
            elif base in READS and sub not in READS[base]:
                found.append(argv)
            elif sub in MUTATIONS:
                found.append(argv)
        return found


class BoardAppTests(BoardFixture):

    def test_keys_1_to_4_stay_and_5_opens_board_categories(self):
        # Break: Board renumbers the existing tabs or invents a Working card from an in-flight row.
        app = self.app()
        app.handle_key("1")
        self.assertEqual(app.tab, "agents")
        app.handle_key("2")
        self.assertEqual(app.tab, "review")
        app.handle_key("5")
        self.assertEqual(app.tab, "browser")
        (app.handle_key("2"), None if app.review_source == "pr" else app.handle_key("\\"))
        self.assertEqual((app.tab, app.review_source), ("review", "pr"))
        frame = self.open_board(app)
        self.assertEqual(app.tab, "board")
        self.assertIn("TODO", frame)
        self.assertIn("Working", frame)
        self.assertIn("Waiting Review", frame)
        self.assertNotIn("Blocked", frame)
        self.assertIn("Done (1)", frame)
        self.assertIn("Keyboard shortcuts", frame)
        self.assertIn("Herdr sidebar", frame)
        self.assertNotIn("Landed row", frame)
        self.assertNotIn("New task", frame)
        calls = [call["argv"] for call in self.transport.calls if "fm-board" in call["argv"][0]]
        self.assertEqual(calls[0][1:], ["list", "--json"])
        self.assertEqual(calls[0] and self.transport.calls[-1]["env"]["FM_HOME"], str(self.home))
        self.assertEqual(self.writers(), [])

    def test_narrow_moves_columns_and_failed_refresh_keeps_cards(self):
        # Break: a narrow pane requires horizontal scrolling, or a failed refresh clears the board.
        app = self.app(cols=80)
        frame = self.open_board(app)
        self.assertIn("TODO", frame)
        app.handle_key("right")
        self.assertEqual(app.board_column, "in_progress")
        self.assertIn("Herdr sidebar", plain(app.render()))
        self.transport.handlers.insert(0, (lambda argv: argv[-2:] == ["list", "--json"], lambda call: CmdResult(1, "", "fm-board: reconcile skipped (boom)")))
        app.handle_key("r")
        failed = plain(app.render())
        self.assertIn("Herdr sidebar", failed)
        self.assertIn("list failed", failed)
        self.assertIn("reconcile skipped", failed)
        self.assertTrue(any(task["id"] == "fp-keys-ab12" for task in app.board_tasks))
        self.assertEqual(self.writers(), [])

    def test_unconfigured_home_invents_no_cards(self):
        # Break: a missing home still draws sample tasks or calls fm-board.
        self.config = Config(firstmate_home=None, firstmate_root=str(self.tool))
        app = self.app()
        frame = self.open_board(app)
        self.assertIn("FirstMate home is not configured", frame)
        self.assertNotIn("Keyboard", frame)
        self.assertFalse(any("fm-board" in " ".join(call["argv"]) for call in self.transport.calls))

    def test_omitted_summary_is_not_an_empty_board(self):
        # Break: omitted rows are treated as proof the column is empty.
        summary = json.loads((self.home / "state" / "home-summary.json").read_text())
        summary["omitted"] = [{"surface": "queued", "count": 4}]
        (self.home / "state" / "home-summary.json").write_text(json.dumps(summary))
        app = self.app()
        frame = self.open_board(app)
        self.assertIn("omitted from this snapshot: queued 4", frame)
        self.assertIn("Keyboard shortcuts", frame)

    def test_keys_and_clicks_cannot_reach_a_task_writer(self):
        # Break: n, the old add control, or a refresh still files or holds a task.
        app = self.app()
        frame = self.open_board(app)
        self.assertNotIn("+ New task", frame)
        self.assertNotIn("n New", frame)
        before = list(self.transport.calls)
        app.handle_key("n")
        app.handle_key("a")
        app.handle_key("enter")
        app.handle_key("y")
        app.handle_key("esc")
        self.assertEqual(app.screen, "tree")
        self.assertFalse((self.state / "board-intake.json").exists())
        row = next(index + 1 for index, line in enumerate(plain(app.render()).splitlines()) if "All tasks" in line)
        col = plain(app.render()).splitlines()[row - 1].index("All tasks") + 1
        app.handle_bytes(f"\x1b[<0;{col};{row}M\x1b[<0;{col};{row}m".encode())
        app.handle_key("r")
        self.assertEqual(self.writers(), [])
        added = self.transport.calls[len(before):]
        self.assertEqual([call["argv"][1:] for call in added if call["argv"][-2:] == ["list", "--json"]], [["list", "--json"]])
        self.assertTrue(all(
            call["argv"][-2:] == ["list", "--json"] or (len(call["argv"]) > 1 and call["argv"][1] == "show")
            for call in added
        ))
        self.assertEqual(self.transport.calls[-1]["argv"][1:], ["list", "--json"])

    def test_selected_task_message_survives_tabs_and_does_not_write_a_task(self):
        # Break: the unsent task context is dropped, or messaging files work.
        app = self.app()
        self.open_board(app)
        app.board_selected = "fp-keys-ab12"
        app.handle_key("tab")
        app.handle_bytes(b"look at this")
        app.handle_key("esc")
        app.handle_key("1")
        app.handle_key("3")
        app.board_selected = "fp-keys-ab12"
        self.assertIn("look at this", plain(app.render()))
        self.assertEqual(self.writers(), [])

    def test_wide_done_expand_shows_cards_and_active_hides_them(self):
        # Break: expanding Done on a wide board draws no cards, or Active tasks still counts them.
        app = self.app()
        self.open_board(app)
        self.tasks.append({
            "id": "fp-odd-0001", "project": "firstpane", "title": "Odd state",
            "state": "mystery", "agent": None, "kind": "ship", "source": "firstmate",
        })
        app.handle_key("r")
        for _ in range(3):
            app.handle_key("right")
        self.assertEqual(app.board_column, "done")
        self.assertIsNone(app.board_selected)
        app.handle_key("enter")
        frame = plain(app.render())
        self.assertIn("Landed row", frame)
        self.assertIn("fp-odd-0001", frame)
        self.assertIn("not mapped", frame)
        app.handle_key("f")
        hidden = plain(app.render())
        self.assertIn("Done (0)", hidden)
        self.assertNotIn("Landed row", hidden)
        self.assertEqual(self.writers(), [])

    def test_a_timed_out_list_keeps_cards_and_the_next_refresh_still_lists(self):
        # Break: one slow list permanently switches the board to board-data.js.
        app = self.app()
        self.open_board(app)
        (self.home / "data" / "board" / "board-data.js").write_text(
            'window.BOARD_DATA = {"tasks": [{"id": "stale-only", "title": "Stale", "state": "todo"}]};'
        )
        self.transport.handlers.insert(0, (
            lambda argv: argv[-2:] == ["list", "--json"],
            lambda call: CmdResult(0, "", "", timed_out=True),
        ))
        app.handle_key("r")
        self.assertTrue(any(task["id"] == "fp-keys-ab12" for task in app.board_tasks))
        self.assertNotEqual(app.board_source, "board-data.js")
        self.assertIn("timed out", plain(app.render()))
        self.transport.handlers.pop(0)
        app.handle_key("r")
        self.assertEqual(self.transport.calls[-1]["argv"][1:], ["list", "--json"])
        self.assertTrue(any(task["id"] == "fp-keys-ab12" for task in app.board_tasks))
        self.assertEqual(self.writers(), [])

    def test_go_to_agent_refuses_without_a_live_pane_and_focuses_when_one_matches(self):
        # Break: Go to agent focuses a stale pane, or a live meta is ignored.
        app = self.app()
        self.open_board(app)
        app.board_selected = "fp-keys-ab12"
        app.handle_key("g")
        self.assertEqual(app.status, "no agent assigned")
        self.assertFalse(any(call["method"] == "pane.focus" for call in self.transport.socket_calls))
        row = pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1")
        write_meta(
            self.home, "fp-keys-ab12", project="/work/firstpane", herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="w1:p1",
            endpoint_task_id="fp-keys-ab12",
        )
        app.session = "lab"
        app.projects = [{"agents": [{
            "task_id": "fp-keys-ab12", "pane_id": "w1:p1", "terminal_id": "term_a",
            "workspace_id": "w1", "tab_id": "w1:t1", "focusable": True, "kind": "agent",
        }]}]
        self.transport.pane_get["w1:p1"] = dict(row)
        app.handle_key("g")
        self.assertTrue(any(call["method"] == "pane.focus" for call in self.transport.socket_calls))
        self.assertEqual(self.writers(), [])


class ClassifierReadTests(unittest.TestCase):
    def test_real_list_classifies_a_fixture_without_writing_a_task(self):
        # Break: the pane invents columns, or reading the board files a backlog row.
        if not FM_BOARD:
            self.skipTest("fm-board is not next to the fm launcher on PATH")
        tmp = tempfile.TemporaryDirectory(prefix="fp-board-read-")
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "fm-home"
        (home / "state").mkdir(parents=True)
        (home / "data").mkdir()
        stub = Path(tmp.name) / "tool"
        (stub / "bin").mkdir(parents=True)
        (stub / "bin" / "fm-crew-state.sh").write_text("#!/bin/sh\necho 'state: working · source: stub · detail editing'\n")
        (stub / "bin" / "fm-crew-state.sh").chmod(0o755)
        ident = "fp-read-only"
        (home / "state" / "home-summary.json").write_text(json.dumps({
            "schema": "fm-secondmate-home-summary.v1",
            "valid": True,
            "omitted": [],
            "queued": [{
                "id": ident, "title": "Visible later", "repo": "firstpane", "kind": "ship",
                "hold_kind": "parked", "hold_reason": "not started", "captain_actionable": False,
                "unresolved_blocker_ids": [],
            }],
            "active_children": [], "endpoints": [], "landed": [], "holds": [], "decisions_open": [],
        }))
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(Path(tmp.name) / "user"),
            "LANG": "C.UTF-8",
            "TMPDIR": tmp.name,
            "FM_HOME": str(home),
            "FIRSTMATE_ROOT": str(stub),
        }
        listed = subprocess.run([str(FM_BOARD), "list", "--json"], env=env, cwd=tmp.name, text=True, capture_output=True, timeout=20)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        match = next(row for row in json.loads(listed.stdout) if row["id"] == ident)
        self.assertEqual(match["state"], "todo")
        self.assertFalse((home / "data" / "backlog.md").exists())
        self.assertFalse((home / "state" / f"{ident}.meta").exists())
