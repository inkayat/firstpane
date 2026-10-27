"""Settings tab: Firstpane's own preferences, kept with the rest of the UI state (ui-state.json).

Browser placement chooses where the Browser tab opens the selected agent's browser
(firstpane/placed.py): a new tab in the Ghostty window in front, or a Herdr pane beside Firstpane. The
choice is saved when it is made; a browser already open moves the next time the Browser tab
opens it.
"""

from firstpane.cells import fit
from firstpane.placed import DEFAULT_PLACEMENT, PLACEMENT_LABELS, PLACEMENTS
from firstpane.pr_view import word_wrap
from firstpane.style import BRIGHT, INK, MUTED, SELECTED_BG, paint, rgb_bg, rgb_fg

PLACEMENT_NOTES = {
    "ghostty": "a new tab in the Ghostty window in front (macOS)",
    "beside": "a Herdr pane to the right of Firstpane",
}
LABEL_WIDTH = max(len(label) for label in PLACEMENT_LABELS.values())


class SettingsMixin:
    def _init_settings(self):
        self.browser_placement = DEFAULT_PLACEMENT
        self._settings_shown()

    def _load_settings(self, data):
        if data.get("browser_placement") in PLACEMENTS:
            self.browser_placement = data["browser_placement"]
        self._settings_shown()

    def _settings_shown(self):
        """The cursor starts on the current choice."""
        self.settings_cursor = PLACEMENTS.index(self.browser_placement)

    def _choose_placement(self, value):
        self.browser_placement = value
        self.settings_cursor = PLACEMENTS.index(value)
        self.status = f"Browser placement saved: {PLACEMENT_LABELS[value]}"
        self._save_ui()

    # ---- body --------------------------------------------------------------------------
    def _settings_body(self, plain, budget):
        def prose(text, indent=1):
            return [(" " * indent + paint(part, rgb_fg(MUTED), plain), None)
                    for part in word_wrap(text, max(1, self.cols - indent))]

        lines = [(" " + paint("Browser placement", rgb_fg(BRIGHT, bold=True), plain), None)]
        lines += prose("Where the Browser tab (3) opens the selected agent's browser")
        lines.append(("", None))
        width = 2 + 4 + LABEL_WIDTH  # cursor, mark, label
        wide = self.cols >= 1 + width + 3 + max(len(note) for note in PLACEMENT_NOTES.values())
        for index, value in enumerate(PLACEMENTS):
            hit = {"kind": "setting-choice", "value": value}
            cursor = index == self.settings_cursor
            mark = "(\u2022)" if value == self.browser_placement else "( )"
            head = f"{'>' if cursor else ' '} {mark} {PLACEMENT_LABELS[value]}"
            note = PLACEMENT_NOTES[value]
            style = (rgb_bg(SELECTED_BG) + ";" if cursor else "") + rgb_fg(INK, bold=value == self.browser_placement)
            if wide:
                lines.append((" " + paint(head.ljust(width), style, plain) + "   " + paint(note, rgb_fg(MUTED), plain), hit))
            else:
                lines.append((" " + paint(fit(head, self.cols - 1), style, plain), hit))
                lines += [(text, hit) for text, _ in prose(note, indent=7)]
        lines.append(("", None))
        lines += prose("j/k move \u00b7 Enter, Space or a click chooses \u00b7 saved at once; "
                       "an open browser moves the next time the Browser tab opens it")
        lines = lines[:budget]
        return [text for text, _ in lines], [hit for _, hit in lines]

    # ---- keys and mouse ------------------------------------------------------------------
    def _settings_key(self, key):
        if key in ("j", "down"):
            self.settings_cursor = min(self.settings_cursor + 1, len(PLACEMENTS) - 1)
        elif key in ("k", "up"):
            self.settings_cursor = max(self.settings_cursor - 1, 0)
        elif key in ("enter", " "):
            self._choose_placement(PLACEMENTS[self.settings_cursor])

    def _settings_mouse(self, hit):
        if hit.get("value") in PLACEMENTS:
            self._choose_placement(hit["value"])
