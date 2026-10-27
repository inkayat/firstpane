"""Agents rows: line one is title, status and changed files; line two is the harness, model
and effort of that same agent. Without a confirmed harness image (these tests never answer the
kitty graphics transmission) the harness is named; tests/test_harness_icons.py covers the images.
Each test names the production break it exists to catch."""

import re

from firstpane.cells import width
from tests.support import agent_for, pane, selected_line, snapshot, workspace, write_meta
from tests.test_panel import PanelFixture, plain


class IdentityFixture(PanelFixture):
    def publish(self):
        # Every agent row carries a Herdr `name` that is not its harness; the tests prove the
        # harness comes from the detected `agent` id, never from that label.
        rows = list(self.panes.values())
        spaces = [workspace(row["workspace_id"], row["workspace_id"], number=index) for index, row in enumerate(rows, start=1)]
        self.transport.snapshot = snapshot(spaces, rows + [self.main.pane], [agent_for(row, name="renamed-label") for row in rows])

    def add_agent(self, task, number, detected, **meta):
        """A live agent with no worktree (its Git state is unknown) bound by a FirstMate meta."""
        row = pane(pane_id=f"w{number}:p1", terminal_id=f"term_{task}", workspace_id=f"w{number}", tab_id=f"w{number}:t1",
                   agent_status="idle", agent=detected)
        self.panes[task] = row
        self.bind(task, **meta)
        self.publish()

    def bind(self, task, **meta):
        row = self.panes[task]
        fields = dict(project=str(self.fx.primary), herdr_session="lab", herdr_workspace_id=row["workspace_id"],
                      herdr_tab_id=row["tab_id"], herdr_pane_id=row["pane_id"], endpoint_task_id=task)
        fields.update(meta)
        write_meta(self.home, task, **fields)
        self.publish()

    def pair(self, app, title):
        """(first line, second line) of the row titled `title`, without the selection bar."""
        lines = self.lines(app)
        at = next(index for index, line in enumerate(lines) if title in line)
        return lines[at].lstrip("\u258c ").rstrip(), lines[at + 1].lstrip("\u258c ").rstrip()

    def identity(self, app, title):
        return self.pair(app, title)[1]


