"""Settings tab: Firstpane's own preferences, kept with the rest of the UI state (ui-state.json).

Browser placement chooses where the Browser tab opens the selected agent's browser: a new tab in
the Ghostty window in front (firstpane/placed.py), or inside Firstpane, drawn in the Browser tab
itself (firstpane/browser.py). The choice is saved when it is made; a browser already open moves
the next time the Browser tab opens it.

Tabs shows or hides each tab in the tab row. Hiding only removes the tab from navigation; its data
and any browser it opened stay as they are. The number keys count the shown tabs 1, 2, 3... in
their fixed order. Settings is a header control, always reachable, not a tab.
"""

LEGACY_SOURCES = {"changes": "workspace", "prs": "pr"}

from firstpane.cells import fit
from firstpane.pr_view import word_wrap
from firstpane.style import BRIGHT, INK, MUTED, SELECTED_BG, paint, rgb_bg, rgb_fg
from firstpane.tabs import TAB_LABELS, TABS

PLACEMENTS = ("ghostty", "embedded")
DEFAULT_PLACEMENT = "ghostty"
PLACEMENT_LABELS = {"ghostty": "Ghostty tab", "embedded": "Inside Firstpane"}
PLACEMENT_NOTES = {
    "ghostty": "a new tab in the Ghostty window in front (macOS)",
    "embedded": "the page drawn in this pane's Browser tab",
}
LABEL_WIDTH = max(len(label) for label in PLACEMENT_LABELS.values())


def migrate_ui_state(data, tabs):
    """(tab, review_source, hidden) from saved ui-state.

    Old Changes and Pull Requests tabs become Review sources. Review is hidden only when both
    were hidden. A saved Settings tab, and Settings in hidden_tabs, are dropped.
    """
    data = data if isinstance(data, dict) else {}
    saved = data.get("tab")
    source = data.get("review_source") if data.get("review_source") in ("workspace", "pr") else "workspace"
    if saved in LEGACY_SOURCES:
        source = LEGACY_SOURCES[saved]
        saved = "review"
    elif saved == "settings":
        saved = None
    raw = data.get("hidden_tabs")
    raw = raw if isinstance(raw, list) else []
    hidden = {name for name in raw if name in tabs}
    if "changes" in raw and "prs" in raw:
        hidden.add("review")
    visible = [name for name in tabs if name not in hidden]
    if saved not in tabs or saved in hidden:
        saved = visible[0] if visible else tabs[0]
    return saved, source, hidden


