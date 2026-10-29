"""Board layout and interaction: lanes, cards, fixed detail and message block, filters, overflow.

The same lab home and fake `fm-board list --json` as test_board. Nothing here writes a task.
"""

from types import SimpleNamespace

from firstpane.cells import width
from tests import test_board
from tests.support import FakePi, pane, strip_sgr, write_meta


def task(ident, title, state, project="firstpane", agent=None, running=None, review=False, kind="ship"):
    return {
        "id": ident, "project": project, "title": title, "state": state, "agent": agent, "kind": kind,
        "source": "firstmate", "since": None, "running": running, "captain_review": review,
        "updated_at": "2026-10-26T00:00:00Z",
    }


REFERENCE = [
    task("fp-keys-ab12", "Keyboard shortcuts", "todo", "dotfiles"),
    task("fp-term-cd34", "Terminal selection transfer", "todo", "firstmate"),
    task("fp-side-ef56", "Herdr right panel", "in_progress", "dotfiles", "herdr-project-sidebar", "editing"),
    task("fp-sync-0a1b", "Workspace alignment", "in_progress", "firstmate", "manual-sync"),
    task("fp-sum-2c3d", "Project status summary", "waiting_review", "dotfiles", "status-review", review=True),
    task("fp-take-4e5f", "Takeover review", "waiting_review", "alter", "takeover-review"),
    task("fp-old-0001", "Landed row", "done", "dotfiles"),
]


class LayoutFixture(test_board.BoardFixture):
    def setUp(self):
        super().setUp()
        self.tasks[:] = [dict(item) for item in REFERENCE]

    def board(self, rows=36, cols=120, env=None):
        if env:
            self.env.update(env)
        app = self.app(rows=rows, cols=cols)
        app.handle_key("3")
        return app

    def lines(self, app):
        return strip_sgr(app.render()).splitlines()

    def text(self, app):
        return "\n".join(self.lines(app))

    def find(self, app, needle):
        for index, line in enumerate(self.lines(app)):
            if needle in line:
                return index + 1, line.index(needle) + 1
        raise AssertionError(f"{needle!r} not on screen:\n" + self.text(app))

    def click(self, app, needle, shift=0):
        row, column = self.find(app, needle)
        column += shift
        app.handle_bytes(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())

    def type(self, app, text):
        for char in text:
            app.handle_key(char)


