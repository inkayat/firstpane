"""Settings tab: the one Browser placement preference, chosen by keys or a click and kept.

Herdr is faked at the socket boundary and Git runs against fixture repositories (tests.test_panel
fixture). No browser is configured here, so choosing a placement never opens one.
"""

import json
import unittest

from tests.test_panel import PanelFixture, plain

GHOSTTY = "Ghostty tab"
BESIDE = "Beside Firstpane"


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
        self.assertIn("( ) " + BESIDE, self.choice_line(app, BESIDE))
        self.assertNotIn("\x1b[", app.render())

    def test_keys_choose_beside_firstpane_and_the_choice_survives_a_restart(self):
        # Break: j/k and Enter do not move or choose, the choice is not written, or a restarted
        # Firstpane is back at the default.
        app = self.app()
        self.keys(app, "6", "j", "enter")
        self.assertIn("(\u2022) " + BESIDE, self.choice_line(app, BESIDE))
        self.assertIn("( ) " + GHOSTTY, self.choice_line(app, GHOSTTY))
        self.assertIn("saved", plain(app.render()))
        self.assertEqual(self.saved(), "beside")
        again = self.app()
        self.assertEqual(again.tab, "settings")
        self.assertIn("(\u2022) " + BESIDE, self.choice_line(again, BESIDE))
        self.keys(again, "k", " ")
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(again, GHOSTTY))
        self.assertEqual(self.saved(), "ghostty")

    def test_a_click_on_a_choice_chooses_it_and_a_click_on_the_tab_opens_settings(self):
        # Break: the Settings tab label or a choice row ignores the mouse.
        app = self.app()
        self.click_text(app, "Settings")
        self.assertEqual(app.tab, "settings")
        self.click_text(app, BESIDE)
        self.assertEqual(self.saved(), "beside")
        self.click_text(app, GHOSTTY)
        self.assertEqual(self.saved(), "ghostty")

    def test_older_saved_state_still_loads_and_an_unknown_placement_falls_back_to_ghostty(self):
        # Break: ui-state.json written before this setting existed (or holding a value this
        # version does not offer) breaks loading, drops the saved tab, or picks Beside.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "changes", "folded_projects": []}))
        app = self.app()
        self.assertEqual(app.tab, "changes")
        app.handle_key("6")
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(app, GHOSTTY))
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "settings", "browser_placement": "in-tab"}))
        again = self.app()
        self.assertIn("(\u2022) " + GHOSTTY, self.choice_line(again, GHOSTTY))

    def test_a_narrow_pane_keeps_both_choices_readable_within_its_width(self):
        # Break: in a side pane the choice labels are cut off or lines run past the pane edge.
        app = self.app(rows=20, cols=44)
        app.handle_key("6")
        lines = self.lines(app)
        self.assertTrue(all(len(line) <= 44 for line in lines), "\n".join(lines))
        self.assertTrue(any(GHOSTTY in line for line in lines))
        self.assertTrue(any(BESIDE in line for line in lines))

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


if __name__ == "__main__":
    unittest.main()