class RowIdentityTests(IdentityFixture):
    def test_pi_omp_claude_and_grok_are_named_in_one_column_without_substitute_glyphs(self):
        # Break: two runtimes share a name, a substitute glyph claims to be a harness logo, the
        # name column shifts, or the harness is read from Herdr's agent label.
        self.panes["alpha-task"]["agent"] = "pi"
        self.panes["beta-task"]["agent"] = "omp"
        self.bind("alpha-task", harness="pi", model="openai-codex/gpt-5.6", effort="high", worktree=str(self.wt_a))
        self.bind("beta-task", harness="omp", model="openai-codex/gpt-5.6", effort="xhigh", worktree=str(self.wt_b))
        self.add_agent("gamma-task", 3, "claude", harness="claude", model="anthropic/claude-opus-4-6", effort="medium")
        self.add_agent("delta-task", 4, "grok", harness="grok", model="xai/grok-4", effort="low")
        app = self.app()
        self.assertEqual(self.identity(app, "Alpha task"), "pi \u00b7 gpt-5.6 \u00b7 effort: high")
        self.assertEqual(self.identity(app, "Beta task"), "omp \u00b7 gpt-5.6 \u00b7 effort: xhigh")
        self.assertEqual(self.identity(app, "Gamma task"), "Claude \u00b7 claude-opus-4-6 \u00b7 effort: medium")
        self.assertEqual(self.identity(app, "Delta task"), "Grok \u00b7 grok-4 \u00b7 effort: low")
        rows = {title: self.row_of(app, title) for title in ("Alpha task", "Beta task", "Gamma task", "Delta task")}
        lines = self.lines(app)
        self.assertEqual({lines[row].index(name) for name, row in zip(("pi", "omp", "Claude", "Grok"), rows.values())}, {9})
        self.assertNotIn("renamed-label", "".join(self.lines(app)))

    def test_pi_running_a_grok_model_stays_pi(self):
        # Break: the model provider decides the harness icon (Pi on xAI/Grok shown as Grok).
        self.panes["alpha-task"]["agent"] = "pi"
        self.bind("alpha-task", harness="pi", model="xai/grok-4.7", effort="high", worktree=str(self.wt_a))
        app = self.app()
        self.assertEqual(self.identity(app, "Alpha task"), "pi \u00b7 grok-4.7 \u00b7 effort: high")
        self.assertNotIn("Grok \u00b7", plain(app.render()))

    def test_missing_ambiguous_or_empty_model_and_effort_are_dashes(self):
        # Break: an absent field is filled from a default (high), another row, the harness
        # name or the screenshot, or a duplicated meta key silently picks one value.
        self.panes["alpha-task"]["agent"] = "omp"
        self.bind("alpha-task", harness="omp", worktree=str(self.wt_a))
        self.panes["beta-task"]["agent"] = "omp"
        write_meta(self.home, "beta-task", project=str(self.fx.primary), herdr_session="lab", herdr_workspace_id="w2",
                   herdr_tab_id="w2:t1", herdr_pane_id="w2:p1", endpoint_task_id="beta-task", harness="omp",
                   model="a/one\nmodel=b/two", effort="", worktree=str(self.wt_b))
        self.add_agent("gamma-task", 3, "pi")  # a meta with no harness at all
        app = self.app()
        self.assertEqual(self.identity(app, "Alpha task"), "omp \u00b7 \u2014 \u00b7 effort: \u2014")
        self.assertEqual(self.identity(app, "Beta task"), "omp \u00b7 \u2014 \u00b7 effort: \u2014")
        self.assertEqual(self.identity(app, "Gamma task"), "pi \u00b7 \u2014 \u00b7 effort: \u2014")

    def test_a_meta_model_is_not_trusted_without_its_recorded_harness(self):
        # Break: a model/effort line in a meta that names no harness is shown against whatever
        # Herdr detects now.
        self.add_agent("gamma-task", 3, "pi", model="openai-codex/gpt-5.6", effort="high")
        app = self.app()
        self.assertEqual(self.identity(app, "Gamma task"), "pi \u00b7 \u2014 \u00b7 effort: \u2014")

    def test_effort_off_and_auto_are_shown_as_recorded(self):
        # Break: a falsy-looking effort (off/auto) is replaced by a default such as high.
        self.panes["alpha-task"]["agent"] = "pi"
        self.panes["beta-task"]["agent"] = "pi"
        self.bind("alpha-task", harness="pi", model="m/x1", effort="off", worktree=str(self.wt_a))
        self.bind("beta-task", harness="pi", model="m/x2", effort="auto", worktree=str(self.wt_b))
        app = self.app()
        self.assertTrue(self.identity(app, "Alpha task").endswith("effort: off"))
        self.assertTrue(self.identity(app, "Beta task").endswith("effort: auto"))

    def test_a_pane_now_running_another_harness_drops_the_old_model_and_effort(self):
        # Break: the pane's previous agent's model/effort stay on a pane Herdr now detects as
        # a different harness.
        self.panes["alpha-task"]["agent"] = "pi"
        self.bind("alpha-task", harness="omp", model="openai-codex/gpt-6", effort="xhigh", worktree=str(self.wt_a))
        app = self.app()
        self.assertEqual(self.identity(app, "Alpha task"), "pi \u00b7 \u2014 \u00b7 effort: \u2014")

    def test_an_agent_with_no_meta_shows_its_detected_harness_and_dashes(self):
        # Break: a Herdr-only agent borrows a FirstMate agent's model/effort or a global default.
        self.panes["loose"] = pane(pane_id="w9:p1", terminal_id="term_x", workspace_id="w9", tab_id="w9:t1", agent="grok",
                                   agent_status="working", cwd=str(self.wt_a), foreground_cwd=str(self.wt_a))
        self.bind("alpha-task", harness="omp", model="openai-codex/gpt-6", effort="xhigh", worktree=str(self.wt_a))
        app = self.app()
        grok = [line for line in self.lines(app) if "Grok \u00b7" in line]
        self.assertEqual([line.lstrip("\u258c ").rstrip() for line in grok], ["Grok \u00b7 \u2014 \u00b7 effort: \u2014"])

    def test_an_unrecognised_harness_is_named_not_given_a_known_icon(self):
        # Break: opencode (or any other unmapped id) is drawn with a known harness image or name.
        self.add_agent("gamma-task", 3, "opencode", harness="opencode", model="openai/gpt-5", effort="high")
        app = self.app()
        self.assertEqual(self.identity(app, "Gamma task"), "opencode \u00b7 gpt-5 \u00b7 effort: high")

    def test_model_and_effort_follow_their_agent_when_the_meta_changes(self):
        # Break: a refresh keeps the previous values, or one agent's change shows on another row.
        self.panes["alpha-task"]["agent"] = "omp"
        self.panes["beta-task"]["agent"] = "omp"
        self.bind("alpha-task", harness="omp", model="a/m-one", effort="high", worktree=str(self.wt_a))
        self.bind("beta-task", harness="omp", model="a/m-two", effort="low", worktree=str(self.wt_b))
        app = self.app()
        self.assertTrue(self.identity(app, "Alpha task").endswith("m-one \u00b7 effort: high"))
        self.bind("beta-task", harness="omp", model="a/m-three", effort="xhigh", worktree=str(self.wt_b))
        app.refresh(manual=True)
        self.assertTrue(self.identity(app, "Alpha task").endswith("m-one \u00b7 effort: high"))
        self.assertTrue(self.identity(app, "Beta task").endswith("m-three \u00b7 effort: xhigh"))