class LaneTests(LayoutFixture):
    def test_three_lanes_until_a_task_is_blocked_then_blocked_has_its_own_lane(self):
        # Break: Blocked is folded into Waiting Review, or an empty Blocked lane crowds the
        # three-column board.
        app = self.board()
        header = next(line for line in self.lines(app) if "TODO 2" in line)
        self.assertNotIn("Blocked", self.text(app))
        for label in ("Working 2", "Waiting Review 2"):
            self.assertIn(label, header)
        for _ in range(3):
            app.handle_key("right")
        self.assertEqual(app.board_column, "done")

        self.tasks.append(task("fp-blk-9999", "Waiting on credentials", "blocked", "alter"))
        app.handle_key("r")
        header = next(line for line in self.lines(app) if "TODO 2" in line)
        self.assertIn("Blocked 1", header)
        self.assertEqual(header.count("\u2502"), 3)
        row, column = self.find(app, "Waiting on credentials")
        self.assertGreater(column - 1, header.rindex("\u2502"))
        self.assertNotIn("Waiting Review 3", self.text(app))
        app.handle_key("left")
        self.assertEqual((app.board_column, app.board_selected), ("blocked", "fp-blk-9999"))
        self.assertIn("Blocked \u00b7 ship", self.text(app))

    def test_card_says_only_what_the_projection_and_a_read_workspace_say(self):
        # Break: a card invents a PR, checks or a file count, or hides a missing agent.
        app = self.board()
        frame = self.text(app)
        for ident in ("fp-keys-ab12", "fp-side-ef56", "fp-sum-2c3d"):
            self.assertIn(ident, frame)
        self.assertIn("No agent assigned", frame)
        self.assertIn("herdr-project-sidebar", frame)
        self.assertIn("editing", frame)
        self.assertIn("Awaiting reply", frame)
        self.assertIn("1 awaiting reply", frame)
        self.assertIn("until later: parked", frame)
        for invented in ("PR #", "Checks", "checks", "files changed", "file changed", "\u2197"):
            self.assertNotIn(invented, frame)

    def test_a_card_with_a_live_agent_shows_changed_files_and_go_to_agent_only_there(self):
        # Break: Go to agent is offered for a task with no live pane, the file count is guessed
        # before the workspace was read, or the click lands outside the control.
        app = self.board()
        write_meta(
            self.home, "fp-side-ef56", project="/work/dotfiles", herdr_session="lab", herdr_workspace_id="w1",
            herdr_tab_id="w1:t1", herdr_pane_id="w1:p1", endpoint_task_id="fp-side-ef56",
        )
        row = {"task_id": "fp-side-ef56", "pane_id": "w1:p1", "terminal_id": "term_a", "workspace_id": "w1",
               "tab_id": "w1:t1", "focusable": True, "kind": "agent", "key": ("agent", "w1:p1")}
        app.projects = [{"key": "dotfiles", "agents": [row]}]
        app.session = "lab"
        self.transport.pane_get["w1:p1"] = pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1")
        app.handle_key("right")
        self.assertEqual(app.board_selected, "fp-side-ef56")
        unread = self.text(app)
        self.assertIn("herdr-project-sidebar \u2197", unread)
        self.assertNotIn("files changed", unread)
        app.agent_ws[("agent", "w1:p1")] = {
            "workspace": SimpleNamespace(path="/work/dotfiles"),
            "status": SimpleNamespace(changed=3, head="fm/sidebar", oid="abc1234", detached=False),
        }
        read = self.text(app)
        self.assertIn("3 files changed", read)
        self.assertIn("Go to agent \u2197", read)
        self.assertIn("dotfiles / herdr-project-sidebar \u00b7 fm/sidebar", read)
        self.click(app, "fp-side-ef56 \u00b7 Herdr right panel")
        self.assertEqual([call for call in self.transport.socket_calls if call["method"] == "pane.focus"], [])
        self.click(app, "Go to agent")
        self.assertEqual([call["params"]["pane_id"] for call in self.transport.socket_calls if call["method"] == "pane.focus"], ["w1:p1"])

    def test_an_assigned_agent_that_is_not_open_is_not_called_unassigned(self):
        # Break: a task with an agent name but no live pane reads "No agent assigned".
        app = self.board()
        app.handle_key("right")
        self.assertIn("Agent not open", self.text(app))
        app.handle_key("g")
        self.assertEqual(app.status, "agent not open in Herdr")
        self.assertFalse(any(call["method"] == "pane.focus" for call in self.transport.socket_calls))

    def test_hostile_task_text_cannot_reach_the_terminal(self):
        # Break: an escape sequence in a title or agent name is drawn raw on a card or chip.
        self.tasks[0]["title"] = "bad\x1b]0;owned\x07title"
        self.tasks[0]["agent"] = "ag\x1b[31ment"
        app = self.board()
        frame = app.render()
        self.assertNotIn("\x1b]", frame)
        self.assertNotIn("owned", frame)
        self.assertNotIn("\x1b[31m", frame)
        self.assertIn("badtitle", self.text(app))


