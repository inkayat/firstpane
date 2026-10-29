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
from tests.test_panel import PanelFixture, plain
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


class ReviewFrameTests(PanelFixture):
    """What the Review tab shows and lets you reach without knowing hidden keys."""

    def screen(self, app):
        return "\n".join(self.lines(app))

    def test_workspace_and_pull_request_are_visible_sub_tabs_and_a_click_keeps_the_place(self):
        # Break: the PR source is reachable only through a hidden key, or switching back loses
        # the workspace diff position.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        screen = self.screen(app)
        self.assertIn("Workspace", screen)
        self.assertIn("Pull Request", screen)
        app.handle_key("G")
        cursor = app.diff_view["cursor"]
        self.click_text(app, "Pull Request")
        self.assertEqual(app.review_source, "pr")
        self.click_text(app, "Workspace")
        self.assertEqual(app.review_source, "workspace")
        self.assertEqual(app.diff_view["cursor"], cursor)

    def test_the_code_review_control_lists_the_skills_and_runs_the_chosen_ones(self):
        # Break: skills and Run review hide in an overflow menu, or the control does not show
        # how many skills are chosen.
        self.changed_readme()
        self.config.review = ReviewConfig(("reviewer",), (make_skill(self.root),), 60)
        app = self.app()
        self.open_changes(app)
        self.assertIn("Code review \u00b7 0 skills", self.screen(app))
        self.click_text(app, "Code review")
        menu = self.screen(app)
        self.assertIn("Run review", menu)
        self.click_text(app, "reviewer")
        self.assertIn("Code review \u00b7 1 skill", self.screen(app))
        self.click_text(app, "Run review")
        self.assertEqual(app.screen, "confirm-review")

    def test_code_review_without_a_command_says_how_to_set_it_up(self):
        # Break: with no review command the control is missing (nothing says why), or it
        # pretends a review can run.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.assertIn("Code review: not set up", self.screen(app))
        self.click_text(app, "Code review")
        self.assertIn("review.command", app.status)
        self.assertEqual(app.screen, "tree")

    def test_the_revision_control_is_visible_and_a_commit_shows_its_own_files(self):
        # Break: commits hide behind an overflow menu, or a pinned commit still shows the
        # workspace's file and file count.
        self.fx.write("one.txt", "alpha\nbeta\n", root=self.wt_a)
        self.fx.write("two.txt", "gamma\ndelta\n", root=self.wt_a)
        self.fx.git(self.wt_a, "add", "one.txt", "two.txt")
        self.fx.git(self.wt_a, "commit", "-q", "-m", "add two files")
        oid = self.fx.head(self.wt_a)
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.assertIn("Uncommitted \u25be", self.screen(app))
        self.click_text(app, "Uncommitted \u25be")
        self.click_text(app, "add two files")
        screen = self.screen(app)
        self.assertIn(f"Commit {oid[:7]} \u25be", screen)
        self.assertIn("one.txt \u25be", screen)
        self.assertIn("1 / 2", screen)
        self.click_text(app, "one.txt \u25be")
        rows = [index + 1 for index, line in enumerate(self.lines(app)) if "two.txt" in line and "\u25be" not in line]
        self.click(app, rows[0], col=8)
        view = app.diff_view
        self.assertEqual(app._line_path(view, view["cursor"]), "two.txt")
        self.assertIn("2 / 2", self.screen(app))

    def test_the_footer_shows_the_selection_mode_and_its_line_count(self):
        # Break: v/V selection has no visible mode, so the user cannot tell a range is active or
        # how many lines a message will carry.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        self.assertIn("NORMAL", self.screen(app))
        app.handle_key("V")
        app.handle_key("j")
        self.assertIn("VISUAL \u00b7 2 lines", self.screen(app))
        app.handle_key("esc")
        self.assertNotIn("VISUAL", self.screen(app))

    def test_the_workspace_sub_tab_shows_the_changed_file_count_and_the_top_tab_does_not(self):
        # Break: the Workspace chip drops the count the design shows, or the count comes back
        # beside the top-level Review tab.
        self.changed_readme()
        self.fx.write("new.txt", "a\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        lines = self.lines(app)
        self.assertEqual(lines[0].split()[:3], ["Agents", "?", "Review"])
        self.assertNotIn("Review 2", lines[0])
        source_row = next(line for line in lines if "Pull Request" in line)
        self.assertIn("Workspace 2", source_row)

    def test_every_added_line_offers_a_plus_that_writes_about_that_line(self):
        # Break: only the cursor line has a [+], so the design's per-line action is missing,
        # or a click on another line's [+] writes about the cursor line instead.
        self.fx.write("README.md", "one\nTWO\nthree\nfour\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        app.render()
        added = [line for line in self.lines(app) if "\u2502 + " in line or "\u258c + " in line]
        self.assertEqual(len(added), 2)
        self.assertTrue(all("[+]" in line for line in added), added)
        row = self.row_of(app, "+ four")
        line = self.lines(app)[row - 1]
        self.click(app, row, col=line.rindex("[+]") + 2)
        self.assertEqual(app.focus, "composer")
        self.assertEqual((app.draft["anchor"]["path"], app.draft["anchor"]["new_start"]), ("README.md", 4))

    def test_the_cursor_line_says_gd_and_gi_have_no_symbol_support(self):
        # Break: the design's inline gd/gI hint is missing, or it promises a lookup Firstpane
        # cannot do.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        cursor_row = next(line for line in self.lines(app) if "\u258c" in line)
        self.assertIn("gd/gI: no symbol support", cursor_row)
        others = [line for line in self.lines(app) if "\u2502" in line and "\u258c" not in line]
        self.assertFalse(any("gd/gI" in line for line in others))

    def test_a_wide_diff_has_a_horizontal_scrollbar_whose_arrows_scroll(self):
        # Break: lines wider than the pane give no sign of what is cut off, or the bar's
        # arrows do nothing.
        self.fx.write("README.md", "one\n" + "x" * 300 + "\nthree\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        app.render()
        bar = self.row_of(app, "\u25c0")
        line = self.lines(app)[bar - 1]
        self.assertIn("\u25b6", line)
        self.click(app, bar, col=line.index("\u25b6") + 1)
        self.assertGreater(app.diff_view["xoff"], 0)
        self.click(app, bar, col=line.index("\u25c0") + 1)
        self.assertEqual(app.diff_view["xoff"], 0)

    def test_a_diff_that_fits_has_no_scrollbar(self):
        # Break: the scrollbar row takes a diff row even when nothing is cut off.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.assertNotIn("\u25c0", "\n".join(self.lines(app)))

    def test_the_selected_line_count_shows_beside_the_file_counts(self):
        # Break: a line range has no count next to the file's +/- figures as in the design.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        app.handle_key("V")
        app.handle_key("j")
        stats = next(line for line in self.lines(app) if "unstaged" in line)
        self.assertIn("2 lines selected", stats)

    def test_the_expand_icon_toggles_focus_and_keeps_the_place(self):
        # Break: the expand icon does nothing, or returning to the normal view jumps the
        # cursor, the scroll, the selected lines, or drops the unsent draft.
        self.fx.write("README.md", "\n".join(f"row {i:02d} " + "x" * 80 for i in range(1, 30)) + "\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        app.render()
        app.handle_key("j")
        app.handle_key("j")
        app.handle_key("V")
        app.handle_key("j")
        bar = self.row_of(app, "\u25c0")
        bar_line = self.lines(app)[bar - 1]
        self.click(app, bar, col=bar_line.index("\u25b6") + 1)
        app.handle_key("enter")
        self.type(app, "keep-me")
        before = self.lines(app)
        header = next(line for line in before if " / " in line)
        diff_rows = sum(1 for line in before if "\u2502" in line or "\u258c" in line)
        place = self._place(app)
        self.assertEqual(place[-1], "keep-me")
        self.click_text(app, "\u2922")
        focused = self.lines(app)
        self.assertNotIn("Write to FirstMate", "\n".join(focused))
        self.assertNotIn(header, focused)
        self.assertIn("Workspace", "\n".join(focused))
        self.assertIn("\u2922", "\n".join(focused))
        self.assertGreater(sum(1 for line in focused if "\u2502" in line or "\u258c" in line), diff_rows)
        self.assertEqual(self._place(app), place)
        self.click_text(app, "\u2922")
        restored = "\n".join(self.lines(app))
        self.assertIn("Write to FirstMate", restored)
        self.assertIn(header, self.lines(app))
        self.assertIn("keep-me", restored)
        self.assertEqual(self._place(app), place)

    def test_a_second_click_uses_the_icon_after_the_header_moves(self):
        # Break: hiding the header leaves the icon on the old row, or a click on the icon
        # where it is drawn does not restore the header and the message.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        before = self.row_of(app, "\u2922")
        self.click_text(app, "\u2922")
        focused = self.lines(app)
        self.assertNotIn("Write to FirstMate", "\n".join(focused))
        after = self.row_of(app, "\u2922")
        self.assertLess(after, before)
        self.click(app, before, col=self.lines(app)[before - 1].index("\u2922") + 1 if "\u2922" in self.lines(app)[before - 1] else 1)
        self.assertNotIn("Write to FirstMate", "\n".join(self.lines(app)))
        self.click_text(app, "\u2922")
        restored = "\n".join(self.lines(app))
        self.assertIn("Write to FirstMate", restored)
        self.assertFalse(app.review_focus)

    def test_writing_a_comment_from_focus_opens_the_message_again(self):
        # Break: choosing a line comment while the header and message are hidden leaves the
        # message hidden, or the message is about a different line.
        self.fx.write("README.md", "one\nTWO\nthree\nfour\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        self.click_text(app, "\u2922")
        self.assertNotIn("Write to FirstMate", self.screen(app))
        row = self.row_of(app, "+ four")
        line = self.lines(app)[row - 1]
        self.click(app, row, col=line.rindex("[+]") + 2)
        shown = self.screen(app)
        self.assertIn("Write to FirstMate", shown)
        self.assertEqual(app.focus, "composer")
        self.assertEqual((app.draft["anchor"]["path"], app.draft["anchor"]["new_start"]), ("README.md", 4))

    def _place(self, app):
        view = app.diff_view
        return (
            app.review_source,
            view["cursor"],
            view["top"],
            view["xoff"],
            view["mark"],
            app.draft["body"] if app.draft else None,
        )

    def test_ctrl_enter_in_the_message_previews_and_sends_nothing(self):
        # Break: Ctrl+Enter is ignored in the message, or it puts text in FirstMate's box
        # without the preview and y.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        app.handle_key("enter")
        self.type(app, "note")
        app.handle_key("ctrl-enter")
        self.assertEqual(app.screen, "confirm-send")
        self.assertEqual(self.main.received, [])

    def test_w_and_b_move_by_word_in_the_workspace_diff(self):
        # Break: w opens a menu in the workspace diff, so word motion works only on PR diffs.
        self.fx.write("README.md", "one\nalpha beta gamma\nthree\n", root=self.wt_a)
        app = self.app()
        self.open_changes(app)
        app.render()
        view = app.diff_view
        view["cursor"] = next(index for index, line in enumerate(view["lines"]) if line.text == "alpha beta gamma")
        app.handle_key("w")
        self.assertIsNone(app.changes_menu)
        self.assertEqual(view["col"], 6)
        app.handle_key("w")
        app.handle_key("b")
        self.assertEqual(view["col"], 6)


class WorkspaceReviewTests(PanelFixture):
    def setUp(self):
        super().setUp()
        self.skill = make_skill(self.root)
        self.config.review = ReviewConfig(("reviewer",), (self.skill,), 60)

    def start(self, app, reviewer):
        app.review_popen = reviewer
        for key in ("S", "enter", "j", "enter"):
            app.handle_key(key)
        self.assertEqual(app.screen, "confirm-review")
        app.handle_key("y")

    def two_changed_files(self):
        self.changed_readme()
        self.fx.write("other.txt", "first\nsecond\n", root=self.wt_a)

    def menu_pick(self, app, needle):
        """Open Code review with S and press Enter on the row containing `needle`."""
        app.handle_key("S")
        items = app._review_menu_items(True)
        app.review_menu["cursor"] = next(index for index, item in enumerate(items) if needle in item[0])
        app.handle_key("enter")

    def test_a_review_of_uncommitted_changes_sends_every_changed_file(self):
        # Break: only the file open in the diff is reviewed, so "current changes" misses files.
        self.two_changed_files()
        app = self.app()
        self.open_changes(app)
        app.render()
        reviewer = FakeReviewer([])
        self.start(app, reviewer)
        self.assertEqual(sorted(row["path"] for row in reviewer.requests[0]["files"]), ["README.md", "other.txt"])

    def test_editing_a_reviewed_file_marks_its_finding_stale_and_keeps_it(self):
        # Break: only a HEAD move marks findings, so a finding on a line edited afterwards still
        # looks current; or the stale finding disappears.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        self.start(app, FakeReviewer([finding("README.md", 2)]))
        app.poll()
        self.assertIsNone(app.review_findings[0].get("stale"))
        self.fx.write("README.md", "one\nTWO changed again\nthree\nfour\n", root=self.wt_a)
        self.clock.t += 20
        app.refresh(manual=True)
        app.render()
        self.assertEqual([item["stale"] for item in app.review_findings], ["stale: file changed since review"])
        app.handle_key("S")
        self.assertIn("stale README.md:2", "\n".join(self.lines(app)))

    def test_a_finding_opens_its_file_and_line_and_ctrl_o_returns(self):
        # Break: findings are only markers on the open file, so a finding in another file
        # cannot be reached, or Ctrl-O does not come back.
        self.two_changed_files()
        app = self.app()
        self.open_changes(app)
        app.render()
        start = (app.diff_view["entry"].path, app.diff_view["cursor"])
        self.start(app, FakeReviewer([finding("other.txt", 2)]))
        app.poll()
        self.menu_pick(app, "other.txt:2")
        app.render()
        view = app.diff_view
        self.assertEqual((view["entry"].path, view["lines"][view["cursor"]].new), ("other.txt", 2))
        app.handle_key("ctrl-o")
        app.render()
        self.assertEqual((app.diff_view["entry"].path, app.diff_view["cursor"]), start)

    def test_a_finding_on_the_selected_lines_goes_into_the_message(self):
        # Break: findings cannot be added to the message context, so FirstMate gets the lines
        # without what the review said about them.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        app.render()
        self.start(app, FakeReviewer([finding("README.md", 2)]))
        app.poll()
        view = app.diff_view
        view["cursor"] = next(index for index, line in enumerate(view["lines"]) if line.kind == "+" and line.new == 2)
        app.handle_key("enter")
        self.type(app, "is this right")
        app.handle_key("enter")
        self.assertEqual(app.screen, "confirm-send")
        self.assertIn("review finding low: look at README.md\nb\n", app.pending["body"])

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

    def keys_through_code_review(self, app):
        """S opens Code review, Enter picks the first skill, then j and Enter run it."""
        for key in ("S", "enter", "j", "enter"):
            app.handle_key(key)

    def test_lines_selected_in_the_pr_diff_become_a_firstmate_draft_pinned_to_the_pr(self):
        # Break: PR diff lines cannot be attached, or the message carries no PR or revision, so
        # FirstMate cannot tell which PR state the lines came from.
        app = self.app()
        app.handle_key("v")
        view = app.diff_view
        view["cursor"] = next(index for index, line in enumerate(view["lines"]) if line.text == "SIDEBAR_LINE")
        app.handle_key("enter")
        self.assertEqual(app.focus, "composer")
        anchor = app.draft["anchor"]
        self.assertEqual((anchor["source"], anchor["path"], anchor["revision"]), ("pr", "app/sidebar.py", "0123456789abcdef0123456789abcdef01234567"))
        app.handle_bytes("why this line".encode())
        app.handle_key("enter")
        self.assertEqual(app.screen, "confirm-send")
        body = app.pending["body"]
        self.assertIn("source pr  revision 0123456789abcdef0123456789abcdef01234567  https://github.com/acme/alpha/pull/7", body)
        self.assertIn("+SIDEBAR_LINE", body)
        self.assertEqual([warning for warning in app.pending["warnings"] if "workspace" in warning], [])

    def test_a_file_in_the_files_section_opens_in_the_review_diff(self):
        # Break: Enter on a PR file opens a plain text page with no line numbers or selection.
        app = self.app()
        app._set_section("files")
        app.handle_key("enter")
        self.assertEqual(app.screen, "tree")
        self.assertTrue(app.pr_diff_open)
        view = app.diff_view
        self.assertEqual(app._line_path(view, view["cursor"]), "app/sidebar.py")

    def test_the_pull_request_source_shows_the_same_review_frame(self):
        # Break: the PR source drops the sub-tabs or the Code review control.
        text = self.text(self.app())
        self.assertIn("Workspace", text)
        self.assertIn("Pull Request #7", text)
        self.assertIn("Code review \u00b7 0 skills", text)

    def test_the_pr_diff_opens_in_the_viewport_and_is_reviewed_by_its_own_headers(self):
        # Break: PR review is unreachable (no diff view), sends a file the PR does not contain,
        # or pins the wrong head.
        app = self.app()
        app.handle_key("v")
        self.assertTrue(app.pr_diff_open)
        self.assertIn("SIDEBAR_LINE", self.text(app))
        reviewer = FakeReviewer([finding("app/sidebar.py", 1)])
        app.review_popen = reviewer
        self.keys_through_code_review(app)
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
        self.keys_through_code_review(app)
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

    def frame(self, text, width, height, full=True):
        data = base64.b64encode(text.encode()).decode()
        return json.dumps({"type": "terminal.frame", "bytes": data, "full": full, "width": width, "height": height}).encode() + b"\n"

    def control_size(self, child):
        argv = child.argv
        return int(argv[argv.index("--cols") + 1]), int(argv[argv.index("--rows") + 1])

    def test_frames_that_arrive_together_are_all_drawn(self):
        # Break: one line is read per poll and the rest wait in a read buffer the next select
        # never reports, so the tab stays blank or a frame behind the shell.
        app = self.app()
        self.attach(app)
        child = self.popens[0]
        child.feed(self.frame("\x1b[1;1Hfirst", 20, 2) + self.frame("\x1b[1;1Hprompt-now", 20, 2, full=False))
        app.render()
        self.assertIn("prompt-now", plain(app.render()))

    def test_the_bottom_row_of_the_shell_is_on_screen(self):
        # Break: the shell is sized taller than the rows the tab shows, so its prompt at the
        # bottom (after output filled the screen) is cut off and typing seems to do nothing.
        app = self.app(rows=30, cols=100)
        self.attach(app)
        child = self.popens[0]
        cols, rows = self.control_size(child)
        self.assertEqual(cols, 100)
        child.feed(self.frame(f"\x1b[1;1Htop-row\x1b[{rows};1Hbottom-prompt", cols, rows))
        screen = plain(app.render())
        self.assertIn("top-row", screen)
        self.assertIn("bottom-prompt", screen)

    def test_default_shell_colors_sit_on_the_firstpane_surface_and_shell_colors_stay(self):
        # Break: Herdr's reset-form SGR (0;39;49) is passed through, so the shell's default
        # cells use the host terminal's colors instead of the surface every other tab uses.
        app = self.app()
        self.attach(app)
        self.popens[0].feed(self.frame("\x1b[1;1H\x1b[0;39;49mplain-cell\x1b[0;1;38;5;4;41mred-cell", 30, 2))
        app.render()
        line = next(line for line in app.render().split("\n") if "plain-cell" in line)
        self.assertEqual(effective_colors(line, "plain-cell"), ("38;2;226;233;248", "48;2;16;22;35"))
        self.assertEqual(effective_colors(line, "red-cell"), ("38;5;4", "41"))

    def test_a_shell_that_is_gone_is_replaced_not_attached(self):
        # Break: a record left from an earlier Herdr server is attached as is; the control
        # closes at once and the tab stays blank.
        app = self.app()
        app.terminal_popen = self.popen
        directory = os.path.realpath(self.wt_a)
        app._terminal_book_for().note(directory, state="ready", label="old", terminal_id="term_gone", pane_id="w9:gone", tab_id="w9:tx", workspace_id="w9")
        app.handle_key("1")
        self.select(app, "alpha-task")
        app._set_tab("terminal")
        app.render()
        self.assertEqual(len(self.created), 1)
        self.assertEqual([child for child in self.popens if "term_gone" in child.argv], [])
        self.assertIn("term_new1", self.popens[-1].argv)

    def test_a_closed_control_says_why_and_takeover_waits_for_t(self):
        # Break: terminal.closed is ignored, so the tab is blank with no reason, or Firstpane
        # takes the shell over from another client without being asked.
        app = self.app()
        self.attach(app)
        reason = "terminal attach failed: terminal term_new1 already has an attached client; retry with --takeover"
        self.popens[0].feed(json.dumps({"type": "terminal.closed", "reason": reason}).encode() + b"\n")
        app.render()
        self.assertIn("attached elsewhere", plain(app.render()))
        self.assertEqual(len(self.popens), 1)
        app.handle_key("t")
        app.render()
        self.assertEqual(len(self.popens), 2)
        self.assertIn("--takeover", self.popens[1].argv)

    def test_a_click_in_the_shell_gives_it_the_keyboard(self):
        # Break: only f/Enter hand the keyboard over, so clicking the shell and typing does nothing.
        app = self.app()
        self.attach(app)
        child = self.popens[0]
        child.feed(self.frame("\x1b[1;1Hclick-here", 20, 2))
        app.render()
        row = next(index for index, line in enumerate(plain(app.render()).split("\n")) if "click-here" in line) + 1
        app.handle_bytes(f"\x1b[<0;3;{row}M\x1b[<0;3;{row}m".encode())
        app.handle_bytes(b"ls")
        sent = [json.loads(chunk) for chunk in b"".join(child.stdin.chunks).splitlines()]
        self.assertIn({"type": "terminal.input", "bytes": base64.b64encode(b"ls").decode()}, sent)


def effective_colors(line, needle):
    """(fg, bg) SGR in force where `needle` starts in a painted line; None is the host default."""
    fg = bg = None
    at = 0
    end = line.index(needle)
    while at < end:
        if line.startswith("\x1b[", at):
            close = line.index("m", at)
            params = line[at + 2 : close].split(";")
            index = 0
            while index < len(params):
                token = params[index]
                if token in ("", "0"):
                    fg = bg = None
                elif token in ("38", "48") and index + 1 < len(params):
                    count = 3 if params[index + 1] == "5" else 5
                    value = ";".join(params[index : index + count])
                    fg, bg = (value, bg) if token == "38" else (fg, value)
                    index += count
                    continue
                elif token == "39":
                    fg = None
                elif token == "49":
                    bg = None
                elif token.isdigit() and (30 <= int(token) <= 37 or 90 <= int(token) <= 97):
                    fg = token
                elif token.isdigit() and (40 <= int(token) <= 47 or 100 <= int(token) <= 107):
                    bg = token
                index += 1
            at = close + 1
        else:
            at += 1
    return fg, bg


class TermGridSafetyTests(unittest.TestCase):
    def test_a_wide_character_takes_two_cells_not_three(self):
        # Break: the second half of a wide character is drawn as an extra space, so every
        # wide character pushes the rest of the row one column right.
        grid = TermGrid()
        grid.apply("\x1b[1;1H漢x".encode(), True, 4, 1)
        self.assertEqual(plain(grid.rows(False)[0]), "漢x ")

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
