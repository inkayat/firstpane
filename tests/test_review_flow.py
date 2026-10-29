"""Review runs end to end through the app: one-shot outcomes, commit and PR scopes, jump-back,
and the terminal tab's shell lifecycle. Review commands, `gh` and Herdr are fakes; git is real
where a workspace is needed. Nothing reaches a model, GitHub or a real Herdr."""

import base64
import json
import os
import unittest

from firstpane.config import Config
from firstpane.diffview import NO_SYMBOL
from firstpane.review import RESULT_SCHEMA, ReviewConfig
from firstpane.termframe import TermGrid
from firstpane.workspace import FileEntry, parse_diff
from tests.support import CmdResult, pane
from tests.test_panel import PanelFixture
from tests.test_pr_review import DIFF, ReviewFixture


class _Child:
    pid = 4242

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class FakeReviewer:
    """A review command that answers every request with the findings it was given."""

    def __init__(self, findings):
        self.findings = findings
        self.requests = []

    def __call__(self, argv, **kwargs):
        request = json.loads(kwargs["stdin"].read())
        self.requests.append(request)
        result = {
            "schema": RESULT_SCHEMA,
            "run_id": request["run_id"],
            "revision_key": request["revision_key"],
            "status": "complete",
            "findings": self.findings,
        }
        kwargs["stdout"].write(json.dumps(result).encode())
        kwargs["stdout"].flush()
        return _Child()


def finding(path, line, ident="f1", side="new"):
    return {"id": ident, "path": path, "side": side, "start_line": line, "end_line": line, "severity": "low", "title": f"look at {path}", "body": "b"}


def make_skill(root):
    skill = root / "skills" / "reviewer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("review carefully\n")
    return str(skill)