class NavigationTests(LayoutFixture):
    def test_columns_tasks_and_clicks_select_by_task_id_and_survive_a_refresh(self):
        # Break: selection follows list position, so a reordered refresh moves it to another task.
        app = self.board()
        self.lines(app)
        self.assertEqual((app.board_column, app.board_selected), ("todo", "fp-keys-ab12"))
        app.handle_key("down")
        self.assertEqual(app.board_selected, "fp-term-cd34")
        app.handle_key("right")
        self.assertEqual((app.board_column, app.board_selected), ("in_progress", "fp-sync-0a1b"))
        app.handle_key("up")
        self.assertEqual(app.board_selected, "fp-side-ef56")
        self.click(app, "Takeover review")
        self.assertEqual((app.board_column, app.board_selected), ("waiting_review", "fp-take-4e5f"))
        self.tasks.reverse()
        app.handle_key("r")
        self.assertEqual(app.board_selected, "fp-take-4e5f")
        self.assertIn("fp-take-4e5f \u00b7 Takeover review", self.text(app))
        bar = [line for line in self.lines(app) if "\u258c" in line]
        self.assertTrue(any("Takeover review" in line for line in bar), bar)

    def test_selected_card_has_an_accent_fill_and_a_bar_that_survives_no_color(self):
        # Break: the selection is only a color, so NO_COLOR shows no selected card.
        app = self.board()
        colored = app.render()
        self.assertIn("48;2;27;44;82", colored)
        self.assertIn("\u258c", strip_sgr(colored))
        plain = self.board(env={"NO_COLOR": "1"})
        frame = plain.render()
        self.assertNotIn("\x1b", frame)
        row = next(line for line in frame.splitlines() if "Keyboard shortcuts" in line)
        self.assertLess(row.index("\u258c"), row.index("Keyboard"))
        other = next(line for line in frame.splitlines() if "Herdr right panel" in line)
        self.assertNotIn("\u258c", other[other.index("\u2502"):])

    def test_an_overflowing_lane_keeps_the_selected_card_in_view_and_counts_the_rest(self):
        # Break: cards past the bottom of a lane are cut with the selection inside the cut.
        for number in range(8):
            self.tasks.append(task(f"fp-many-{number:04d}", f"Queued item {number}", "todo"))
        app = self.board(rows=24)
        self.assertIn("\u25bc", self.text(app))
        for _ in range(9):
            app.handle_key("down")
        frame = self.text(app)
        self.assertEqual(app.board_selected, "fp-many-0007")
        self.assertIn("Queued item 7", frame)
        self.assertIn("\u25b2", frame)
        for rows, cols in ((24, 120), (18, 100), (30, 80), (12, 60), (40, 160)):
            app.rows, app.cols = rows, cols
            frame = self.text(app)
            self.assertIn("Queued item 7", frame, (rows, cols))
        app.handle_key("up")
        self.assertEqual(app.board_selected, "fp-many-0006")
        self.assertIn("Queued item 6", self.text(app))

    def test_every_pane_size_draws_exactly_its_rows_inside_its_columns(self):
        # Break: a size drops or overflows a row, or leaves the frame shorter than the pane.
        app = self.board()
        for rows, cols in ((6, 30), (8, 40), (10, 60), (14, 72), (18, 100), (24, 80), (32, 120), (36, 99), (36, 100), (50, 200)):
            app.rows, app.cols = rows, cols
            lines = self.lines(app)
            self.assertEqual(len(lines), rows, (rows, cols))
            self.assertTrue(all(width(line) <= cols for line in lines), (rows, cols))
            self.assertEqual(len(app.hit_rows), rows, (rows, cols))
        app.handle_key("1")
        app.handle_key("3")
        self.assertEqual(app.tab, "board")

    def test_narrow_board_names_every_lane_and_a_click_on_one_switches_to_it(self):
        # Break: a narrow pane hides the other lanes' counts or cannot reach them by pointer.
        app = self.board(rows=30, cols=80)
        strip = next(line for line in self.lines(app) if "TODO 2" in line)
        for count in ("\u25cf 2", "\u25c9 2", "\u2713 1"):
            self.assertIn(count, strip)
        self.assertNotIn("Waiting Review", strip)
        self.click(app, "\u25c9 2")
        self.assertEqual(app.board_column, "waiting_review")
        self.assertIn("Waiting Review 2", self.text(app))
        self.assertIn("Project status summary", self.text(app))


class FilterAndDoneTests(LayoutFixture):
    def check_filters(self, rows):
        app = self.board(rows=rows)
        self.assertIn("7 tasks \u00b7 3 projects", self.text(app))
        self.assertEqual("\u256d" in self.lines(app)[1], rows >= 36)
        self.click(app, "All projects")
        self.assertEqual(app.board_project, "dotfiles")
        self.assertIn("4 tasks \u00b7 1 project", self.text(app))
        self.assertNotIn("Terminal selection transfer", self.text(app))
        self.click(app, "Search tasks")
        self.assertEqual(app.focus, "search")
        self.type(app, "sidebar")
        self.assertIn("1 task \u00b7 1 project", self.text(app))
        self.assertIn("sidebar", self.lines(app)[1 if rows < 36 else 2])
        app.handle_key("enter")
        self.assertEqual(app.focus, "list")
        self.click(app, "All tasks")
        self.assertEqual(app.board_active, "active")
        self.assertIn("Active tasks", self.text(app))
        app.handle_key("esc")
        self.assertEqual(app.board_search.text, "")

    def test_project_search_and_active_filters_work_in_the_one_line_filter_row(self):
        # Break: a click in the compact row cycles the wrong filter, or a filter only changes cards.
        self.check_filters(24)

    def test_project_search_and_active_filters_work_in_the_boxed_filter_row(self):
        # Break: the three boxes' click regions drift from what they draw.
        self.check_filters(40)

    def test_a_selected_task_the_filters_hide_is_still_the_message_context_and_says_so(self):
        # Break: a filter silently swaps the message context, or the detail shows a task the
        # lanes do not without saying why.
        app = self.board()
        self.lines(app)
        self.click(app, "Search tasks")
        self.type(app, "sidebar")
        frame = self.text(app)
        self.assertNotIn("fp-keys-ab12  Keyboard", frame)
        self.assertEqual(app.board_selected, "fp-keys-ab12")
        self.assertIn("hidden by filters", frame)
        self.assertIn("fp-keys-ab12 \u00b7 Keyboard shortcuts", frame)

    def test_done_strip_is_collapsed_counted_and_opens_to_one_line_cards(self):
        # Break: Done cards crowd the lanes when opened, or the strip hides its count.
        app = self.board()
        self.assertIn("\u25b8 Done (1)", self.text(app))
        self.assertNotIn("Landed row", self.text(app))
        self.click(app, "Done (1)")
        self.assertTrue(app.board_done_open)
        frame = self.text(app)
        self.assertIn("\u25be Done (1)", frame)
        self.assertIn("fp-old-0001", frame)
        self.assertIn("Landed row", frame)
        self.assertEqual(sum("Landed row" in line for line in self.lines(app)), 1)
        self.assertIn("Keyboard shortcuts", frame)
        app.handle_key("f")
        self.assertIn("\u25be Done (0)", self.text(app))
        self.assertNotIn("Landed row", self.text(app))


