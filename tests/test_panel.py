"""The workspace panel: tabs, selected agent, Changes, Git confirms, inline messages and sending.

Herdr is faked at the socket boundary, including the FirstMate primary's pi message box
(FakePi); Git runs for real against fixture repositories; the FirstMate inbox (PR notes,
announce) is a fake handler. Nothing reaches a real repository, inbox, FirstMate or GitHub.
"""

import re
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path

from firstpane.agents_view import humanize
from firstpane.app import App
from firstpane.config import Config
from firstpane.drafts import DraftStore
from firstpane.transport import LiveTransport
from tests.gitfixture import GitFixture
from tests.support import Clock, CmdResult, FakePi, FakeTerminal, FakeTransport, agent_for, note_json, pane, selected_line, snapshot, workspace, write_meta

ADD = "[+] Add comment"


def plain(frame):
    return re.sub(r"\x1b\[[0-9;]*m", "", frame)


class GitTransport(FakeTransport):
    """Fake Herdr socket and inbox; real git."""

    def __init__(self):
        super().__init__()
        self.live = LiveTransport(None)
        self.inbox = []
        self.inbox_results = []

    def run(self, argv, stdin=None, env=None, timeout=None, cwd=None, detach=False):
        if argv and str(argv[0]).endswith("fm-inbox.sh"):
            self.inbox.append({"argv": list(argv), "stdin": stdin, "env": dict(env or {})})
            if self.inbox_results:
                return self.inbox_results.pop(0)
            return CmdResult(0, note_json("created"), "")
        return self.live.run(argv, stdin=stdin, env=env, timeout=timeout, cwd=cwd, detach=detach)


class PanelFixture(unittest.TestCase):
    def setUp(self):
        self.fx = GitFixture()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "fm-home"
        self.tool = self.root / "fm-tool"
        (self.tool / "bin").mkdir(parents=True)
        (self.tool / "bin" / "fm-inbox.sh").write_text("#!/bin/sh\nexit 0\n")
        self.state = self.root / "state"
        self.transport = GitTransport()
        self.clock = Clock()
        self.wt_a = self.fx.worktree("wt-a", "fm/alpha-task")
        self.wt_b = self.fx.worktree("wt-b", "fm/beta-task")
        self.config = Config(
            firstmate_home=str(self.home),
            firstmate_root=str(self.tool),
            socket_path="/Users/x/.config/herdr/sessions/lab/herdr.sock",
            project_allowlist=(str(self.fx.base),),
        )
        self.panes = {
            "alpha-task": pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1", agent_status="idle"),
            "beta-task": pane(pane_id="w2:p1", terminal_id="term_b", workspace_id="w2", tab_id="w2:t1", agent_status="working"),
        }
        for task, wt, row in (("alpha-task", self.wt_a, self.panes["alpha-task"]), ("beta-task", self.wt_b, self.panes["beta-task"])):
            write_meta(
                self.home,
                task,
                project=str(self.fx.primary),
                herdr_session="lab",
                herdr_workspace_id=row["workspace_id"],
                herdr_tab_id=row["tab_id"],
                herdr_pane_id=row["pane_id"],
                endpoint_task_id=task,
                worktree=str(wt),
            )
            self.transport.pane_get[row["pane_id"]] = dict(row)
        self.main = FakePi(self.transport, self.home)
        self.publish()

    def tearDown(self):
        self.fx.cleanup()
        self.tmp.cleanup()

    def publish(self):
        rows = list(self.panes.values())
        self.transport.snapshot = snapshot(
            [workspace("w1", "alpha"), workspace("w2", "beta", number=2)], rows + [self.main.pane], [agent_for(row) for row in rows]
        )

    def pasted(self):
        """What each transfer put in FirstMate's message box, without the paste markers."""
        return [text.removeprefix("\x1b[200~").removesuffix("\x1b[201~") for text in self.main.received]

    def app(self, rows=30, cols=100, env=None):
        app = App(
            config=self.config,
            transport=self.transport,
            state_dir=self.state,
            env=env if env is not None else dict(self.fx.env),
            clock=self.clock,
            terminal=FakeTerminal(),
            rows=rows,
            cols=cols,
        )
        app.refresh(manual=True)
        return app

    def select(self, app, task):
        for _ in range(30):
            app.handle_key("k")
        for _ in range(30):
            frame = app.render()
            if humanize(task) in selected_line(frame) or task in selected_line(frame):
                app.handle_key("enter")
                return
            app.handle_key("j")
        raise AssertionError(frame)

    def keys(self, app, *keys):
        for key in keys:
            app.handle_key(key)

    def type(self, app, text):
        app.handle_bytes(text.encode())

    def lines(self, app):
        return plain(app.render()).split("\n")

    def row_of(self, app, needle):
        """1-based screen row of the first line containing `needle`."""
        lines = self.lines(app)
        for index, line in enumerate(lines):
            if needle in line:
                return index + 1
        raise AssertionError(f"{needle!r} not on screen:\n" + "\n".join(lines))

    def click(self, app, row, col=12, shift=False):
        button = 4 if shift else 0
        app.handle_bytes(f"\x1b[<{button};{col};{row}M\x1b[<{button};{col};{row}m".encode())

    def double_click(self, app, row):
        self.click(app, row)
        self.click(app, row)

    def drag(self, app, start, end, col=12):
        app.handle_bytes(f"\x1b[<0;{col};{start}M\x1b[<32;{col};{end}M\x1b[<0;{col};{end}m".encode())

    def drag_through(self, app, rows, col=12):
        events = f"\x1b[<0;{col};{rows[0]}M"
        events += "".join(f"\x1b[<32;{col};{row}M" for row in rows[1:])
        events += f"\x1b[<0;{col};{rows[-1]}m"
        app.handle_bytes(events.encode())

    def changed_readme(self):
        """README.md in alpha's worktree: line 2 rewritten, line 4 added."""
        self.fx.write("README.md", "one\nTWO\nthree\nfour\n", root=self.wt_a)

    def click_text(self, app, needle, shift=0):
        """Click the first cell of `needle` (plus `shift` cells) where it is drawn."""
        row = self.row_of(app, needle)
        self.click(app, row, col=self.lines(app)[row - 1].index(needle) + 1 + shift)

    def pick_file(self, app, path):
        """Click the file picker (the first ▾ control), then `path` in its list."""
        self.click_text(app, "\u25be", shift=-2)
        rows = [index + 1 for index, line in enumerate(self.lines(app)) if f"  {path}" in line and "\u25be" not in line]
        self.assertTrue(rows, "\n".join(self.lines(app)))
        self.click(app, rows[0], col=8)

    def open_changes(self, app, task="alpha-task"):
        app.handle_key("1")
        self.select(app, task)
        app.handle_key("2")

    def expected_context(self):
        head = self.fx.head(self.wt_a)
        return (
            "Firstpane message\n"
            f"project: primary  {self.fx.primary}\n"
            "agent: alpha-task  claude  pane w1:p1  terminal term_a\n"
            f"workspace: {self.wt_a}  (fm)\n"
            f"branch: fm/alpha-task (no upstream) @ {head[:12]}\n"
        )