class WorkspaceReviewTests(PanelFixture):
    def setUp(self):
        super().setUp()
        self.skill = make_skill(self.root)
        self.config.review = ReviewConfig(("reviewer",), (self.skill,), 60)

    def start(self, app, reviewer):
        app.review_popen = reviewer
        app._menu_pick(("skill", self.skill))
        app._menu_pick(("review",))
        self.assertEqual(app.screen, "confirm-review")
        app.handle_key("y")

    def test_a_finished_review_is_applied_once_and_later_status_and_stale_marks_survive(self):
        # Break: every poll re-reads the finished run, so a later status message and a stale
        # label are overwritten by "review complete" and fresh unmarked findings.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        self.start(app, FakeReviewer([finding("README.md", 2)]))
        app.poll()
        self.assertEqual(app.status, "review complete: 1 findings")
        self.assertIsNone(app.review_supervisor)
        app.status = "something else"
        app.review_head = "0" * 40
        view = app.diff_view
        view["cursor"] = next(index for index, line in enumerate(view["lines"]) if line.new == 2)
        app._note_finding()
        self.assertEqual(app.status, "stale: HEAD moved")
        app.render()
        app.poll()
        app.render()
        self.assertEqual(app.status, "stale: HEAD moved")
        self.assertEqual(app.review_findings[0]["stale"], "stale: HEAD moved")

    def test_gd_and_gI_in_the_review_diff_report_no_symbol_support_without_moving(self):
        # Break: "g" opens the Git menu before the diff viewport sees it, so gd/gI do nothing.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        for second in ("d", "I"):
            app.status = ""
            cursor = app.diff_view["cursor"]
            app.handle_key("g")
            self.assertIsNone(app.changes_menu)
            app.handle_key(second)
            self.assertEqual(app.status, NO_SYMBOL)
            self.assertEqual(app.diff_view["cursor"], cursor)

    def test_a_running_review_is_not_started_twice(self):
        # Break: a second Run review replaces the supervisor and orphans the running child.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()

        class Running(_Child):
            def poll(self):
                return None

        app.review_popen = lambda argv, **kwargs: Running()
        app._menu_pick(("skill", self.skill))
        app._menu_pick(("review",))
        app.handle_key("y")
        self.assertEqual(app.status, "review running")
        app._menu_pick(("review",))
        self.assertEqual(app.status, "a review is already running")
        self.assertEqual(app.screen, "tree")

    def commit_two_files(self):
        self.fx.write("one.txt", "alpha\nbeta\n", root=self.wt_a)
        self.fx.write("two.txt", "gamma\ndelta\n", root=self.wt_a)
        self.fx.git(self.wt_a, "add", "one.txt", "two.txt")
        self.fx.git(self.wt_a, "commit", "-q", "-m", "two files")
        return self.fx.head(self.wt_a)

    def test_a_commit_of_a_clean_workspace_is_reviewed_with_every_file_of_the_commit(self):
        # Break: the commit view needs an uncommitted file, sends one unrelated file, drops
        # findings on the commit's other files, or labels the commit "uncommitted".
        oid = self.commit_two_files()
        app = self.app()
        self.open_changes(app)
        app._menu_pick(("rev", oid))
        view = app.diff_view
        self.assertIsNotNone(view)
        self.assertEqual(view["revision"], oid)
        self.assertIn("two.txt", view["patch"])
        reviewer = FakeReviewer([finding("two.txt", 1)])
        self.start(app, reviewer)
        request = reviewer.requests[0]
        self.assertEqual(request["scope"], "commit")
        self.assertEqual(request["revision"]["commit"], oid)
        self.assertEqual(request["revision"]["parent"], self.fx.git(self.wt_a, "rev-parse", oid + "^"))
        self.assertEqual([row["path"] for row in request["files"]], ["one.txt", "two.txt"])
        self.assertEqual({row["status"] for row in request["files"]}, {"added"})
        app.poll()
        self.assertEqual(app.status, "review complete: 1 findings")
        lines = view["lines"]
        marked = [index for index in range(len(lines)) if app._finding_marker(index)]
        self.assertEqual([view["paths"][index] for index in marked], ["two.txt"])
        self.assertIn(" ! ", app._finding_marker(marked[0]))

    def test_quoted_non_ascii_paths_are_reviewed_under_their_real_names(self):
        # Break: git's quoted header ("a/\\303\\251.txt") is skipped, so the commit has no files
        # to review or findings on that file are dropped.
        self.fx.write("\u00e9.txt", "caf\u00e9\n", root=self.wt_a)
        self.fx.git(self.wt_a, "add", "-A")
        self.fx.git(self.wt_a, "commit", "-q", "-m", "accent")
        oid = self.fx.head(self.wt_a)
        app = self.app()
        self.open_changes(app)
        app._menu_pick(("rev", oid))
        reviewer = FakeReviewer([finding("\u00e9.txt", 1)])
        self.start(app, reviewer)
        self.assertEqual([row["path"] for row in reviewer.requests[0]["files"]], ["\u00e9.txt"])
        app.poll()
        self.assertEqual(app.status, "review complete: 1 findings")
        self.assertEqual(set(app.diff_view["paths"]) - {None}, {"\u00e9.txt"})

    def test_a_message_in_a_commit_diff_names_the_lines_real_file_and_never_spans_files(self):
        # Break: the draft is attached to a fake "commit abc1234" path, or one draft mixes files.
        oid = self.commit_two_files()
        app = self.app()
        self.open_changes(app)
        app._menu_pick(("rev", oid))
        view = app.diff_view
        second = next(index for index, line in enumerate(view["lines"]) if view["paths"][index] == "two.txt" and line.kind == "+")
        view["cursor"], view["mark"] = second, None
        app._open_diff_message()
        anchor = app.draft["anchor"]
        self.assertEqual((anchor["path"], anchor["group"], anchor["revision"]), ("two.txt", "commit", oid))
        app.handle_key("esc")
        app.draft = None
        first = next(index for index, line in enumerate(view["lines"]) if view["paths"][index] == "one.txt" and line.kind == "+")
        view["cursor"], view["mark"] = first, second
        app.status = ""
        app._open_diff_message()
        self.assertEqual(app.status, "select lines of one file to write about")
        self.assertIsNone(app.draft)

    def test_a_commit_draft_sits_under_its_own_files_identical_row_and_goes_stale_with_it(self):
        # Break: a draft on two.txt line 1 is drawn under one.txt's identical "+x" row, and never
        # reported stale when two.txt's own row is gone.
        self.fx.write("one.txt", "x\n", root=self.wt_a)
        self.fx.write("two.txt", "x\n", root=self.wt_a)
        self.fx.git(self.wt_a, "add", "one.txt", "two.txt")
        self.fx.git(self.wt_a, "commit", "-q", "-m", "same row twice")
        oid = self.fx.head(self.wt_a)
        app = self.app()
        self.open_changes(app)
        app._menu_pick(("rev", oid))
        view = app.diff_view
        second = next(index for index, line in enumerate(view["lines"]) if view["paths"][index] == "two.txt" and line.kind == "+")
        view["cursor"], view["mark"] = second, None
        app._open_diff_message()
        app.composer.text = "about two"
        app.handle_key("esc")
        rows = self.lines(app)
        marks = [index for index, line in enumerate(rows) if "+ x" in line]
        draft_row = next(index for index, line in enumerate(rows) if "about two" in line)
        self.assertEqual(draft_row, marks[1] + 1)
        draft = app.drafts.open_drafts()[0]
        draft["anchor"]["rows"][0][3] = "changed"
        stale = [text for text, _ in app._diff_rows(True, 40, [draft])]
        self.assertTrue(any("about two" in text for text in stale[:1]), stale)

    def test_switching_agent_drops_the_commit_pin_and_the_other_agents_findings(self):
        # Break: the pinned commit runs in another repository, or agent A's findings are drawn
        # on agent B's diff.
        oid = self.commit_two_files()
        app = self.app()
        self.open_changes(app)
        app._menu_pick(("rev", oid))
        self.start(app, FakeReviewer([finding("one.txt", 1)]))
        app.poll()
        self.assertEqual(len(app.review_findings), 1)
        app.handle_key("1")
        self.select(app, "beta-task")
        self.assertIsNone(app.revision_oid)
        self.assertEqual(app.review_findings, [])
        self.assertEqual(app.review_jumps, [])
        app.handle_key("2")
        app.render()
        self.assertNotEqual((app.diff_view or {}).get("revision"), oid)

    def test_g_jumps_are_recorded_and_ctrl_o_returns_to_the_row(self):
        # Break: gg and G leave no earlier position, so Ctrl-O only ever says "no earlier position".
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        view = app.diff_view
        start = view["cursor"]
        app.handle_key("G")
        self.assertNotEqual(view["cursor"], start)
        app.handle_key("ctrl-o")
        self.assertEqual(view["cursor"], start)
        app.handle_key("ctrl-o")
        self.assertEqual(app.status, "no earlier position")