class MessageBlockTests(LayoutFixture):
    def block(self, app):
        lines = self.lines(app)
        label = next(index for index, line in enumerate(lines) if "Write to FirstMate" in line)
        return label, lines

    def test_detail_and_message_block_are_fixed_at_the_bottom_while_selection_moves(self):
        # Break: the message block rides with the cards, so selecting another task moves it.
        app = self.board()
        label, lines = self.block(app)
        self.assertIn("Selected task context", lines[label])
        self.assertIn("fp-keys-ab12 \u00b7 Keyboard shortcuts", lines[label + 1])
        self.assertIn("Write about the selected task", lines[label + 2])
        self.assertIn("Send \u21b5", lines[label + 2])
        self.assertIn("press Enter there", lines[label + 3])
        self.assertIn("Column", lines[label + 4])
        self.assertEqual(len(lines), label + 5)
        self.assertIn("fp-keys-ab12 \u00b7 Keyboard shortcuts", lines[label - 3])
        app.handle_key("right")
        app.handle_key("down")
        again, lines = self.block(app)
        self.assertEqual(again, label)
        self.assertIn("fp-sync-0a1b \u00b7 Workspace alignment", lines[again + 1])

    def test_enter_adds_the_projections_reasons_to_the_detail_without_moving_the_message_block(self):
        # Break: the detail expansion pushes the message block off the pane.
        app = self.board()
        label, _ = self.block(app)
        app.handle_key("enter")
        again, lines = self.block(app)
        self.assertEqual(len(lines), 36)
        self.assertEqual(again, label)
        self.assertTrue(any(line.strip().startswith("Reason") and "until later: parked" in line for line in lines))

    def test_draft_and_chip_survive_selection_refresh_and_the_close_button(self):
        # Break: a refresh, another selection or closing the editor loses the unsent text.
        app = self.board()
        app.handle_key("tab")
        self.type(app, "look at this")
        self.assertEqual(app.focus, "composer")
        self.assertIn("fp-keys-ab12 \u00b7 Keyboard shortcuts \u00d7", self.text(app))
        app.refresh(manual=True)
        self.assertEqual(app.composer.text, "look at this")
        self.assertIn("look at this", self.text(app))
        self.click(app, "\u00d7")
        self.assertEqual(app.focus, "list")
        self.assertNotIn("\u00d7", self.text(app))
        self.assertIn("look at this", self.text(app))
        app.handle_key("right")
        self.assertNotIn("look at this", self.text(app))
        app.handle_key("left")
        self.assertEqual(app.board_selected, "fp-keys-ab12")
        self.assertIn("look at this", self.text(app))
        self.assertEqual(self.writers(), [])

    def test_send_refuses_an_empty_message_and_a_missing_firstmate_and_keeps_the_draft(self):
        # Break: Send arms an empty preview, or a refusal drops the typed text.
        app = self.board()
        self.click(app, "Send \u21b5")
        self.assertEqual(app.status, "nothing to send: write the message first")
        self.assertEqual(app.screen, "tree")
        self.type(app, "ready")
        self.click(app, "Send \u21b5")
        self.assertEqual(app.screen, "tree")
        self.assertIn("not transferred", app.status)
        self.assertEqual(app.composer.text, "ready")
        self.assertEqual(self.writers(), [])

    def test_send_previews_then_puts_the_message_unsent_in_the_primarys_box_and_cancel_keeps_it(self):
        # Break: Send skips the preview, submits for the captain, or a cancel loses the draft.
        primary = FakePi(self.transport, self.home)
        app = self.board()
        app.handle_key("tab")
        self.type(app, "ready")
        self.click(app, "Send \u21b5")
        self.assertEqual(app.screen, "confirm-send")
        self.assertIn("board task: fp-keys-ab12", app.pending["body"])
        self.assertEqual(primary.received, [])
        app.handle_key("n")
        self.assertEqual(app.screen, "tree")
        self.assertEqual(primary.received, [])
        self.assertEqual(app.composer.text, "ready")
        self.click(app, "Send \u21b5")
        app.handle_key("y")
        self.assertEqual(len(primary.received), 1)
        self.assertIn("ready", primary.received[0])
        self.assertEqual(primary.submitted, [])
        self.assertEqual(app.focus, "list")
        self.assertEqual(self.writers(), [])

    def refused_send(self, **kwargs):
        primary = FakePi(self.transport, self.home, **kwargs)
        app = self.board()
        app.handle_key("tab")
        self.type(app, "ready")
        self.click(app, "Send \u21b5")
        self.assertEqual(app.screen, "tree")
        self.assertIn("not transferred", app.status)
        self.assertEqual(primary.received, [])
        self.assertEqual(app.composer.text, "ready")

    def test_a_busy_firstmate_refuses_and_nothing_is_pasted(self):
        # Break: the message is pasted into an agent that is mid-turn.
        self.refused_send(status="working")

    def test_an_occupied_message_box_refuses_and_nothing_is_pasted(self):
        # Break: the message is pasted over text the captain was typing in FirstMate.
        self.refused_send(text="half a thought")

    def test_no_new_task_control_exists_and_the_message_block_degrades_in_a_short_pane(self):
        # Break: a short pane drops the field the status messages explain, or a new-task control appears.
        app = self.board(rows=14)
        frame = self.text(app)
        self.assertNotIn("New task", frame)
        self.assertNotIn("Task actions", frame)
        self.assertIn("Send", frame)
        self.assertIn("Keyboard shortcuts", frame)
        app.rows = 9
        self.assertIn("Write about the selected task", self.text(app))


