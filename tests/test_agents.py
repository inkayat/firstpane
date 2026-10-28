"""The Agents tab: search and status filter, grouped two-line agents, the pinned context and
the FirstMate message field. Each test names the production break it exists to catch."""

from tests.support import agent_for, selected_line
from tests.test_panel import PanelFixture, plain


class AgentsTabTests(PanelFixture):
    def frame(self, app):
        return plain(app.render())

    def input_row(self, app):
        lines = self.lines(app)
        return next(index for index, hit in enumerate(app.hit_rows, start=1) if hit and hit["kind"] == "composer" and "\u2502" in lines[index - 1])

    def count_line(self, app):
        return next(line for line in self.lines(app) if " agent" in line and "project" in line)

    def publish_status(self, **statuses):
        for task, status in statuses.items():
            self.panes[task]["agent_status"] = status
        self.publish()
        self.clock.t += 5

    # ---- message field ---------------------------------------------------------------
    def test_message_to_the_cursor_agent_is_previewed_then_put_in_firstmates_box(self):
        # Break: the field writes about the agent selected earlier instead of the one under
        # the cursor, fills FirstMate's box without the exact preview, goes through another
        # path (inbox) or submits, or keeps the text after it was put in the box.
        app = self.app()
        app.handle_key("j")
        self.assertIn("Beta task", selected_line(app.render()))
        app.handle_key("tab")
        self.type(app, "Merhaba ş")
        self.assertIn("Merhaba ş", self.frame(app))
        self.assertEqual(self.main.received, [])
        app.handle_key("enter")
        preview = self.frame(app)
        self.assertIn("Put in FirstMate's message box (pane w5:p1, pi)", preview)
        self.assertIn("agent: beta-task", preview)
        self.assertIn("Merhaba ş", preview)
        self.assertEqual(self.main.received, [])
        app.handle_key("y")
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(len(self.main.received), 1)
        self.assertEqual(self.main.submitted, [])
        self.assertIn("agent: beta-task", self.pasted()[0])
        self.assertIn("Merhaba ş", self.pasted()[0])
        frame = self.frame(app)
        self.assertIn("press Enter there to send", frame)
        self.assertNotIn("Merhaba", frame)
        self.assertEqual(app.focus, "list")

    def test_the_message_field_needs_an_agent_under_the_cursor(self):
        # Break: on a project row the field opens against a stale or missing agent and a
        # message goes out without a subject.
        app = self.app()
        app.handle_key("k")
        app.handle_key("tab")
        self.assertEqual(app.focus, "list")
        self.assertIn("select an agent", self.frame(app))
        self.type(app, "x")
        self.assertEqual(list((self.state / "drafts").glob("open-*.json")), [])

    def test_an_unsent_message_stays_with_its_agent_when_the_cursor_moves(self):
        # Break: moving to another agent shows or sends the first agent's text, or Esc
        # discards it.
        app = self.app()
        app.handle_key("tab")
        self.type(app, "half a thought")
        app.handle_key("esc")
        self.assertEqual(app.focus, "list")
        self.assertIn("half a thought", self.lines(app)[self.input_row(app) - 1])
        app.handle_key("j")
        row = self.lines(app)[self.input_row(app) - 1]
        self.assertNotIn("half a thought", row)
        self.assertIn("Write something about the selected agent", row)
        app.handle_key("k")
        self.assertIn("half a thought", self.lines(app)[self.input_row(app) - 1])
        self.assertEqual(self.transport.inbox, [])

    def test_clicks_reach_the_field_and_the_send_button_and_a_list_click_leaves_it(self):
        # Break: the field is keyboard-only, the send button does nothing (or sends without
        # the preview), or a click on the list keeps typing into the field.
        app = self.app()
        row = self.input_row(app)
        self.click(app, row, col=10)
        self.assertEqual(app.focus, "composer")
        self.type(app, "from a click")
        self.click(app, row, col=app.cols - 3)
        self.assertIn("Put in FirstMate's message box", self.frame(app))
        self.assertEqual(self.main.received, [])
        app.handle_key("n")
        self.click(app, self.row_of(app, "Beta task"))
        self.assertEqual(app.focus, "list")
        self.assertEqual(app.draft, None)
        self.assertIn("beta-task", app.target["label"])
        self.type(app, "x")
        self.assertNotIn("from a click x", self.frame(app))
        app.handle_key("k")
        self.assertIn("from a click", self.frame(app))

    def test_the_footer_names_the_keys_that_work_in_the_current_focus(self):
        # Break: the hint line keeps advertising list keys while typing, drops them entirely,
        # or on an agent row still says Enter is needed when moving onto it selects it.
        app = self.app()
        footer = self.lines(app)[-1]
        self.assertIn("\u2191\u2193 Select", footer)
        self.assertIn("g Focus", footer)
        self.assertNotIn("Enter", footer)
        app.handle_key("tab")
        self.assertIn("Enter Preview and send", self.lines(app)[-1])
        app.handle_key("esc")
        app.handle_key("/")
        self.assertIn("Esc Clear", self.lines(app)[-1])
        app.handle_key("esc")
        app.handle_key("k")
        self.assertIn("Enter Fold", self.lines(app)[-1])

    # ---- search and filter -----------------------------------------------------------
    def test_search_matches_title_branch_and_project_and_esc_restores_the_list(self):
        # Break: search only sees the visible title, hides a project whose name matches, or
        # leaves the query after Esc.
        app = self.app()
        app.handle_key("/")
        self.type(app, "fm/beta")
        frame = self.frame(app)
        self.assertIn("Beta task", frame)
        self.assertNotIn("Alpha task", frame)
        self.assertIn("1 project \u00b7 1 agent", frame)
        app.handle_key("enter")
        self.assertEqual(app.focus, "list")
        self.assertNotIn("Alpha task", self.frame(app))
        app.handle_key("esc")
        frame = self.frame(app)
        self.assertIn("Alpha task", frame)
        self.assertIn("1 project \u00b7 2 agents", frame)
        app.handle_key("/")
        self.type(app, "primary")
        self.assertIn("Alpha task", self.frame(app))
        self.assertIn("Beta task", self.frame(app))

    def test_status_filter_cycles_with_the_key_and_a_click_and_says_when_nothing_matches(self):
        # Break: the filter only hides rows without updating counts, cannot be clicked, or
        # leaves an empty pane with no explanation.
        app = self.app()
        app.handle_key("f")
        frame = self.frame(app)
        self.assertIn("Working \u25be", frame)
        self.assertIn("Beta task", frame)
        self.assertNotIn("Alpha task", frame)
        self.assertIn("1 project \u00b7 1 agent", frame)
        row = next(index for index, line in enumerate(self.lines(app), start=1) if "Working \u25be" in line)
        column = self.lines(app)[row - 1].index("Working") + 2
        self.click(app, row, col=column)
        self.assertIn("Waiting \u25be", self.frame(app))
        self.assertIn("No matching agents", self.frame(app))
        self.click(app, row, col=column)
        self.assertIn("Idle \u25be", self.frame(app))
        self.assertIn("Alpha task", self.frame(app))
        self.assertNotIn("Beta task", self.frame(app))
        app.handle_key("esc")
        self.assertIn("All \u25be", self.frame(app))
        self.assertIn("Beta task", self.frame(app))

    def test_clicking_the_search_field_focuses_it_and_typing_filters(self):
        # Break: the search field is drawn but not clickable.
        app = self.app()
        row = next(index for index, line in enumerate(self.lines(app), start=1) if "Search projects or agents" in line)
        self.click(app, row, col=12)
        self.assertEqual(app.focus, "search")
        self.type(app, "alp")
        self.assertNotIn("Beta task", self.frame(app))
        self.assertIn("Alpha task", self.frame(app))

    # ---- rows ------------------------------------------------------------------------
    def test_waiting_agents_are_worded_ringed_and_counted(self):
        # Break: a blocked agent reads as just another idle one, or the waiting count is
        # missing or counts other states.
        self.publish_status(**{"beta-task": "blocked"})
        app = self.app()
        frame = self.frame(app)
        beta = next(line for line in frame.splitlines() if "Beta task" in line)
        self.assertIn("\u25cb", beta)
        self.assertRegex(beta, r"Waiting for response \u00b7 clean\s+\u203a")
        self.assertIn("\u25cf 1 waiting for response", frame)
        self.publish_status(**{"alpha-task": "blocked"})
        app.refresh(manual=True)
        self.assertIn("\u25cf 2 waiting for response", self.frame(app))

    def test_herdr_title_is_the_row_title_and_hostile_text_is_inert(self):
        # Break: the title Herdr reports is ignored for a guessed one, or its escape
        # sequences reach the screen.
        rows = list(self.panes.values())
        agents = [agent_for(row) for row in rows]
        agents[0]["title"] = "Review \x1b]0;PWN\x07the sidebar"
        self.transport.snapshot["agents"] = agents
        app = self.app()
        frame = app.render()
        self.assertIn("Review the sidebar", plain(frame))
        self.assertNotIn("PWN", frame)
        self.assertNotIn("\x1b]", frame)

    def test_a_narrow_pane_keeps_the_status_word_and_never_overflows(self):
        # Break: at 30-40 columns the status moves off screen or a row is cut mid-escape.
        self.publish_status(**{"beta-task": "blocked"})
        for rows, cols in ((30, 100), (24, 56), (16, 40), (12, 30)):
            app = self.app(rows=rows, cols=cols)
            lines = self.lines(app)
            self.assertEqual(len(lines), rows, (rows, cols))
            self.assertTrue(all(len(line) <= cols for line in lines), (rows, cols))
            self.assertTrue(any("Waiting" in line for line in lines), (rows, cols))
            self.assertTrue(any("Idle" in line for line in lines), (rows, cols))

    def test_without_color_the_selection_and_status_are_still_readable(self):
        # Break: NO_COLOR drops the only selection cue or leaves escape codes in the frame.
        app = self.app(env={"NO_COLOR": "1"})
        frame = app.render()
        self.assertNotIn("\x1b", frame)
        self.assertTrue(selected_line(frame).startswith("\u258c"))
        self.assertIn("Idle", frame)
        self.assertIn("Working", frame)

    def test_the_context_line_keeps_the_branch_when_the_names_do_not_fit(self):
        # Break: a narrow pane blanks the selected agent's branch, leaving it shown nowhere.
        wide = next(line for line in self.lines(self.app(rows=24, cols=60)) if line.startswith(" primary / "))
        self.assertTrue(wide.rstrip().endswith("fm/alpha-task"), wide)
        mid = self.lines(self.app(rows=24, cols=30))
        self.assertIn(" fm/alpha-task", [line.rstrip() for line in mid])
        self.assertFalse(any(line.startswith(" primary / ") for line in mid))
        tight = self.lines(self.app(rows=24, cols=12))
        line = next(line for line in tight if line.startswith(" fm/"))
        self.assertTrue(line.rstrip().endswith("\u2026") and len(line) <= 12, line)

    def test_no_agent_line_sits_over_the_heading_and_the_list_gets_that_row(self):
        # Break: the project / agent / branch line is still drawn above "Write to FirstMate", the
        # freed row is left blank so the list is no taller, the heading goes with the line, or the
        # pane height that switches to this layout moves. (The selected agent and the message
        # target are untouched: the message tests above send to the agent under the cursor.)
        for rows, cols in ((30, 100), (25, 60), (30, 30)):
            lines = self.lines(self.app(rows=rows, cols=cols))
            heading = next(index for index, line in enumerate(lines) if "Write to FirstMate" in line)
            self.assertEqual(heading, rows - 5, (rows, cols))
            self.assertEqual(lines[heading - 1].strip("\u2500"), "", (rows, cols))
            self.assertFalse([line for line in lines if "fm/alpha-task" in line or "primary / " in line], (rows, cols))
            self.assertEqual(lines[heading + 1].count("\u256d"), 1, (rows, cols))
