"""Agents list: one blank, inert row separates neighbouring two-line agents of a project.
Each test names the production break it exists to catch."""

from tests.test_agent_identity import IdentityFixture


class AgentSpacingTests(IdentityFixture):
    def crowd(self):
        """Primary holds alpha, beta, gamma and epsilon; zeta holds delta. The list shows
        them as Alpha, Beta, Epsilon, Gamma under primary, then Delta under zeta."""
        zeta = self.fx.base / "zeta"
        zeta.mkdir()
        self.add_agent("gamma-task", 3, "claude", harness="claude")
        self.add_agent("delta-task", 4, "claude", harness="claude", project=str(zeta))
        self.add_agent("epsilon-task", 5, "claude", harness="claude")

    def tree(self, app):
        """(hit kind or None, text) of every screen line from the first project heading down."""
        lines = self.lines(app)
        first = next(i for i, hit in enumerate(app.hit_rows) if hit and hit["kind"] == "project")
        return [((hit or {}).get("kind"), lines[i].rstrip()) for i, hit in enumerate(app.hit_rows) if i >= first]

    def test_one_blank_row_separates_agents_and_groups_keep_their_single_gap(self):
        # Break: neighbouring agents touch, the gap doubles up before the next project
        # heading, or a blank row appears between a heading and its first agent.
        self.crowd()
        app = self.app(rows=40, cols=100)
        shown = self.tree(app)[:16]
        self.assertEqual(
            [kind for kind, _ in shown],
            ["project", "agent", "agent", None, "agent", "agent", None, "agent", "agent", None, "agent", "agent", None, "project", "agent", "agent"],
        )
        self.assertEqual([text for kind, text in shown if kind is None], ["", "", "", ""])
        self.assertEqual([text.lstrip("\u258c ").split()[1] for kind, text in shown if kind == "agent"][::2], ["Alpha", "Beta", "Epsilon", "Gamma", "Delta"])

    def test_a_click_reaches_either_line_of_each_agent_and_the_gap_selects_nothing(self):
        # Break: the second line of an agent below a gap selects its neighbour, or the gap row
        # is a hit target for the agent above or below it.
        self.crowd()
        app = self.app(rows=40, cols=100)
        lines = self.lines(app)
        for title, task in (("Gamma task", "gamma-task"), ("Beta task", "beta-task"), ("Epsilon task", "epsilon-task"), ("Alpha task", "alpha-task")):
            at = next(i for i, line in enumerate(lines) if title in line)
            for offset in (0, 1):
                app.target = None
                self.click(app, at + 1 + offset)
                self.assertEqual(app.target["label"], task, f"{title} line {offset + 1}")
        app.target = None
        hits = app.hit_rows
        gaps = [i + 1 for i in range(1, len(hits) - 1) if hits[i] is None and hits[i - 1] and hits[i - 1]["kind"] == "agent" and hits[i + 1] and hits[i + 1]["kind"] == "agent"]
        self.assertEqual(len(gaps), 3)
        for row in gaps:
            self.click(app, row)
        self.assertIsNone(app.target)

    def test_keys_walk_every_row_and_a_short_pane_scrolls_without_a_gap_at_the_window_edge(self):
        # Break: the window cuts the selected agent's second line, opens on a blank row under
        # the heading it keeps on top, or loses that heading once the list scrolls past it.
        self.crowd()
        app = self.app(rows=14, cols=40)
        walked = []
        for _ in range(7):
            lines = self.lines(app)
            at = next(i for i, line in enumerate(lines) if line.startswith("\u258c"))
            walked.append(lines[at].strip("\u258c \u25cf").split("  ")[0])
            if "task" in lines[at]:
                self.assertIn("Claude", lines[at + 1], "second line of the selected agent is cut off")
            window = lines[3:11]
            self.assertTrue(window[0].strip(), "the window opens on a blank row:\n" + "\n".join(lines))
            for above, below in zip(window, window[1:]):
                self.assertFalse("Claude" in above and "task" in below, "agents touch:\n" + "\n".join(lines))
            if "Gamma" in walked[-1]:
                self.assertIn("\u25be primary", window[0], "the project heading is not kept on top")
            app.handle_key("j")
        self.assertEqual(walked, ["Alpha task", "Beta task", "Epsilon task", "Gamma task", "\u25be zeta", "Delta task", "Delta task"])
        for _ in range(5):
            app.handle_key("k")
        self.assertEqual(self.lines(app)[4][:2], "\u258c\u25cf")
        self.assertIn("Alpha task", self.lines(app)[4])

    def test_a_scrolling_list_keeps_the_agents_above_the_selection_in_view(self):
        # Break: spacers count as part of the adjacent agent's row, so the window opens on the
        # selected agent and drops the agents above it.
        for number, name in enumerate(("gamma", "epsilon", "eta", "theta"), start=3):
            self.add_agent(f"{name}-task", number, "claude", harness="claude")
        app = self.app(rows=14, cols=40)
        for _ in range(6):
            app.handle_key("j")
        lines = self.lines(app)
        self.assertIn("\u25be primary", lines[3])
        self.assertIn("Gamma task", lines[4], "\n".join(lines))
        self.assertTrue(lines[6].strip() == "" and "Theta task" in lines[7], "\n".join(lines))
        self.assertTrue(lines[7].startswith("\u258c"))