class LayoutTests(PanelFixture):
    def test_every_tab_uses_the_agents_dark_surface(self):
        # Break: a tab is drawn on the terminal default or a brighter fill, selection is color
        # only, or NO_COLOR still emits escapes or changes the words.
        surface = "48;2;16;22;35"
        app = self.app(rows=24, cols=80)
        self.select(app, "alpha-task")
        plain_app = self.app(rows=24, cols=80, env={**self.fx.env, "NO_COLOR": "1"})
        self.select(plain_app, "alpha-task")
        for key in ("1", "2", "3", "4"):
            app.handle_key(key)
            plain_app.handle_key(key)
            frame = app.render()
            bare = plain_app.render()
            self.assertIn(surface, frame, key)
            self.assertNotIn("48;2;40;48;60", frame, key)
            self.assertNotIn("48;5;", frame, key)
            self.assertRegex(frame.splitlines()[0], r"\x1b\[1;4")
            self.assertNotIn("\x1b", bare, key)
            self.assertIn("Review" if app.cols >= 60 else "Rv", plain(frame).splitlines()[0])
            self.assertIn("Review" if app.cols >= 60 else "Rv", bare.splitlines()[0])
        app.rows, app.cols = 16, 32
        narrow = app.render()
        self.assertIn(surface, narrow)
        self.assertTrue(all(len(plain(line)) <= 32 for line in narrow.splitlines()))

    def test_agents_keeps_its_field_and_other_tabs_do_not_hide_one(self):
        # Break: Agents loses its field, Browser grows a composer, or typing on Changes or
        # Browser without a selection is stored as a message.
        for rows, cols in ((30, 100), (41, 68), (12, 40), (6, 30)):
            app = self.app(rows=rows, cols=cols)
            self.select(app, "alpha-task")
            lines = self.lines(app)
            self.assertEqual(len(lines), rows, (rows, cols))
            self.assertTrue(all(len(line) <= cols for line in lines), (rows, cols))
            self.assertTrue(any("\u203a" in line for line in lines[-6:]), (rows, cols))
            app.handle_key("5")
            lines = self.lines(app)
            self.assertFalse([line for line in lines if "\u203a" in line or "Write to FirstMate" in line], (rows, cols))
            app.handle_key("m")
            self.type(app, "zq")
            self.assertNotIn("zq", plain(app.render()), (rows, cols))
            app.handle_key("2")
            self.type(app, "zq")
            self.assertNotIn("zq", plain(app.render()), (rows, cols))
            if rows >= 16:
                self.assertIn("Write to FirstMate", plain(app.render()))
            app.handle_key("1")
            self.assertTrue(any(line.startswith("\u258c") for line in plain(app.render()).splitlines()), (rows, cols))
        self.assertEqual(list((self.state / "drafts").glob("open-*.json")), [])
        self.assertEqual(self.transport.inbox, [])

    def test_changes_selection_and_inline_editor_stay_inside_normal_and_narrow_frames(self):
        # Break: the old/new gutters, the range icon or the inline editor push lines past the
        # pane edge, or a resize scrolls the selected lines or the editor off screen.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.keys(app, "shift-down", "enter")
        self.type(app, "sığar mı")
        for rows, cols in ((30, 100), (41, 68), (14, 40), (10, 30)):
            app.rows, app.cols = rows, cols
            lines = self.lines(app)
            self.assertEqual(len(lines), rows, (rows, cols))
            self.assertTrue(all(len(line) <= cols for line in lines), (rows, cols))
            self.assertTrue(any("sığar mı" in line for line in lines), (rows, cols))
            self.assertTrue(any("- two" in line for line in lines), (rows, cols))

    def test_review_tab_label_has_no_change_count_while_the_agent_row_keeps_it(self):
        # Break: the top-level Review label shows the selected agent's changed-file count
        # (a second copy of the number the agent's own row already shows), or removing it
        # changes the tab order, the narrow labels or where a click on Review lands.
        self.fx.write("one.txt", "1\n", root=self.wt_a)
        self.fx.write("two.txt", "2\n", root=self.wt_a)
        app = self.app()
        self.select(app, "alpha-task")
        lines = self.lines(app)
        self.assertEqual(lines[0].split(), ["Agents", "?", "Review", "Board", "Terminal", "Browser", "Settings"])
        self.assertNotIn("Review 2", lines[0])
        alpha = next(line for line in lines if "Alpha task" in line)
        self.assertRegex(alpha, r"Alpha task\s+Idle \u00b7 2 files")
        app.rows, app.cols = 30, 50
        self.assertEqual(self.lines(app)[0].split(), ["Agents", "?", "Rv", "Bd", "Tm", "Br", "Set"])
        app.rows, app.cols = 30, 100
        column = lines[0].index("Review") + 1
        app.handle_bytes(f"\x1b[<0;{column};1M\x1b[<0;{column};1m".encode())
        self.assertEqual(app.tab, "review")
        self.assertEqual(self.lines(app)[0].split(), ["Agents", "?", "Review", "Board", "Terminal", "Browser", "Settings"])

    def test_header_shows_the_selected_agent_branch_status_and_changed_files(self):
        # Break: the header does not follow the selection, reads the primary checkout
        # instead of the agent worktree, or hides the changed-file count; the Agents rows show the
        # same count.
        self.fx.write("one.txt", "1\n", root=self.wt_a)
        self.fx.write("two.txt", "2\n", root=self.wt_a)
        app = self.app()
        self.select(app, "alpha-task")
        lines = plain(app.render()).splitlines()
        alpha = lines.index(next(line for line in lines if "Alpha task" in line))
        self.assertRegex(lines[alpha], r"Alpha task\s+Idle \u00b7 2 files\s+\u203a")
        beta = lines.index(next(line for line in lines if "Beta task" in line))
        self.assertRegex(lines[beta], r"Beta task\s+Working \u00b7 clean\s+\u203a")
        app.handle_key("2")
        lines = plain(app.render()).splitlines()
        self.assertIn("/ alpha-task", lines[1])
        self.assertIn("fm/alpha-task (no upstream)", lines[1])
        self.assertIn("idle \u00b7 2 files", lines[2])
        self.assertIn("wt-a (fm)", lines[2])
        app.handle_key("1")
        self.select(app, "beta-task")
        app.handle_key("2")
        lines = plain(app.render()).splitlines()
        self.assertIn("/ beta-task", lines[1])
        self.assertIn("working \u00b7 0 files", lines[2])

    def test_enter_selects_in_the_panel_and_g_focuses(self):
        # Break: Enter on an agent leaves Firstpane (pane.focus) instead of selecting it.
        app = self.app()
        self.select(app, "alpha-task")
        self.assertNotIn("pane.focus", [call["method"] for call in self.transport.socket_calls])
        app.handle_key("g")
        focus = [call for call in self.transport.socket_calls if call["method"] == "pane.focus"]
        self.assertEqual([call["params"] for call in focus], [{"pane_id": "w1:p1"}])

    def test_closed_agent_is_shown_closed_and_never_retargeted(self):
        # Break: when the selected pane closes (or is reused), the header silently shows
        # another agent or a reused pane's occupant.
        app = self.app()
        self.select(app, "alpha-task")
        self.panes["alpha-task"] = pane(pane_id="w1:p1", terminal_id="term_new", workspace_id="w1", tab_id="w1:t1", agent_status="idle")
        self.publish()
        self.clock.t += 5
        app.refresh(manual=True)
        app.handle_key("2")
        lines = plain(app.render()).splitlines()
        self.assertIn("/ alpha-task", lines[1])
        self.assertIn("agent closed", lines[2])

    def test_search_filters_agents(self):
        # Break: the agent filter keeps non-matching agents or does not clear on Esc.
        app = self.app()
        app.handle_key("/")
        self.type(app, "bet")
        frame = plain(app.render())
        self.assertIn("Beta task", frame)
        self.assertNotIn("Alpha task", frame)
        app.handle_key("esc")
        self.assertIn("Alpha task", plain(app.render()))


