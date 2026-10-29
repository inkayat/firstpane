"""The tab row: tab names in their usual order (the number keys count only the shown ones), their labels, and the short labels a narrow pane uses.

Settings is a header control, not a tab. Review replaces the old Changes and Pull Requests tabs.
"""

TABS = ("agents", "review", "board", "terminal", "browser")
TAB_LABELS = {
    "agents": "Agents",
    "review": "Review",
    "board": "Board",
    "terminal": "Terminal",
    "browser": "Browser",
}
TAB_SHORT = {
    "agents": "Ag",
    "review": "Rv",
    "board": "Bd",
    "terminal": "Tm",
    "browser": "Br",
}
SETTINGS_KEY = ","
SOURCE_KEY = "\\"
