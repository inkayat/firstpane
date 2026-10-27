"""Settings tab: the one Browser placement preference, chosen by keys or a click and kept.

Herdr is faked at the socket boundary and Git runs against fixture repositories (tests.test_panel
fixture). No browser is configured here, so choosing a placement never opens one.
"""

import json
import re
import unittest

from tests.test_panel import PanelFixture, plain

GHOSTTY = "Ghostty tab"
INSIDE = "Inside Firstpane"


class SettingsTabTests(PanelFixture):
    def choice_line(self, app, label):
        return next(line for line in self.lines(app) if label in line)

    def saved(self):
        return json.loads((self.state / "ui-state.json").read_text()).get("browser_placement")

    def test_six_opens_settings_with_the_browser_placement_choice_and_the_safe_default(self):
        # Break: there is no Settings tab, the choice is unlabelled, both options look alike
        # without color, or the initial placement is not the Ghostty tab.
        app = self.app(env={**self.fx.env, "NO_COLOR": "1"})
        app.handle_key("6")
        frame = plain(app.render())
        self.assertIn("Settings", frame.split("\n")[0])
        self.assertIn("Browser placement", frame)
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(app, GHOSTTY))
        self.assertIn("( ) " + INSIDE, self.choice_line(app, INSIDE))
        self.assertNotIn("\x1b[", app.render())

    def test_keys_choose_inside_firstpane_and_the_choice_survives_a_restart(self):
        # Break: j/k and Enter do not move or choose, the choice is not written, or a restarted
        # Firstpane is back at the default.
        app = self.app()
        self.keys(app, "6", "j", "enter")
        self.assertIn("(\u2022) " + INSIDE, self.choice_line(app, INSIDE))
        self.assertIn("( ) " + GHOSTTY, self.choice_line(app, GHOSTTY))
        self.assertIn("saved", plain(app.render()))
        self.assertEqual(self.saved(), "embedded")
        again = self.app()
        self.assertEqual(again.tab, "settings")
        self.assertIn("(\u2022) " + INSIDE, self.choice_line(again, INSIDE))
        self.keys(again, "k", " ")
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(again, GHOSTTY))
        self.assertEqual(self.saved(), "ghostty")

    def test_a_click_on_a_choice_chooses_it_and_a_click_on_the_tab_opens_settings(self):
        # Break: the Settings tab label or a choice row ignores the mouse.
        app = self.app()
        self.click_text(app, "Settings")
        self.assertEqual(app.tab, "settings")
        self.click_text(app, INSIDE)
        self.assertEqual(self.saved(), "embedded")
        self.click_text(app, GHOSTTY)
        self.assertEqual(self.saved(), "ghostty")

    def test_older_saved_state_still_loads_and_an_unknown_placement_falls_back_to_ghostty(self):
        # Break: ui-state.json written before this setting existed (or holding a value this
        # version does not offer) breaks loading, drops the saved tab, or picks Inside Firstpane.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "changes", "folded_projects": []}))
        app = self.app()
        self.assertEqual(app.tab, "changes")
        app.handle_key("6")
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(app, GHOSTTY))
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "settings", "browser_placement": "in-tab"}))
        again = self.app()
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(again, GHOSTTY))

    def test_a_saved_beside_firstpane_choice_now_opens_inside_firstpane(self):
        # Break: after the upgrade a user who chose the Herdr-side browser (the only choice that
        # works off macOS) silently lands on the Ghostty tab default.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "settings", "browser_placement": "beside"}))
        app = self.app()
        self.assertIn("(\u2022) " + INSIDE, self.choice_line(app, INSIDE))
        app.handle_key("1")
        self.assertEqual(self.saved(), "embedded")

    def test_a_narrow_pane_keeps_both_choices_readable_within_its_width(self):
        # Break: in a side pane the choice labels are cut off or lines run past the pane edge.
        app = self.app(rows=20, cols=44)
        app.handle_key("6")
        lines = self.lines(app)
        self.assertTrue(all(len(line) <= 44 for line in lines), "\n".join(lines))
        self.assertTrue(any(GHOSTTY in line for line in lines))
        self.assertTrue(any(INSIDE in line for line in lines))

    def test_a_changes_draft_survives_a_visit_to_settings_and_a_placement_change(self):
        # Break: leaving for Settings (or saving the placement) drops the unsent inline message.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "yarım kaldı")
        app.handle_key("esc")
        self.keys(app, "6", "j", "enter", "2")
        self.assertIn("yarım kaldı", self.lines(app)[self.row_of(app, "+ TWO")])