class PullRequestReviewTests(ReviewFixture):
    OIDS = ["a" * 40, "b" * 40, "c" * 40]
    COMMIT_DIFF = "diff --git a/app/sidebar.py b/app/sidebar.py\n--- a/app/sidebar.py\n+++ b/app/sidebar.py\n@@ -1 +1 @@\n-before\n+COMMIT_LINE\n"

    def setUp(self):
        super().setUp()
        self.skill = make_skill(self.root)
        self.config.review = ReviewConfig(("reviewer",), (self.skill,), 60)
        self.views[7]["commits"] = [{"oid": oid, "messageHeadline": f"commit {index}"} for index, oid in enumerate(self.OIDS)]
        self.api_calls = []

        def commit_diff(call):
            self.api_calls.append(call["argv"])
            return CmdResult(0, self.COMMIT_DIFF)

        self.transport.handlers.insert(0, (lambda argv: argv[:2] == ["gh", "api"] and any("/commits/" in part for part in argv), commit_diff))

    def start(self, app, reviewer):
        app.review_popen = reviewer
        app.handle_key("y")

    def test_the_pr_diff_opens_in_the_viewport_and_is_reviewed_by_its_own_headers(self):
        # Break: PR review is unreachable (no diff view), sends a file the PR does not contain,
        # or pins the wrong head.
        app = self.app()
        app.handle_key("v")
        self.assertTrue(app.pr_diff_open)
        self.assertIn("SIDEBAR_LINE", self.text(app))
        reviewer = FakeReviewer([finding("app/sidebar.py", 1)])
        app.review_popen = reviewer
        app._run_menu(("skill", self.skill), app._current_pr())
        app._run_menu(("review",), app._current_pr())
        self.assertEqual(app.screen, "confirm-review")
        app.handle_key("y")
        request = reviewer.requests[0]
        self.assertEqual(request["scope"], "pr")
        self.assertEqual(request["revision"]["head"], "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual([(row["path"], row["status"]) for row in request["files"]], [("app/old.py", "deleted"), ("app/sidebar.py", "modified")])
        self.assertIsNone(request["repository"]["path"])
        app.poll()
        self.assertEqual(app.status, "review complete: 1 findings")
        self.assertIn(" ! ", self.text(app))

    def test_the_commit_picker_pins_the_chosen_commit_not_the_oldest(self):
        # Break: C pins commits[0] (the oldest) and says so without any effect on the diff.
        app = self.app()
        app.handle_key("C")
        self.assertEqual(app.pr_menu_kind, "commits")
        text = self.text(app)
        self.assertLess(text.index("c" * 7), text.index("a" * 7))
        app.handle_key("j")
        app.handle_key("enter")
        self.assertEqual(app.pr_commit_oid, self.OIDS[2])
        self.assertTrue(app.pr_diff_open)
        self.assertTrue(any(self.OIDS[2] in part for part in self.api_calls[-1]))
        self.assertIn("COMMIT_LINE", self.text(app))
        reviewer = FakeReviewer([])
        app.review_popen = reviewer
        app._run_menu(("skill", self.skill), app._current_pr())
        app._run_menu(("review",), app._current_pr())
        app.handle_key("y")
        request = reviewer.requests[0]
        self.assertEqual((request["scope"], request["revision"]["commit"]), ("pr_commit", self.OIDS[2]))
        app.handle_key("U")
        self.assertIsNone(app.pr_commit_oid)
        self.assertIn("SIDEBAR_LINE", self.text(app))

    def test_ctrl_o_returns_from_the_pr_to_the_workspace_position(self):
        # Break: switching Workspace / Pull Request records no position, so Ctrl-O cannot go back.
        app = self.app()
        app._set_review_source("workspace")
        lines = [line for line in parse_diff("@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n")]
        app.diff_view = {
            "key": ("w",), "entry": FileEntry("unstaged", "M", "x.py"), "lines": lines, "cursor": 2, "mark": None,
            "top": 0, "xoff": 0, "error": "", "workspace": None, "head": None, "branch": None,
            "patch": "p", "paths": None, "revision": "uncommitted",
        }
        app._set_review_source("pr")
        app.handle_key("v")
        self.assertEqual(app.review_source, "pr")
        app.diff_view["cursor"] = 0
        app.handle_key("ctrl-o")
        self.assertEqual(app.review_source, "workspace")
        self.assertEqual(app.diff_view["cursor"], 2)


class TerminalLifecycleTests(PanelFixture):
    def setUp(self):
        super().setUp()
        self.config.herdr_pane_id = "w9:p9"
        self.transport.pane_get["w9:p9"] = pane(pane_id="w9:p9", terminal_id="term_own", workspace_id="w9", tab_id="w9:t1")
        self.created = []
        self.closed = []
        self.popens = []

        def create(params):
            index = len(self.created) + 1
            self.created.append(params)
            self.transport.pane_get[f"w9:n{index}"] = pane(pane_id=f"w9:n{index}", terminal_id=f"term_new{index}", workspace_id="w9", tab_id=f"w9:t{index}")
            return {"id": "t", "result": {"root_pane": {"pane_id": f"w9:n{index}"}}}

        self.transport.socket_handlers["tab.create"] = create
        self.transport.socket_handlers["pane.close"] = lambda params: self.closed.append(params) or {"id": "t", "result": {}}

    def popen(self, argv, **kwargs):
        child = _Terminal(argv)
        self.addCleanup(child.close)
        self.popens.append(child)
        return child

    def attach(self, app):
        app.terminal_popen = self.popen
        app.handle_key("1")
        self.select(app, "alpha-task")
        app._set_tab("terminal")
        app.render()

    def test_closing_the_terminal_detaches_it_and_the_next_shell_is_attached(self):
        # Break: close leaves the dead control attached, so the new shell never receives input
        # and the old screen stays up.
        app = self.app()
        self.attach(app)
        self.assertEqual(len(self.popens), 1)
        self.assertIn("term_new1", self.popens[0].argv)
        app.terminal_grid.apply(b"\x1b[1;1Hold-shell", True, 20, 2)
        app.handle_key("x")
        app.handle_key("y")
        self.assertEqual(len(self.closed), 1)
        self.assertIsNone(app.terminal_control)
        self.assertEqual(app.terminal_grid.cells, [])
        app.render()
        self.assertEqual(len(self.popens), 2)
        self.assertIn("term_new2", self.popens[1].argv)
        self.assertEqual(app.terminal_status, "")
        self.assertNotIn("old-shell", "\n".join(app.terminal_grid.rows(False)))

    def test_selecting_another_agent_attaches_that_agents_shell(self):
        # Break: input keeps going to the previous directory's shell after the selection moves.
        app = self.app()
        self.attach(app)
        app.terminal_grid.apply(b"\x1b[1;1Halpha-screen", True, 20, 2)
        app.handle_key("1")
        self.select(app, "beta-task")
        app._set_tab("terminal")
        app.render()
        self.assertEqual(len(self.popens), 2)
        self.assertIn("term_new2", self.popens[1].argv)
        self.assertNotIn("alpha-screen", "\n".join(app.terminal_grid.rows(False)))
        self.assertTrue(self.popens[0].released)

    def test_a_dead_control_does_not_raise_on_input_or_resize(self):
        # Break: a closed pipe raises BrokenPipeError out of the key path.
        app = self.app()
        self.attach(app)
        app.terminal_control.stdin = _Broken()
        app.terminal_control.input_text("x")
        app.terminal_control.resize(10, 5)
        self.assertEqual(app.terminal_control.feed_keys(b"a"), None)

    def test_malformed_and_unsupported_frames_are_reported_and_the_last_good_frame_stays(self):
        # Break: a bad base64 or a C1 control crashes the poll or corrupts the screen, and
        # an unsupported sequence is silently ignored.
        app = self.app()
        self.attach(app)
        child = self.popens[0]

        def send(payload):
            child.feed(json.dumps(payload).encode() + b"\n")
            app.poll()

        good = base64.b64encode(b"\x1b[1;1Hgood").decode()
        send({"type": "terminal.frame", "bytes": good, "full": True, "width": 10, "height": 2})
        self.assertIn("good", app.terminal_grid.rows(False)[0])
        send({"type": "terminal.frame", "bytes": "!!!not base64", "width": 10, "height": 2})
        self.assertTrue(app.terminal_status.startswith("Terminal output"))
        send({"type": "terminal.frame", "bytes": base64.b64encode("\x1b[1;1H\x9b31mX".encode()).decode(), "width": 10, "height": 2})
        self.assertIn("good", app.terminal_grid.rows(False)[0])
        self.assertNotIn("\x9b", "".join(app.terminal_grid.rows(False)))
        send({"type": "terminal.frame", "bytes": good, "width": "wide", "height": 2})
        self.assertTrue(app.terminal_status.startswith("Terminal output"))
        send({"type": "terminal.frame", "bytes": good, "full": False, "width": 10, "height": 2})
        self.assertTrue(app.terminal_status.startswith("Terminal output"))
        send({"type": "terminal.frame", "bytes": good, "full": True, "width": 10, "height": 2})
        self.assertEqual(app.terminal_status, "")


class TermGridSafetyTests(unittest.TestCase):
    def test_c1_controls_and_delete_are_not_drawn(self):
        # Break: U+009B (CSI) or DEL from the shell is re-emitted to the host terminal.
        for char in ("\x9b", "\x7f", "\x85"):
            grid = TermGrid()
            self.assertEqual(grid.apply(f"\x1b[1;1Ha{char}b".encode(), True, 5, 1), "unsupported")
            self.assertNotIn(char, "".join(grid.rows(False)))

    def test_an_unsupported_frame_leaves_the_previous_screen_intact(self):
        # Break: a half-applied frame leaves part of the new text over the old screen.
        grid = TermGrid()
        self.assertIsNone(grid.apply(b"\x1b[1;1Hhello", True, 5, 1))
        self.assertEqual(grid.apply(b"\x1b[1;1HXY\x1b[K", False, 5, 1), "unsupported")
        self.assertIn("hello", grid.rows(False)[0])
        self.assertNotIn("XY", grid.rows(False)[0])


class _Broken:
    def write(self, data):
        raise BrokenPipeError()

    def flush(self):
        raise BrokenPipeError()

    def close(self):
        raise BrokenPipeError()


class _Sink:
    def __init__(self):
        self.chunks = []

    def write(self, data):
        self.chunks.append(data)

    def flush(self):
        return None

    def close(self):
        return None


class _Terminal:
    pid = 77

    def __init__(self, argv):
        self.argv = argv
        read, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read, "rb")
        self.stdin = _Sink()
        self.released = False

    def feed(self, data):
        os.write(self.write_fd, data)

    def close(self):
        self.stdout.close()
        os.close(self.write_fd)

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.released = True
        return 0


