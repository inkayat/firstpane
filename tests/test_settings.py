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
        app.handle_key(",")
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
        self.keys(app, ",", "j", "enter")
        self.assertIn("(\u2022) " + INSIDE, self.choice_line(app, INSIDE))
        self.assertIn("( ) " + GHOSTTY, self.choice_line(app, GHOSTTY))
        self.assertIn("saved", plain(app.render()))
        self.assertEqual(self.saved(), "embedded")
        again = self.app()
        again.handle_key(",")
        self.assertTrue(again.settings_open)
        self.assertIn("(\u2022) " + INSIDE, self.choice_line(again, INSIDE))
        self.keys(again, "k", " ")
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(again, GHOSTTY))
        self.assertEqual(self.saved(), "ghostty")

    def test_a_click_on_a_choice_chooses_it_and_a_click_on_the_tab_opens_settings(self):
        # Break: the Settings tab label or a choice row ignores the mouse.
        app = self.app()
        self.click_text(app, "Settings")
        self.assertTrue(app.settings_open)
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
        self.assertEqual((app.tab, app.review_source), ("review", "workspace"))
        app.handle_key(",")
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(app, GHOSTTY))
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "settings", "browser_placement": "in-tab"}))
        again = self.app()
        again.handle_key(",")
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(again, GHOSTTY))

    def test_a_saved_beside_firstpane_choice_now_opens_inside_firstpane(self):
        # Break: after the upgrade a user who chose the Herdr-side browser (the only choice that
        # works off macOS) silently lands on the Ghostty tab default.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "settings", "browser_placement": "beside"}))
        app = self.app()
        app.handle_key(",")
        self.assertIn("(\u2022) " + INSIDE, self.choice_line(app, INSIDE))
        app.handle_key("1")
        self.assertEqual(self.saved(), "embedded")

    def test_a_narrow_pane_keeps_both_choices_readable_within_its_width(self):
        # Break: in a side pane the choice labels are cut off or lines run past the pane edge.
        app = self.app(rows=20, cols=44)
        app.handle_key(",")
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
        self.keys(app, ",", "j", "enter", "2")
        self.assertIn("yarım kaldı", self.lines(app)[self.row_of(app, "+ TWO")])



class TabVisibilityTests(PanelFixture):
    """Settings shows or hides each tab. Settings itself stays a header control."""

    def tab_row(self, app):
        return self.lines(app)[0]

    def saved(self):
        return json.loads((self.state / "ui-state.json").read_text())

    def open_settings(self, app):
        if not app.settings_open:
            app.handle_key(",")

    def toggle(self, app, label):
        self.open_settings(app)
        self.click_text(app, f"] {label}")

    def test_hiding_review_removes_it_and_a_restart_keeps_that(self):
        # Break: hiding Review leaves it in the row, or a restart shows it again.
        app = self.app()
        self.toggle(app, "Review")
        app.handle_key("esc")
        self.assertNotIn("Review", self.tab_row(app).split())
        self.assertEqual(self.saved()["hidden_tabs"], ["review"])
        again = self.app()
        self.assertNotIn("Review", self.tab_row(again).split())
        self.toggle(again, "Review")
        again.handle_key("esc")
        self.assertIn("Review", self.tab_row(again).split())

    def test_number_keys_count_only_the_shown_tabs(self):
        # Break: a hidden tab still takes a number, or the next tab does not move up.
        app = self.app()
        self.toggle(app, "Review")
        app.handle_key("esc")
        app.handle_key("2")
        self.assertEqual(app.tab, "board")
        app.handle_key("4")
        self.assertEqual(app.tab, "browser")

    def test_saved_changes_and_prs_tabs_migrate_and_settings_is_not_a_tab(self):
        # Break: an old Changes or Pull Requests tab is dropped, or Settings comes back as a tab.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "prs", "hidden_tabs": ["changes", "prs", "settings"]}))
        app = self.app()
        self.assertEqual((app.tab, app.review_source), ("agents", "pr"))
        self.assertIn("review", app.hidden_tabs)
        self.assertNotIn("settings", app.hidden_tabs)
        app.handle_key(",")
        self.assertTrue(app.settings_open)
        self.assertNotIn("] Settings", plain(app.render()))


if __name__ == "__main__":
    unittest.main()