class CursorSelectionTests(PanelFixture):
    """The agent under the Agents tab's cursor is the selected agent: no Enter is needed."""

    def changes(self, app):
        """The Changes tab's frame, then back to the Agents tab."""
        app.handle_key("2")
        frame = plain(app.render())
        app.handle_key("1")
        return frame

    def test_moving_onto_an_agent_shows_its_changes_and_details_without_enter(self):
        # Break: Changes and its header stay on the agent chosen earlier (or on none) until
        # Enter is pressed, or moving between rows touches a pane or FirstMate's message box.
        self.fx.write("alpha-only.txt", "a\n", root=self.wt_a)
        self.fx.write("beta-only.txt", "b\n", root=self.wt_b)
        app = self.app()
        calls = len(self.transport.socket_calls)
        app.handle_key("j")
        frame = self.changes(app)
        lines = frame.splitlines()
        self.assertIn("/ beta-task", lines[1])
        self.assertIn("fm/beta-task", lines[1])
        self.assertIn("working \u00b7 1 file", lines[2])
        self.assertIn("wt-b", lines[2])
        self.assertNotIn("wt-a", lines[2])
        self.assertIn("beta-only.txt", frame)
        self.assertNotIn("alpha-only.txt", frame)
        app.handle_key("k")
        frame = self.changes(app)
        lines = frame.splitlines()
        self.assertIn("/ alpha-task", lines[1])
        self.assertIn("wt-a (fm)", lines[2])
        self.assertIn("alpha-only.txt", frame)
        self.assertNotIn("beta-only.txt", frame)
        self.assertEqual(self.transport.socket_calls[calls:], [])
        self.assertEqual(self.main.received, [])
        self.assertEqual(self.transport.inbox, [])

    def test_a_late_read_for_the_agent_moved_away_from_never_shows_for_the_one_moved_to(self):
        # Break: alpha's diff or counts, read before the cursor moved on, land afterwards and
        # are shown as beta's; or beta's own diff never replaces them.
        self.fx.write("README.md", "one\nALPHA\nthree\n", root=self.wt_a)
        self.fx.write("README.md", "one\nBETA\nthree\n", root=self.wt_b)
        app = self.app()
        app.handle_key("enter")
        held = []

        def submit(fn):
            future = Future()
            held.append((future, fn))
            return future

        app.submit = submit
        self.assertIn("reading diff...", self.changes(app))
        app.handle_key("j")
        app.handle_key("2")
        frame = plain(app.render())
        self.assertIn("/ beta-task", frame.splitlines()[1])
        self.assertEqual(len(held), 4)  # a diff and a counts read for each agent, alpha's first
        for future, read in held[:2]:
            future.set_result(read())
        frame = plain(app.render())
        self.assertNotIn("ALPHA", frame)
        self.assertIn("reading diff...", frame)
        for future, read in held[2:]:
            future.set_result(read())
        frame = plain(app.render())
        self.assertIn("+ BETA", frame)
        self.assertNotIn("ALPHA", frame)

    def test_moving_through_agents_reads_no_workspace_before_it_is_due(self):
        # Break: every cursor move re-reads every listed agent's Git state, so holding an
        # arrow key starts a burst of git subprocesses.
        app = self.app()
        reads = []
        app.submit = lambda fn: reads.append(fn) or Future()
        for key in "jkjkj":
            app.handle_key(key)
            app.render()
        self.assertEqual(reads, [])
        self.assertEqual((app.target or {}).get("label"), "beta-task")