class TabVisibilityTests(PanelFixture):
    """Settings shows or hides each tab; hiding only removes it from navigation."""

    LABELS = ("Agents", "Changes", "Browser", "Pull Requests", "Board", "Settings")

    def tab_row(self, app):
        return self.lines(app)[0]

    def saved(self):
        return json.loads((self.state / "ui-state.json").read_text())

    def toggle(self, app, label):
        self.click_text(app, f"] {label}")

    def row_text(self, app, label):
        return next(line for line in self.lines(app) if f"] {label}" in line)

    def test_hiding_a_tab_removes_it_from_the_tab_row_and_its_number_key_goes_nowhere(self):
        # Break: the toggle does nothing, the tab stays in the row, or its number key still
        # opens a tab the user hid.
        app = self.app()
        app.handle_key("6")
        self.assertIn("[x] Board", self.row_text(app, "Board"))
        self.toggle(app, "Board")
        self.assertNotIn("Board", self.tab_row(app))
        self.assertIn("[ ] Board", self.row_text(app, "Board"))
        self.assertEqual(self.saved()["hidden_tabs"], ["board"])
        app.handle_key("5")
        self.assertEqual(app.tab, "settings")
        app.handle_key("1")
        self.assertEqual(app.tab, "agents")

    def test_clicking_where_a_hidden_tab_was_opens_the_tab_now_drawn_there(self):
        # Break: the click targets still follow the full tab list, so a click lands on the
        # wrong (or the hidden) tab.
        app = self.app()
        app.handle_key("6")
        self.toggle(app, "Changes")
        self.click_text(app, "Browser")
        self.assertEqual(app.tab, "browser")

    def test_keys_toggle_the_tab_under_the_cursor_and_the_choice_survives_a_restart(self):
        # Break: j/k never reach the tab rows, Space does not toggle, hidden tabs are not
        # written, or a restarted Firstpane shows them again.
        app = self.app()
        self.keys(app, "6", "j", "j", "j", " ")  # Placement x2, then Agents, Changes
        self.assertIn("[ ] Changes", self.row_text(app, "Changes"))
        self.keys(app, "j", "j", "j", "enter")  # Browser, Pull Requests, Board
        self.assertIn("[ ] Board", self.row_text(app, "Board"))
        again = self.app()
        self.assertEqual(again.tab, "settings")
        first = self.tab_row(again)
        self.assertNotIn("Changes", first)
        self.assertNotIn("Board", first)
        for label in ("Agents", "Browser", "Pull Requests", "Settings"):
            self.assertIn(label, first)
        self.assertIn("[ ] Changes", self.row_text(again, "Changes"))
        self.toggle(again, "Changes")
        self.assertIn("Changes", self.tab_row(again))
        self.assertEqual(self.saved()["hidden_tabs"], ["board"])

    def test_state_without_the_setting_or_with_junk_in_it_shows_every_tab(self):
        # Break: ui-state.json from before this setting hides tabs (or fails to load), or
        # junk names and wrong types hide something real.
        self.state.mkdir(parents=True, exist_ok=True)
        for hidden in (None, "board", ["nothing", 7, None, ["board"]], {"board": True}):
            payload = {"tab": "changes"}
            if hidden is not None:
                payload["hidden_tabs"] = hidden
            (self.state / "ui-state.json").write_text(json.dumps(payload))
            app = self.app()
            self.assertEqual(app.tab, "changes", hidden)
            row = self.tab_row(app)
            for label in self.LABELS:
                self.assertIn(label, row, hidden)

    def test_a_saved_tab_that_is_hidden_opens_the_first_visible_tab_instead(self):
        # Break: Firstpane restarts on a tab that has no entry in the row; with everything
        # hidden it must still open on Settings, the tab key 6 always reaches.
        self.state.mkdir(parents=True, exist_ok=True)
        for saved, hidden, want in (
            ("board", ["board", "agents"], "changes"),
            ("settings", ["settings"], "agents"),
            ("board", ["agents", "changes", "browser", "prs", "board", "settings"], "settings"),
        ):
            (self.state / "ui-state.json").write_text(json.dumps({"tab": saved, "hidden_tabs": hidden}))
            self.assertEqual(self.app().tab, want, hidden)

    def test_settings_can_be_hidden_and_key_six_still_opens_it_and_shows_it_again(self):
        # Break: Settings stays in the row once hidden, key 6 stops opening it (no way back),
        # or the hide is not remembered.
        app = self.app()
        app.handle_key("6")
        self.toggle(app, "Settings")
        self.assertNotIn("Settings", self.tab_row(app))
        self.assertIn("key 6", app.status)
        self.assertEqual(self.saved()["hidden_tabs"], ["settings"])
        app.handle_key("1")
        self.assertEqual(app.tab, "agents")
        self.assertNotIn("Settings", self.tab_row(app))
        app.handle_key("6")
        self.assertEqual(app.tab, "settings")
        self.assertIn("[ ] Settings", self.row_text(app, "Settings"))
        self.toggle(app, "Settings")
        self.assertIn("Settings", self.tab_row(app))
        self.assertEqual(self.saved()["hidden_tabs"], [])

    def test_hiding_every_tab_leaves_key_six_as_the_way_back_and_it_survives_a_restart(self):
        # Break: with all six hidden the number keys open something, key 6 no longer opens
        # Settings, or a restart is stuck on a tab with no way to restore the others.
        app = self.app()
        app.handle_key("6")
        for label in self.LABELS:
            self.toggle(app, label)
        self.assertEqual(self.tab_row(app).strip(), "")
        for key in "12345":
            app.handle_key(key)
        self.assertEqual(app.tab, "settings")
        again = self.app()
        self.assertEqual(again.tab, "settings")
        self.assertEqual(self.tab_row(again).strip(), "")
        self.toggle(again, "Agents")
        again.handle_key("1")
        self.assertEqual(again.tab, "agents")
        again.handle_key("6")
        self.assertEqual(again.tab, "settings")

    def test_hiding_and_showing_changes_keeps_its_unsent_draft_and_the_selected_agent(self):
        # Break: hiding a tab throws away that tab's state, here the inline message under a
        # diff line, or the agent the other tabs act on.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "yarım kaldı")
        app.handle_key("esc")
        target = dict(app.target)
        self.keys(app, "6")
        self.toggle(app, "Changes")
        self.assertNotIn("Changes", self.tab_row(app))
        self.toggle(app, "Changes")
        app.handle_key("2")
        self.assertIn("yarım kaldı", self.lines(app)[self.row_of(app, "+ TWO")])
        self.assertEqual(app.target, target)

    def test_a_narrow_pane_can_still_reach_and_toggle_the_last_tab_row(self):
        # Break: in a short side pane the cursor moves onto tab rows that are cut off, so the
        # toggle acts on a row the user cannot see.
        app = self.app(rows=20, cols=44)
        app.handle_key("6")
        for _ in range(6):
            app.handle_key("j")
        self.assertTrue(any("> [x] Board" in line for line in self.lines(app)), "\n".join(self.lines(app)))
        app.handle_key(" ")
        self.assertTrue(any("> [ ] Board" in line for line in self.lines(app)), "\n".join(self.lines(app)))
        self.assertNotIn("Bd", self.tab_row(app))
        self.assertTrue(all(len(line) <= 44 for line in self.lines(app)))

    # ---- numbers follow what is shown --------------------------------------------------
    NAMES = ("agents", "changes", "browser", "prs", "board", "settings")
    LABEL = dict(zip(NAMES, LABELS))
    NUMBERED = re.compile(r"(\d) (Agents|Changes|Browser|Pull Requests|Board|Settings)")

    def numbered(self, app):
        return [(int(number), label) for number, label in self.NUMBERED.findall(self.tab_row(app))]

    def save_hidden(self, hidden):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "ui-state.json").write_text(json.dumps({"hidden_tabs": list(hidden)}))

    def test_every_combination_of_hidden_tabs_numbers_what_is_shown_from_one_and_the_keys_agree(self):
        # Break: a number is still the tab's fixed position (Settings stays 6, a hidden tab leaves
        # a gap), the row and the number keys disagree, or a number past the shown tabs opens one.
        # Key 6 is the single extra way back to a hidden Settings.
        for mask in range(64):
            hidden = [name for bit, name in enumerate(self.NAMES) if mask >> bit & 1]
            shown = [name for name in self.NAMES if name not in hidden]
            self.save_hidden(hidden)
            app = self.app()
            self.assertEqual(self.numbered(app), [(n, self.LABEL[name]) for n, name in enumerate(shown, start=1)], hidden)
            for number, name in enumerate(shown, start=1):
                app.handle_key(str(number))
                self.assertEqual(app.tab, name, (hidden, number))
            for number in range(len(shown) + 1, 7):
                before = app.tab
                app.handle_key(str(number))
                recovered = number == 6 and "settings" in hidden
                self.assertEqual(app.tab, "settings" if recovered else before, (hidden, number))

    def test_with_only_agents_and_settings_shown_they_are_one_and_two(self):
        # Break: Settings keeps its fixed 6 (or leaves a gap) when the tabs between are hidden, so
        # the number on screen is not the key that works.
        self.save_hidden(["changes", "browser", "prs", "board"])
        app = self.app()
        app.handle_key("2")
        self.assertEqual(app.tab, "settings")
        app.handle_key("1")
        self.assertEqual(app.tab, "agents")
        app.handle_key("6")
        self.assertEqual(app.tab, "agents")
        self.assertEqual(self.tab_row(app).split(), ["1", "Agents", "2", "Settings"])

    def test_hiding_and_showing_a_tab_renumbers_the_row_and_the_keys_at_once_and_after_a_restart(self):
        # Break: the labels or the keys keep the old numbers until a restart, or the saved state
        # stores numbers (it must keep tab names, so showing a tab again restores its place).
        app = self.app()
        app.handle_key("6")
        self.toggle(app, "Changes")
        self.assertEqual(self.numbered(app), [(1, "Agents"), (2, "Browser"), (3, "Pull Requests"), (4, "Board"), (5, "Settings")])
        app.handle_key("5")
        self.assertEqual(app.tab, "settings")
        app.handle_key("1")
        app.handle_key("4")
        self.assertEqual(app.tab, "board")
        self.assertEqual(self.saved()["hidden_tabs"], ["changes"])
        again = self.app()
        self.assertEqual(self.numbered(again), [(1, "Agents"), (2, "Browser"), (3, "Pull Requests"), (4, "Board"), (5, "Settings")])
        again.handle_key("5")
        self.assertEqual(again.tab, "settings")
        self.toggle(again, "Changes")
        self.assertEqual(self.numbered(again)[1], (2, "Changes"))
        again.handle_key("2")
        self.assertEqual(again.tab, "changes")

    def test_a_narrow_pane_keeps_the_numbers_on_the_short_labels(self):
        # Break: the short labels a narrow pane uses lose their numbers or keep the fixed ones.
        self.save_hidden(["changes"])
        app = self.app(rows=20, cols=44)
        self.assertEqual(self.tab_row(app).split(), ["1", "Agents", "2", "Br", "3", "PR", "4", "Bd", "5", "Set"])
        app.handle_key("5")
        self.assertEqual(app.tab, "settings")

    def test_clicking_a_numbered_tab_label_opens_it(self):
        # Break: the click targets do not cover the number the label now starts with.
        self.save_hidden(["changes", "browser"])
        app = self.app()
        self.click_text(app, "2 Pull Requests")
        self.assertEqual(app.tab, "prs")
        self.click_text(app, "4 Settings")
        self.assertEqual(app.tab, "settings")


if __name__ == "__main__":
    unittest.main()