if __name__ == "__main__":
    unittest.main()


class PatchHeaderTests(unittest.TestCase):
    PATCH = (
        'diff --git "a/\\303\\251.txt" "b/\\303\\251.txt"\nnew file mode 100644\n--- /dev/null\n+++ "b/\\303\\251.txt"\n@@ -0,0 +1 @@\n+x\n'
        "diff --git a/d b/e/f.txt b/d b/e/f.txt\n--- a/d b/e/f.txt\n+++ b/d b/e/f.txt\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/o.txt b/n.txt\nsimilarity index 90%\nrename from o.txt\nrename to n.txt\n@@ -1 +1 @@\n-a\n+b\n"
    )

    def test_files_and_row_paths_come_from_the_headers_whatever_the_quoting(self):
        # Break: quoted headers vanish, a directory named "d b" splits the path, or a rename
        # keeps the old name for its rows.
        from firstpane.review import _spans, patch_files, patch_line_paths

        files = patch_files(self.PATCH)
        self.assertEqual([(row["path"], row["old_path"], row["status"]) for row in files], [
            ("é.txt", None, "added"), ("d b/e/f.txt", None, "modified"), ("n.txt", "o.txt", "renamed"),
        ])
        paths = patch_line_paths(self.PATCH)
        self.assertEqual(len(paths), len(parse_diff(self.PATCH)))
        self.assertEqual(set(paths), {"é.txt", "d b/e/f.txt", "n.txt"})
        self.assertEqual(set(_spans(self.PATCH)), {"é.txt", "d b/e/f.txt", "n.txt"})