class RowStatusLineTests(IdentityFixture):
    def test_line_one_is_title_then_status_and_changed_files(self):
        # Break: files or status move to the identity line, unknown Git reads as clean, or a
        # changed-file count belongs to another agent.
        self.fx.write("one.txt", "1\n", root=self.wt_a)
        self.add_agent("gamma-task", 3, "pi")
        app = self.app()
        alpha, _ = self.pair(app, "Alpha task")
        beta, _ = self.pair(app, "Beta task")
        gamma, _ = self.pair(app, "Gamma task")
        self.assertRegex(alpha, r"^\S+\s+Alpha task\s+Idle \u00b7 1 file\s+\u203a$")
        self.assertRegex(beta, r"^\S+\s+Beta task\s+Working \u00b7 clean\s+\u203a$")
        self.assertRegex(gamma, r"^\S+\s+Gamma task\s+Idle \u00b7 workspace unknown\s+\u203a$")
        self.assertNotIn("clean", gamma)

    def test_status_changes_update_the_words_and_the_age(self):
        # Break: the status word or age stays after a refresh, or an agent first seen in its
        # current state gets an invented age.
        app = self.app()
        self.assertNotIn("ago", self.pair(app, "Alpha task")[0])
        self.panes["beta-task"]["agent_status"] = "done"
        self.publish()
        self.clock.t += 5
        app.refresh(manual=True)
        self.assertRegex(self.pair(app, "Beta task")[0], r"Done \u00b7 clean \u00b7 now\s+\u203a$")
        self.clock.t += 5 * 60
        self.assertRegex(self.pair(app, "Beta task")[0], r"Done \u00b7 clean \u00b7 5m ago\s+\u203a$")

    def test_the_branch_stays_in_the_selected_agents_footer_not_in_rows(self):
        # Break: the branch is repeated on the rows instead of the footer, or the footer keeps
        # the previous agent's branch after the selection moves.
        app = self.app(rows=24, cols=60)
        rows = "".join(line for line in self.lines(app) if not line.startswith(" primary / "))
        self.assertNotIn("fm/alpha-task", rows)
        self.assertNotIn("fm/beta-task", rows)
        footer = next(line for line in self.lines(app) if line.startswith(" primary / "))
        self.assertTrue(footer.rstrip().endswith("fm/alpha-task"), footer)
        app.handle_key("j")
        footer = next(line for line in self.lines(app) if line.startswith(" primary / "))
        self.assertTrue(footer.rstrip().endswith("fm/beta-task"), footer)

    def test_a_click_on_either_line_selects_that_agent_and_selection_keeps_rows_intact(self):
        # Break: the identity line is not a hit target, or a click on it selects the row above
        # or below.
        self.panes["alpha-task"]["agent"] = "pi"
        self.panes["beta-task"]["agent"] = "omp"
        self.bind("alpha-task", harness="pi", model="m/a", effort="low", worktree=str(self.wt_a))
        self.bind("beta-task", harness="omp", model="m/b", effort="high", worktree=str(self.wt_b))
        app = self.app()
        before = {title: self.identity(app, title) for title in ("Alpha task", "Beta task")}
        self.click(app, self.row_of(app, "omp \u00b7 b"))
        self.assertEqual(app.target["label"], "beta-task")
        self.assertIn("Beta task", selected_line(app.render()))
        self.click(app, self.row_of(app, "pi \u00b7 a"), col=40)
        self.assertEqual(app.target["label"], "alpha-task")
        self.assertEqual({title: self.identity(app, title) for title in before}, before)