class SettingsMixin:
    def _init_settings(self):
        self.browser_placement = DEFAULT_PLACEMENT
        self.hidden_tabs = set()
        self._settings_shown()

    def _load_settings(self, data):
        placement = data.get("browser_placement")
        if placement == "beside":  # the Herdr pane beside Firstpane that Inside Firstpane replaced
            placement = "embedded"
        if placement in PLACEMENTS:
            self.browser_placement = placement
        hidden = data.get("hidden_tabs")
        self.hidden_tabs = {name for name in hidden if name in TABS} if isinstance(hidden, list) else set()
        self._settings_shown()

    def _visible_tabs(self):
        return [name for name in TABS if name not in self.hidden_tabs]

    def _tab_for_number(self, key):
        """The tab a number key opens: the shown tabs counted from 1. Settings is not a number."""
        visible = self._visible_tabs()
        if key.isdigit() and 1 <= int(key) <= len(visible):
            return visible[int(key) - 1]
        return None

    def _settings_shown(self):
        """The cursor starts on the current choice."""
        self.settings_cursor = PLACEMENTS.index(self.browser_placement)

    def _settings_items(self):
        """What the cursor walks: the placement choices, then one row per tab."""
        return [("placement", value) for value in PLACEMENTS] + [("tab", name) for name in TABS]

    def _choose_placement(self, value):
        self.browser_placement = value
        self.settings_cursor = PLACEMENTS.index(value)
        self.status = f"Browser placement saved: {PLACEMENT_LABELS[value]}"
        self._save_ui()

    def _toggle_tab(self, name):
        self.settings_cursor = self._settings_items().index(("tab", name))
        self.hidden_tabs ^= {name}
        hidden = name in self.hidden_tabs
        self.status = f"{TAB_LABELS[name]} tab {'hidden' if hidden else 'shown'}"
        self._save_ui()

    # ---- body --------------------------------------------------------------------------
    def _settings_body(self, plain, budget):
        def prose(text, indent=1):
            return [(" " * indent + paint(part, rgb_fg(MUTED), plain), None)
                    for part in word_wrap(text, max(1, self.cols - indent))]

        lines = [(" " + paint("Browser placement", rgb_fg(BRIGHT, bold=True), plain), None)]
        lines += prose("Where the Browser tab opens the selected agent's browser")
        lines.append(("", None))
        width = 2 + 4 + LABEL_WIDTH  # cursor, mark, label
        wide = self.cols >= 1 + width + 3 + max(len(note) for note in PLACEMENT_NOTES.values())
        cursor_line = 0  # the row the cursor is on, kept on screen in a short pane
        for index, value in enumerate(PLACEMENTS):
            hit = {"kind": "setting-choice", "value": value}
            cursor = index == self.settings_cursor
            mark = "(\u2022)" if value == self.browser_placement else "( )"
            head = f"{'>' if cursor else ' '} {mark} {PLACEMENT_LABELS[value]}"
            note = PLACEMENT_NOTES[value]
            style = (rgb_bg(SELECTED_BG) + ";" if cursor else "") + rgb_fg(INK, bold=value == self.browser_placement)
            cursor_line = len(lines) if cursor else cursor_line
            if wide:
                lines.append((" " + paint(head.ljust(width), style, plain) + "   " + paint(note, rgb_fg(MUTED), plain), hit))
            else:
                lines.append((" " + paint(fit(head, self.cols - 1), style, plain), hit))
                lines += [(text, hit) for text, _ in prose(note, indent=7)]
        lines.append(("", None))
        lines.append((" " + paint("Tabs", rgb_fg(BRIGHT, bold=True), plain), None))
        lines += prose("Show or hide tabs in the tab row. The number keys open the shown tabs, counted from 1; "
                       "a hidden tab keeps its data. Settings stays in the header")
        lines.append(("", None))
        for index, name in enumerate(TABS, start=len(PLACEMENTS)):
            hit = {"kind": "setting-tab", "value": name}
            cursor = index == self.settings_cursor
            shown = name not in self.hidden_tabs
            head = f"{'>' if cursor else ' '} [{'x' if shown else ' '}] {TAB_LABELS[name]}"
            style = (rgb_bg(SELECTED_BG) + ";" if cursor else "") + rgb_fg(INK, bold=shown)
            cursor_line = len(lines) if cursor else cursor_line
            lines.append((" " + paint(fit(head, self.cols - 1), style, plain), hit))
        lines.append(("", None))
        lines += prose("j/k move \u00b7 Enter, Space or a click chooses or toggles \u00b7 saved at once; "
                       "an open browser moves the next time the Browser tab opens it")
        start = max(0, cursor_line - budget + 1)
        lines = lines[start : start + budget]
        return [text for text, _ in lines], [hit for _, hit in lines]

    # ---- keys and mouse ------------------------------------------------------------------
    def _settings_key(self, key):
        items = self._settings_items()
        if key in ("j", "down"):
            self.settings_cursor = min(self.settings_cursor + 1, len(items) - 1)
        elif key in ("k", "up"):
            self.settings_cursor = max(self.settings_cursor - 1, 0)
        elif key in ("enter", " "):
            kind, value = items[self.settings_cursor]
            if kind == "placement":
                self._choose_placement(value)
            else:
                self._toggle_tab(value)

    def _settings_mouse(self, hit):
        if hit["kind"] == "setting-tab":
            self._toggle_tab(hit["value"])
        elif hit.get("value") in PLACEMENTS:
            self._choose_placement(hit["value"])