class ChangesTests(PanelFixture):
    def test_changes_shows_the_picked_file_with_counts_and_a_numbered_old_new_diff(self):
        # Break: the file picker or the per-file +/- counts are wrong, untracked files are not
        # counted, the place in the list is wrong, or the diff shows one number column so old
        # and new lines are confused.
        self.changed_readme()
        self.fx.write("new.txt", "a\nb\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        lines = self.lines(app)
        frame = "\n".join(lines)
        self.assertIn("README.md \u25be", lines[5])
        self.assertIn("1 / 2", lines[5])
        self.assertIn("+2 -1  unstaged", lines[6])
        colored = app.render()
        self.assertIn("48;2;28;62;42", colored)
        self.assertTrue("48;2;72;30;42" in colored or "48;2;88;36;48" in colored)
        for row in ("  1   1 \u2502   one", "  2   - \u258c - two", "  -   2 \u2502 + TWO", "  3   3 \u2502   three", "  -   4 \u2502 + four"):
            self.assertIn(row, frame)
        self.assertLess(self.row_of(app, "- two"), self.row_of(app, "+ TWO"))
        app.handle_key("tab")
        lines = self.lines(app)
        self.assertIn("All changes · 2 files  +4 -1", "\n".join(lines))
        file_rows = [line.rstrip() for line in lines if ("  README.md" in line or "  new.txt" in line) and "\u25be" not in line]
        self.assertTrue(file_rows[0].endswith("+2 -1") and file_rows[0].lstrip().startswith("\u2713"), file_rows)
        self.assertTrue(file_rows[1].endswith("+2") and "?  new.txt" in file_rows[1], file_rows)
        self.click(app, self.row_of(app, "?  new.txt"), col=8)
        frame = plain(app.render())
        self.assertIn("  -   1 \u258c + a", frame)
        self.assertIn("2 / 2", frame)
        self.assertNotIn("+ TWO", frame)
        app.handle_key("[")
        self.assertIn("+ TWO", plain(app.render()))
        app.handle_key("]")
        self.assertIn("2 / 2", plain(app.render()))

    def test_scope_menu_and_stage_button_act_on_the_file_shown(self):
        # Break: the scope does not filter the list, the Stage control stages another file
        # than the one shown (or skips the preview), or the view loses the file after it
        # moves from unstaged to staged.
        self.changed_readme()
        self.fx.write("new.txt", "n\n", root=self.wt_a)
        self.fx.write("staged.txt", "s\n", root=self.wt_a)
        self.fx.git(self.wt_a, "add", "staged.txt")
        app = self.app()
        self.open_changes(app)
        self.assertIn("1 / 3", self.lines(app)[5])
        self.click_text(app, "Uncommitted \u25be")
        self.click_text(app, "Staged")
        lines = self.lines(app)
        self.assertIn("staged.txt \u25be", lines[5])
        self.assertIn("1 / 1", lines[5])
        self.assertIn("Uncommitted \u00b7 staged \u25be", lines[5])
        self.click_text(app, "Unstage file")
        self.assertIn("git restore --staged -- staged.txt", plain(app.render()))
        app.handle_key("n")
        self.click_text(app, "Uncommitted \u00b7 staged \u25be")
        self.click_text(app, "All changes")
        self.pick_file(app, "new.txt")
        self.assertIn("new.txt \u25be", self.lines(app)[5])
        self.click_text(app, "Stage file")
        self.assertIn("git add -- new.txt", plain(app.render()))
        self.assertEqual(self.fx.git(self.wt_a, "diff", "--cached", "--name-only"), "staged.txt")
        app.handle_key("y")
        self.assertEqual(self.fx.git(self.wt_a, "diff", "--cached", "--name-only").split(), ["new.txt", "staged.txt"])
        self.clock.t += 20
        app.refresh(manual=True)
        lines = self.lines(app)
        self.assertIn("new.txt \u25be", lines[5])
        self.assertIn("staged", lines[6])
        self.assertIn("Unstage file", lines[6])

    def test_controls_stay_whole_and_clickable_in_narrow_panes(self):
        # Break: a narrow pane clips a control or a click lands on a neighbouring control.
        self.changed_readme()
        for cols in (100, 48, 30):
            app = self.app(rows=24, cols=cols)
            self.open_changes(app)
            for label, item in (("Git \u25be", "Fetch"), ("\u22ef", "Refresh")):
                self.click_text(app, label)
                self.assertIn(item, plain(app.render()), cols)
                app.handle_key("esc")
                self.assertNotIn(item, plain(app.render()), cols)
            self.assertTrue(all(len(line) <= cols for line in self.lines(app)), cols)

    def test_double_click_on_a_line_opens_an_inline_message_for_that_line_and_fills_firstmates_box(self):
        # Break: double-click does not open an editor at the line, the message loses the
        # file/side/range/HEAD/blob or the exact excerpt, the preview differs from what lands
        # in FirstMate's message box, the message is submitted or queued instead of left
        # there unsent, or a transferred message stays on screen as a draft.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        row = self.row_of(app, "+ TWO")
        self.double_click(app, row)
        self.assertIn("Write to FirstMate", plain(app.render()))
        self.assertIn("1 selection added", plain(app.render()))
        self.assertNotIn("FirstMate", self.lines(app)[row])
        self.type(app, "Bu satırı kontrol et")
        self.assertIn("Bu satırı kontrol et", plain(app.render()))
        self.assertEqual(self.main.received, [])
        app.handle_key("enter")
        preview = plain(app.render())
        self.assertIn("Put in FirstMate's message box (pane w5:p1, pi)", preview)
        head = self.fx.head(self.wt_a)
        blob = self.fx.git(self.wt_a, "hash-object", "README.md")
        expected = (
            self.expected_context()
            + "\n"
            "Bu satırı kontrol et\n"
            "\n"
            f"[1] diff README.md  unstaged  new lines 2-2  HEAD {head[:12]}  new blob {blob[:12]}\n"
            "source workspace  revision uncommitted\n"
            "```diff\n"
            "+TWO\n"
            "```\n"
        )
        self.assertIn(expected.rstrip("\n"), preview)
        app.handle_key("y")
        self.assertEqual(self.pasted(), [expected.rstrip("\n")])
        self.assertEqual(self.main.submitted, [])
        self.assertEqual(self.transport.inbox, [])
        frame = plain(app.render())
        self.assertIn("in FirstMate's message box (pane w5:p1): press Enter there to send", frame)
        self.assertNotIn("Bu satırı kontrol et", frame)

    def test_a_keyboard_range_has_one_add_comment_on_its_first_line_and_the_message_carries_every_line(self):
        # Break: every selected line gets the Add comment control, it is not on the first
        # selected line, clicking it does nothing, or the message carries only the clicked line.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.click(app, self.row_of(app, "- two"))
        self.type(app, "\x1b[1;2B\x1b[1;2B")
        lines = self.lines(app)
        offers = [line for line in lines if ADD in line]
        self.assertEqual(len(offers), 1, "\n".join(lines))
        self.assertIn("- two", offers[0])
        self.assertEqual(sum(1 for line in lines if "\u258c" in line), 3, "\n".join(lines))
        self.click_text(app, ADD, shift=1)
        self.type(app, "üç satır")
        app.handle_key("enter")
        app.handle_key("y")
        head = self.fx.head(self.wt_a)
        blob = self.fx.git(self.wt_a, "hash-object", "README.md")
        self.assertEqual(
            self.pasted(),
            [self.expected_context()
             + "\nüç satır\n\n"
             f"[1] diff README.md  unstaged  old lines 2-3  new lines 2-3  HEAD {head[:12]}  new blob {blob[:12]}\n"
             "source workspace  revision uncommitted\n"
             "```diff\n-two\n+TWO\n three\n```"],
        )

    def test_drag_and_shift_click_select_ranges_with_the_mouse(self):
        # Break: dragging over lines or Shift-clicking selects only one line, so a mouse
        # user cannot comment on several lines at once.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.drag(app, self.row_of(app, "  one"), self.row_of(app, "+ TWO"))
        offers = [line for line in self.lines(app) if ADD in line]
        self.assertEqual(len(offers), 1)
        self.assertIn("  one", offers[0])
        app.handle_key("enter")
        self.keys(app, "a", "enter")
        self.assertIn("```diff\n one\n-two\n+TWO\n```", plain(app.render()))
        self.keys(app, "n", "esc")
        self.click(app, self.row_of(app, "  three"))
        self.click(app, self.row_of(app, "+ four"), shift=True)
        app.handle_key("enter")
        self.keys(app, "b", "enter")
        self.assertIn("old lines 3-3  new lines 3-4", plain(app.render()))

    def test_a_drag_extends_the_selection_on_every_move_of_one_press(self):
        # Break: only the first drag event of a press moves the selection, so dragging down
        # several lines selects just two of them.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        rows = [self.row_of(app, needle) for needle in ("  one", "+ TWO", "  three", "+ four")]
        self.drag_through(app, rows)
        app.handle_key("enter")
        self.keys(app, "a", "enter")
        self.assertIn("old lines 1-3  new lines 1-4", plain(app.render()))

    def test_slow_git_reads_leave_keys_and_rendering_responsive_until_they_land(self):
        # Break: counts and the selected file's diff are read on the UI thread, so a slow
        # `git diff` freezes rendering and key handling until it finishes.
        self.changed_readme()
        app = self.app()
        self.select(app, "alpha-task")
        held = []

        def submit(fn):
            future = Future()
            held.append((future, fn))
            return future

        app.submit = submit
        diffs = []
        run = self.transport.run

        def spy(argv, **kwargs):
            if "diff" in argv:
                diffs.append(argv)
            return run(argv, **kwargs)

        self.transport.run = spy
        app.handle_key("2")
        for _ in range(3):
            frame = plain(app.render())
        self.assertEqual(diffs, [])
        self.assertIn("reading diff...", frame)
        self.assertIn("README.md", frame)
        app.handle_key("j")
        app.handle_key("k")
        self.assertEqual(diffs, [])
        self.assertEqual(len(held), 2)
        for future, fn in held:
            future.set_result(fn())
        frame = plain(app.render())
        self.assertNotIn("reading diff...", frame)
        self.assertIn("+ TWO", frame)
        self.assertRegex(frame, r"\+2 -1|\+1 -1")
        self.assertTrue(diffs)

    def test_a_status_refresh_with_a_slow_counts_read_keeps_the_last_counts(self):
        # Break: every status refresh swaps in a record with no counts, so the +/- column and
        # the summary total vanish until the slow counts read lands.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.assertRegex(plain(app.render()), r"\+2 -1")
        held = []

        def submit(fn):
            future = Future()
            held.append((future, fn))
            return future

        app.submit = submit
        self.clock.t += 20
        app.refresh(manual=True)
        for future, fn in held:
            future.set_result(fn())
        held.clear()
        app.poll()
        frame = plain(app.render())
        self.assertEqual(len(held), 2)
        self.assertRegex(frame, r"\+2 -1")
        self.fx.write("README.md", "one\nTWO\nthree\nfour\nfive\n", root=self.wt_a)
        for future, fn in held:
            future.set_result(fn())
        self.assertRegex(plain(app.render()), r"\+3 -1")

    def test_stage_from_the_panel_is_previewed_and_refuses_a_reused_pane(self):
        # Break: Git runs without a preview, or a pane reused by another agent between
        # preview and y still receives the write.
        self.fx.write("new.txt", "n\n", root=self.wt_a)
        app = self.app()
        self.select(app, "alpha-task")
        self.keys(app, "2", "s")
        preview = plain(app.render())
        self.assertIn("git add -- new.txt", preview)
        self.assertIn(str(self.wt_a), preview)
        self.assertEqual(self.fx.git(self.wt_a, "diff", "--cached", "--name-only"), "")
        self.transport.pane_get["w1:p1"] = dict(self.panes["alpha-task"], terminal_id="term_other")
        app.handle_key("y")
        self.assertIn("refused", plain(app.render()))
        self.assertEqual(self.fx.git(self.wt_a, "diff", "--cached", "--name-only"), "")
        self.transport.pane_get["w1:p1"] = dict(self.panes["alpha-task"])
        self.keys(app, "s", "y")
        self.assertIn("stage done", plain(app.render()))
        self.assertEqual(self.fx.git(self.wt_a, "diff", "--cached", "--name-only"), "new.txt")

    def test_a_second_git_write_is_refused_while_one_is_running_and_the_first_outcome_is_shown(self):
        # Break: a second confirm overwrites the running job, so the first outcome is never
        # shown and two Git writes run against the same repository at once.
        self.fx.write("new.txt", "n\n", root=self.wt_a)
        held = []

        def submit(fn):
            future = Future()
            if held == [None]:
                held[0] = (future, fn)
            else:
                future.set_result(fn())
            return future

        app = self.app()
        app.submit = submit
        self.select(app, "alpha-task")
        self.keys(app, "2", "s")
        held.append(None)
        app.handle_key("y")
        self.assertIsNotNone(held[0])
        self.assertIsNotNone(app.git_job)
        self.keys(app, "f")
        self.assertIn("fetch refused: stage still running", plain(app.render()))
        self.assertNotEqual(app.screen, "confirm-git")
        self.assertIsNone(app.pending)
        self.assertEqual(self.fx.git(self.wt_a, "diff", "--cached", "--name-only"), "")
        future, fn = held[0]
        future.set_result(fn())
        self.keys(app, "f")
        self.assertIn("stage done", app.status)
        self.assertEqual(self.fx.git(self.wt_a, "diff", "--cached", "--name-only"), "new.txt")
        self.assertEqual(app.screen, "confirm-git")

    def test_commit_from_the_panel_uses_the_previewed_editor_message(self):
        # Break: the commit runs without showing the message, uses a different message than
        # the preview, or commits in the primary checkout instead of the agent worktree.
        self.fx.write("feature.txt", "f\n", root=self.wt_a)
        self.fx.git(self.wt_a, "add", "feature.txt")
        self.transport.editor_body = "Add feature from Firstpane\n"
        app = self.app()
        self.select(app, "alpha-task")
        self.keys(app, "2", "C")
        preview = plain(app.render())
        self.assertIn("Add feature from Firstpane", preview)
        self.assertIn("A feature.txt", preview)
        primary_head = self.fx.head()
        app.handle_key("y")
        self.assertIn("commit done", plain(app.render()))
        self.assertEqual(self.fx.git(self.wt_a, "log", "-1", "--format=%s"), "Add feature from Firstpane")
        self.assertEqual(self.fx.head(), primary_head)

    def test_pull_is_refused_while_the_selected_agent_is_working(self):
        # Break: a pull rewrites the worktree under a working agent.
        self.fx.git(self.wt_b, "push", "-q", "-u", "origin", "fm/beta-task")
        app = self.app()
        self.select(app, "beta-task")
        self.keys(app, "2", "p")
        self.assertIn("agent busy in this worktree: beta-task working", plain(app.render()))

    def test_ambiguous_workspace_is_read_only(self):
        # Break: a meta worktree that disagrees with Herdr's checkout still accepts writes.
        self.transport.snapshot["workspaces"][0]["worktree"] = {
            "repo_key": str(self.fx.primary / ".git"),
            "repo_name": "base",
            "repo_root": str(self.fx.primary),
            "checkout_path": str(self.wt_b),
            "is_linked_worktree": True,
        }
        self.fx.write("x.txt", "x\n", root=self.wt_a)
        app = self.app()
        self.select(app, "alpha-task")
        app.handle_key("2")
        self.assertIn("workspace ambiguous", plain(app.render()).splitlines()[2])
        self.keys(app, "s")
        self.assertIn("stage refused", plain(app.render()))
        self.assertEqual(self.fx.git(self.wt_a, "diff", "--cached", "--name-only"), "")


class InlineMessageTests(PanelFixture):
    def test_a_draft_stays_bound_to_its_lines_through_file_agent_and_tab_switches_and_a_restart(self):
        # Break: switching file, agent or tab loses the draft, shows it under another agent
        # or file, or a restart forgets it; reopening the same line starts an empty draft.
        self.changed_readme()
        self.fx.write("new.txt", "a\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "yarım kaldı")
        self.pick_file(app, "new.txt")
        self.assertNotIn("yarım kaldı", plain(app.render()))
        self.pick_file(app, "README.md")
        self.assertIn("yarım kaldı", self.lines(app)[self.row_of(app, "+ TWO")])
        self.open_changes(app, "beta-task")
        self.assertNotIn("yarım kaldı", plain(app.render()))
        app.handle_key("1")
        again = self.app()
        self.open_changes(again)
        self.assertIn("yarım kaldı", self.lines(again)[self.row_of(again, "+ TWO")])
        self.double_click(again, self.row_of(again, "+ TWO"))
        self.type(again, "!")
        again.handle_key("enter")
        self.assertIn("yarım kaldı!\n", plain(again.render()))

    def test_a_draft_whose_lines_changed_is_shown_stale_and_never_moves_to_other_lines(self):
        # Break: after the agent edits the selected lines the draft silently attaches to
        # whatever line now has that number, or disappears, or sends without saying so.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "eski satır")
        app.handle_key("esc")
        self.fx.write("README.md", "one\nTWO!\nthree\nfour\n", root=self.wt_a)
        self.clock.t += 20
        app.refresh(manual=True)
        lines = self.lines(app)
        row = self.row_of(app, "+ TWO!")
        self.assertNotIn("eski satır", lines[row])
        stale = self.row_of(app, "stale")
        self.assertIn("eski satır", self.lines(app)[stale - 1])
        self.click(app, stale)
        app.handle_key("enter")
        preview = plain(app.render())
        self.assertIn("changed since they were selected", preview)
        self.assertIn("```diff\n+TWO\n```", preview)

    def test_a_busy_box_keeps_the_draft_and_a_retry_once_it_is_clear_fills_it_once(self):
        # Break: a refusal (the captain is typing in FirstMate's box) drops the inline draft
        # or pastes anyway, or the retry after the box is cleared pastes twice.
        self.main.text = "yazıyorum"
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "tekrar dene")
        app.handle_key("enter")
        frame = plain(app.render())
        self.assertIn("not transferred: FirstMate's message box already has text", frame)
        self.assertIn("tekrar dene", frame)
        self.assertEqual(self.main.received, [])
        self.main.text = ""
        self.keys(app, "enter", "y")
        self.assertEqual(len(self.main.received), 1)
        self.assertIn("tekrar dene", self.pasted()[0])
        frame = plain(app.render())
        self.assertIn("press Enter there to send", frame)
        self.assertNotIn("tekrar dene", frame)

    def test_a_paste_the_box_does_not_show_is_unconfirmed_and_never_pasted_twice(self):
        # Break: an unobserved paste is reported as transferred and the draft dropped, or
        # trying again puts a second copy into FirstMate's box.
        self.main.render = False
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "gördün mü")
        self.keys(app, "enter", "y")
        frame = plain(app.render())
        self.assertIn("transfer unconfirmed: FirstMate's message box does not show the message", frame)
        self.assertIn("last send: unconfirmed", frame)
        self.assertIn("gördün mü", frame)
        self.main.render = True
        app.handle_key("enter")
        self.assertIn("already has text", plain(app.render()))
        self.assertEqual(len(self.main.received), 1)

    def test_keys_typed_in_the_inline_editor_are_text_and_paste_is_inert(self):
        # Break: j/k/y typed in the editor move the diff cursor or confirm something, or a
        # pasted newline opens the send preview.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "jky")
        app.handle_bytes(b"\x1b[200~line one\ny 2\x1b[201~")
        self.assertEqual(app.screen, "tree")
        self.assertEqual(self.transport.inbox, [])
        app.handle_bytes(b"\x1b[D\x1b[D\x7f\x1b[3~")
        app.handle_key("enter")
        self.assertIn("\njkyline one\n2\n", plain(app.render()))

    def test_a_second_pane_does_not_silently_overwrite_the_draft(self):
        # Break: two Firstpane panes editing the same lines overwrite each other's draft.
        self.changed_readme()
        one, two = self.app(), self.app()
        for app in (one, two):
            self.open_changes(app)
            self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(one, "from one")
        self.type(two, "from two")
        self.assertIn("draft conflict", plain(two.render()))
        two.handle_key("enter")
        self.assertEqual(self.transport.inbox, [])
        two.handle_key("ctrl-r")
        self.assertIn("from one", plain(two.render()))
        copies = list((self.state / "drafts").glob("conflict-*.txt"))
        self.assertEqual([path.read_text() for path in copies], ["from two"])

    def test_branch_drift_after_selection_is_shown_and_a_changed_body_is_not_sent(self):
        # Break: lines selected on one HEAD are sent after the agent committed without
        # saying so, or y sends text that differs from the preview on screen.
        self.fx.write("README.md", "changed\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ changed"))
        self.type(app, "look")
        self.fx.git(self.wt_a, "commit", "-q", "-am", "agent commit")
        self.clock.t += 20
        app.refresh(manual=True)
        app.handle_key("enter")
        self.assertIn("attachment 1 was captured on fm/alpha-task (no upstream)", plain(app.render()))
        self.fx.git(self.wt_a, "commit", "-q", "--allow-empty", "-m", "again")
        self.clock.t += 20
        app.refresh(manual=True)
        app.handle_key("y")
        self.assertEqual(self.main.received, [])
        self.assertIn("changed since the preview", plain(app.render()))

    def test_narrow_preview_shows_every_character_that_will_be_sent(self):
        # Break: a long workspace path or text line is cut at the pane edge, so the user
        # confirms a message they could not fully read.
        self.changed_readme()
        app = self.app(rows=60, cols=40)
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "a long line that is certainly wider than forty cells, ending here")
        app.handle_key("enter")
        lines = plain(app.render()).splitlines()
        start = lines.index("---") + 1
        shown = "".join(lines[start : lines.index("---", start)]).replace(" ", "")
        app.handle_key("y")
        sent = self.pasted()[0]
        self.assertEqual(shown, sent.replace("\n", "").replace(" ", ""))

    def test_a_saved_unannounced_message_from_before_the_message_box_is_announced_not_resent(self):
        # Break: `a` only finds PR notes, so an inline message an earlier Firstpane saved to
        # the inbox without a wake-up can never be announced, or announcing creates a second
        # note.
        store = DraftStore(self.state / "drafts")
        old = store.load("fm:alpha-task:term_a|diff:old", {"label": "alpha-task"})
        old.update(body="wake up", outcome="saved-unannounced", note_id="n-msg")
        self.assertTrue(store.save(old))
        self.transport.inbox_results = [CmdResult(0, note_json("created", note_id="n-msg"), "")]
        app = self.app()
        self.open_changes(app)
        app.handle_key("a")
        self.assertEqual([call["argv"][1] for call in self.transport.inbox], ["announce"])
        self.assertIn("n-msg", self.transport.inbox[0]["argv"])
        self.assertIn("note outcome: queued", plain(app.render()))
        self.assertEqual(self.main.received, [])

    def test_unconfigured_firstmate_keeps_the_draft_and_sends_nothing(self):
        # Break: without a FirstMate home the inline editor sends somewhere or discards the text.
        self.config.firstmate_home = None
        self.panes["alpha-task"].update(cwd=str(self.wt_a), foreground_cwd=str(self.wt_a))
        self.publish()
        self.changed_readme()
        app = self.app()
        self.open_changes(app, "claude")
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "kept")
        app.handle_key("enter")
        frame = plain(app.render())
        self.assertIn("FirstMate not configured", frame)
        self.assertIn("kept", frame)
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(self.main.received, [])

    def test_a_draft_left_by_the_removed_composer_is_offered_and_sent_with_its_attachments(self):
        # Break: text and attachments typed into the old bottom composer become unreachable
        # once the composer is gone, or are sent without their attachment.
        store = DraftStore(self.state / "drafts")
        old = store.load("fm:alpha-task:term_a", {"label": "alpha-task"})
        old["body"] = "eski kutudan mesaj"
        old["attachments"] = [{
            "kind": "diff", "path": "README.md", "group": "unstaged", "side": "new", "start": 2, "end": 2,
            "head": None, "branch": None, "blob": None, "workspace": str(self.wt_a), "lines": ["+TWO"], "lines_total": 1,
        }]
        self.assertTrue(store.save(old))
        app = self.app()
        self.open_changes(app)
        self.assertIn("eski kutudan mesaj", plain(app.render()))
        app.handle_key("m")
        app.handle_key("enter")
        app.handle_key("y")
        sent = self.pasted()[0]
        self.assertIn("\neski kutudan mesaj\n", sent)
        self.assertIn("[1] diff README.md  unstaged  new lines 2-2", sent)
        self.assertNotIn("eski kutudan mesaj", plain(app.render()))


if __name__ == "__main__":
    unittest.main()