class RowWidthAndSafetyTests(IdentityFixture):
    LONG_MODEL = "vendor/" + "very-long-model-name-" * 5 + "xhigh"

    def long_row(self):
        self.panes["alpha-task"]["agent"] = "omp"
        self.bind("alpha-task", harness="omp", model=self.LONG_MODEL, effort="xhigh", worktree=str(self.wt_a))

    def test_a_long_model_is_cut_before_the_effort_and_nothing_overflows(self):
        # Break: a long model pushes effort off screen, or a line is wider than the pane.
        self.long_row()
        for rows, cols in ((30, 100), (24, 72), (24, 56)):
            app = self.app(rows=rows, cols=cols)
            lines = self.lines(app)
            self.assertTrue(all(width(line) <= cols for line in lines), (cols, lines))
            line = self.identity(app, "Alpha task")
            self.assertTrue(line.startswith("omp \u00b7 very-long"), line)
            self.assertIn("\u2026 \u00b7 effort: xhigh", line)

    def test_a_narrow_pane_keeps_title_status_and_harness_name(self):
        # Break: at 30-44 columns the status word or harness name drops out, or a row is cut
        # mid-cell.
        self.long_row()
        self.panes["alpha-task"]["title"] = "A very long task title that cannot possibly fit"
        self.panes["alpha-task"]["agent_status"] = "blocked"
        self.publish()
        for rows, cols in ((20, 44), (16, 36), (12, 30)):
            app = self.app(rows=rows, cols=cols)
            lines = self.lines(app)
            self.assertEqual(len(lines), rows)
            self.assertTrue(all(width(line) <= cols for line in lines), (cols, lines))
            first, second = next((a, b) for a, b in zip(lines, lines[1:]) if "Waiting" in a)
            self.assertIn("Waiting", first)
            self.assertIn("omp", second)
            self.assertTrue(first.rstrip().endswith("\u203a"), first)

    def test_hostile_text_in_task_harness_model_and_effort_is_inert(self):
        # Break: escape sequences or bidi overrides from meta or Herdr reach the terminal.
        self.panes["alpha-task"]["agent"] = "omp"
        self.bind("alpha-task", harness="omp", model="x/\x1b]0;PWN\x07gpt\x1b[31m-5\u202e", effort="hi\x1b[2Jgh", worktree=str(self.wt_a))
        self.add_agent("gamma-task", 3, "\x1b]0;PWN\x07pi\x1b[31m")
        app = self.app()
        frame = app.render()
        self.assertNotIn("PWN", frame)
        self.assertNotIn("\x1b]", frame)
        self.assertIsNone(re.search(r"\x1b\[(?!(?:[0-9;]*m))", frame))
        self.assertEqual(self.identity(app, "Alpha task"), "omp \u00b7 gpt-5<bidi> \u00b7 effort: high")


class RowStatusColorTests(IdentityFixture):
    WORKING = "\x1b[38;2;110;231;183m"
    IDLE = "\x1b[38;2;254;255;255m"
    WAITING = "\x1b[38;2;238;203;83m"

    def test_working_and_done_are_green_idle_is_white_waiting_is_the_old_working_yellow(self):
        # Break: Working keeps its old yellow, Idle is not white, Waiting loses the yellow Working
        # used to have, or Done loses its check mark (the two greens stay told apart by glyph
        # and word).
        self.panes["alpha-task"]["agent_status"] = "idle"
        self.panes["beta-task"]["agent_status"] = "working"
        self.add_agent("gamma-task", 3, "pi")
        self.panes["gamma-task"]["agent_status"] = "done"
        self.add_agent("delta-task", 4, "pi")
        self.panes["delta-task"]["agent_status"] = "blocked"
        self.publish()
        frame = self.app(rows=40).render()
        for color, word, glyph in (
            (self.IDLE, "Idle", "\u25cf"),
            (self.WORKING, "Working", "\u25cf"),
            (self.WORKING, "Done", "\u2713"),
            (self.WAITING, "Waiting for response", "\u25cb"),
        ):
            self.assertIn(color + word + "\x1b[0m", frame, word)
            self.assertIn(color + glyph + "\x1b[0m", frame, glyph)

    def test_without_color_every_status_keeps_its_word_and_glyph(self):
        # Break: NO_COLOR drops the status word or leaves escapes in the rows.
        app = self.app(env={"NO_COLOR": "1"})
        frame = app.render()
        self.assertNotIn("\x1b", frame)
        self.assertRegex(frame, r"\u25cf\s+Beta task\s+Working")