class PointerTests(LayoutFixture):
    def test_a_click_on_a_lane_header_goes_to_the_lane_and_never_drops_the_selection(self):
        # Break: a click on lane chrome (header, rule) clears the selection and with it the message context.
        app = self.board()
        self.lines(app)
        row, column = self.find(app, "Waiting Review 2")
        app.handle_bytes(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())
        self.assertEqual((app.board_column, app.board_selected), ("waiting_review", "fp-sum-2c3d"))
        app.handle_key("down")
        self.assertEqual(app.board_selected, "fp-take-4e5f")
        app.handle_bytes(f"\x1b[<0;{column};{row + 1}M\x1b[<0;{column};{row + 1}m".encode())
        self.assertEqual(app.board_selected, "fp-take-4e5f")
        self.assertIn("fp-take-4e5f \u00b7 Takeover review", self.text(app))


class UnconfirmedTransferTests(LayoutFixture):
    def test_an_unconfirmed_transfer_keeps_the_draft_and_the_context_for_a_retry(self):
        # Break: a paste nobody saw land is reported as sent and the draft is dropped.
        primary = FakePi(self.transport, self.home)
        primary.render = False
        app = self.board()
        app.handle_key("tab")
        self.type(app, "ready")
        self.click(app, "Send \u21b5")
        self.assertEqual(app.screen, "confirm-send")
        app.handle_key("y")
        self.assertTrue(app.status.startswith("transfer unconfirmed"), app.status)
        self.assertEqual(app.screen, "tree")
        self.assertEqual(app.composer.text, "ready")
        self.assertEqual(app.draft["outcome"], "unconfirmed")
        self.assertIn("fp-keys-ab12 \u00b7 Keyboard shortcuts \u00d7", self.text(app))
        self.assertIn("last send: unconfirmed", self.text(app))
