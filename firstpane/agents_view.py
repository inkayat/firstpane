"""The Agents tab: a search field with a status filter, counts, collapsible project groups of
two-line agent rows, and, pinned at the bottom, the selected agent's context, the FirstMate
message field and a key-hint footer.

Everything shown comes from the panel model: Herdr's own agent fields, the agent's resolved
workspace (branch, changed files) and the FirstMate message drafts. A field the model does not
have is left out, never invented. Layout follows the supplied reference image; colors are 24-bit
and `NO_COLOR` drops them (glyph, bar and words keep every state readable).
"""

from firstpane.cells import clip, fit, pad, spread, width
from firstpane.sanitize import sanitize
from firstpane.style import (
    ACCENT,
    BG,
    BORDER,
    BRIGHT,
    FIELD_BG,
    GREEN,
    INK,
    MUTED,
    PINK,
    SELECTED_BG,
    SEND_BG,
    VIOLET,
    YELLOW,
    paint,
)

RESET = "\x1b[0m"

BAR = "\u258c"
CHEVRON = "\u203a"
SEARCH_ICON = "\u2315"
PLACEHOLDER = "Search projects or agents\u2026"
COMPOSE_PLACEHOLDER = "Write something about the selected agent\u2026"
COMPOSE_NO_AGENT = "Select an agent to write to FirstMate"
TALL_TREE = 13  # the layout switches at the pane height it always did; the row its context line freed goes to the list
WIDE_STATUS = 64
LONG_STATUS = 88

# status: (glyph, color, label, short label). Working and Done are green (told apart by glyph
# and word), Idle is white; Waiting (blocked) is the yellow Working used to have.
STATUS_VIEW = {
    "working": ("\u25cf", GREEN, "Working", "Working"),
    "blocked": ("\u25cb", YELLOW, "Waiting for response", "Waiting"),
    "idle": ("\u25cf", BRIGHT, "Idle", "Idle"),
    "done": ("\u2713", GREEN, "Done", "Done"),
    "unknown": ("?", VIOLET, "Unknown", "Unknown"),
}
# harness id: name. Where the terminal shows kitty graphics a known harness is drawn by its
# official icon alone (firstpane/icons.py); the name is shown only where that image cannot be.
# Ids Herdr reports for the same tool are folded in.
HARNESS_NAMES = {"pi": "pi", "omp": "omp", "claude": "Claude", "grok": "Grok", "codex": "Codex"}
HARNESS_ALIASES = {"claude-code": "claude", "grok-build": "grok"}
UNKNOWN = "\u2014"
TITLE_MIN = 12
MODEL_MIN = 8
FILTERS = (("all", "All"), ("working", "Working"), ("blocked", "Waiting"), ("idle", "Idle"), ("done", "Done"))


def status_view(status):
    found = STATUS_VIEW.get(status)
    if found:
        return found
    label = sanitize(status).capitalize() or "Unknown"
    return ("\u00b7", MUTED, label, label)


def age_label(seconds):
    """`now`, `4m ago`, `3h ago`, `2d ago`."""
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def runtime_id(raw):
    """The harness a Herdr agent id names (`claude-code` as `claude`), or None when it names none."""
    text = " ".join(sanitize(raw).split()).lower()
    return HARNESS_ALIASES.get(text, text) or None


def harness_name(runtime):
    """The display name of a harness id; unlisted ids are shown as reported."""
    return HARNESS_NAMES.get(runtime) or runtime or "unknown"


def model_label(raw):
    """The model id without its provider (`openai-codex/gpt-5.6` as `gpt-5.6`), or None."""
    text = " ".join(sanitize(raw).split())
    rest = text.partition("/")[2]
    return rest or text or None


def effort_label(raw):
    return " ".join(sanitize(raw).split()) or None


def agent_identity(detected, recorded=None, model=None, effort=None):
    """(harness id, model, effort) of one agent. `detected` is the id Herdr sees running in the
    pane; `recorded`, `model` and `effort` are FirstMate's launch record for it. The record is
    used only when it names a harness and Herdr does not contradict it; otherwise the model and
    effort belong to some other process and are unknown (None)."""
    live, launched = runtime_id(detected), runtime_id(recorded)
    if launched is None or (live is not None and live != launched):
        return live, None, None
    return launched, model_label(model), effort_label(effort)


def humanize(task_id):
    """`herdr-project-sidebar` as `Herdr project sidebar`."""
    words = " ".join((task_id or "").replace("_", " ").replace("-", " ").split())
    return words[:1].upper() + words[1:]


def _fg(text, color, plain, bold=False):
    return paint(text, ("1;" if bold else "") + "38;2;%d;%d;%d" % color, plain)


def _on(text, color, plain):
    """`text` on a background color; it is re-asserted after every reset inside."""
    if plain:
        return text
    code = "\x1b[48;2;%d;%d;%dm" % color
    return code + text.replace(RESET, RESET + code) + RESET


def _surface(text, color, cols):
    return _on(text + " " * max(0, cols - width(text)), color, False)


def _plural(count, noun):
    return f"{count} {noun}" + ("" if count == 1 else "s")


class AgentsMixin:
    # ---- filtering -------------------------------------------------------------------
    def _status_label(self):
        return dict(FILTERS)[self.status_filter]

    def _cycle_filter(self):
        names = [name for name, _ in FILTERS]
        self.status_filter = names[(names.index(self.status_filter) + 1) % len(names)]

    def _clear_agent_filters(self):
        self.search.set("")
        self.status_filter = "all"

    def _branch_of(self, row):
        info = self._ws_info(row)
        status = info.get("status") if info else None
        if status is None:
            return None
        return f"detached {(status.oid or '?')[:7]}" if status.detached else (status.head or "?")

    def _agent_matches(self, project, row):
        if self.status_filter != "all" and row["status"] != self.status_filter:
            return False
        query = self.search.text.strip().lower()
        if not query or query in project["label"].lower():
            return True
        haystack = " ".join((row["text"], row.get("title") or "", self._branch_of(row) or ""))
        return query in haystack.lower()

    # ---- keys and clicks -------------------------------------------------------------
    def _open_agent_composer(self, row):
        """Write to FirstMate about the agent under the cursor: it becomes the selected agent
        (the message context) and its unsent message, if any, opens."""
        if row is None or row["kind"] != "agent":
            self.status = "select an agent to write about"
            return
        if not self.target or tuple(self.target["key"]) != tuple(row["key"]):
            self._select_target(row)
        target = self._agent_target()
        if self.draft is not None and self.draft.get("target") == target:
            self.focus = "composer"
            return
        self._open_draft(self.drafts.load(target, self._binding()))

    def _agents_bar_click(self, kind, hit, column):
        rows = self._rows()
        if kind == "searchbar":
            self._close_inline()
            if column >= hit["filter_at"]:
                self._cycle_filter()
            else:
                self.focus = "search"
            return
        if column >= hit["send_at"] and self.draft is not None and self.composer.text.strip():
            self._arm_send()
            return
        self._open_agent_composer(rows[self._selected_index(rows)] if rows else None)

    # ---- frame -----------------------------------------------------------------------
    def _agents_layout(self):
        """Which pieces fit: the reference layout when the pane is tall enough, one-line
        fields and no gaps below that, then pieces drop (footer, counts, context, search)."""
        avail = self.rows - 1
        tall = {"search": 3, "counts": 1, "gap": 1, "sep": 1, "ctx": 0, "label": 1, "input": 3, "footer": 1}
        if avail - sum(tall.values()) >= TALL_TREE:
            return tall
        layout = {"search": 1, "counts": 1, "gap": 0, "sep": 0, "ctx": 1, "label": 0, "input": 1, "footer": 1}
        for name in ("footer", "counts", "ctx", "search"):
            if avail - sum(layout.values()) >= 3:
                break
            layout[name] = 0
        return layout

    def _agents_frame(self, plain):
        layout = self._agents_layout()
        cols = self.cols
        rows = self._rows()
        index = self._selected_index(rows)
        cursor = rows[index] if rows else None
        tab = (self._tab_row(plain), {"kind": "tabs", "key": ("tabs",)}, None)
        top = []
        if layout["search"]:
            top.extend(self._search_lines(plain, layout["search"] == 3))
        if layout["counts"]:
            top.append((self._counts_line(rows, plain), None, None))
            if layout["gap"]:
                top.append(("", None, None))
        bottom = []
        if layout["sep"]:
            bottom.append((_fg("\u2500" * cols, BORDER, plain), None, None))
        if layout["ctx"]:
            bottom.append((self._context_line(cursor, plain), None, None))
        if layout["label"]:
            bottom.append((" " + _fg("Write to FirstMate", INK, plain), None, None))
        bottom.extend(self._input_lines(cursor, plain, layout["input"] == 3))
        if layout["footer"]:
            bottom.append((self._status_line(plain) if self.status else self._hint_line(cursor, plain), None, None))
        budget = max(1, self.rows - 1 - len(top) - len(bottom))
        tree = self._agent_tree_lines(rows, index, budget, plain, tall=layout["gap"] == 1)
        if self.status and not layout["footer"]:
            tree = tree[: budget - 1] + [(self._status_line(plain), None, None)] if len(tree) >= budget else tree + [(self._status_line(plain), None, None)]
        tree += [("", None, None)] * (budget - len(tree))
        lines = ([tab] + top + tree + bottom)[: self.rows]
        self.hit_rows = [hit for _, hit, _ in lines]
        if plain:
            return "\n".join(fit(pad(text, cols), cols) for text, _, _ in lines)
        return "\n".join(fit(_surface(text, color or BG, cols), cols) for text, _, color in lines)

    # ---- top -------------------------------------------------------------------------
    def _filter_text(self):
        if self.cols < 44 and self.status_filter == "all":
            return "\u25be"
        return self._status_label() + " \u25be"

    def _field_text(self, text, placeholder, focused, cursor, limit, plain):
        """The visible part of a one-line field: its text with a caret, or a placeholder."""
        if not text and not focused:
            return _fg(clip(placeholder, limit), MUTED, plain)
        if not focused:
            return _fg(clip(text.split("\n")[0], limit), INK, plain)
        line_start = text.rfind("\n", 0, cursor) + 1
        line_end = text.find("\n", cursor)
        line = text[line_start : len(text) if line_end < 0 else line_end]
        column = cursor - line_start
        before, after = line[:column], line[column:]
        room = max(1, limit - 1)
        while width(before) > room:
            before = before[1:]
        if plain:
            return clip(before + "\u258f" + after, limit)
        rest = clip(after[1:], max(0, limit - width(before) - 1))
        return _fg(before, INK, plain) + paint(after[:1] or " ", "7", plain) + _fg(rest, INK, plain)

    def _search_lines(self, plain, tall):
        cols = self.cols
        focused = self.focus == "search"
        label = self._filter_text()
        box = max(4, cols - width(label) - 4)
        tint = ACCENT if focused else BORDER
        limit = max(1, box - 6 if tall else box - 4)
        field = self._field_text(self.search.text, PLACEHOLDER, focused, self.search.cursor, limit, plain)
        icon = _fg(SEARCH_ICON, ACCENT if focused else MUTED, plain)
        hit = {"kind": "searchbar", "key": ("searchbar",), "filter_at": box + 2}
        right = _fg(label, ACCENT, plain)
        if not tall:
            inner = " " + icon + " " + field
            inner += " " * max(0, box - width(inner))
            text = " " + (inner if plain else _on(inner, FIELD_BG, plain)) + "  " + right
            return [(text, hit, None)]
        side = _fg("\u2502", tint, plain)
        top = _fg("\u256d" + "\u2500" * (box - 2) + "\u256e", tint, plain)
        bottom = _fg("\u2570" + "\u2500" * (box - 2) + "\u256f", tint, plain)
        middle = side + " " + icon + " " + field
        middle += " " * max(0, box - 1 - width(middle)) + side
        return [(" " + top, hit, None), (" " + middle + "  " + right, hit, None), (" " + bottom, hit, None)]

    def _counts_line(self, rows, plain):
        agents = [row for row in rows if row["kind"] == "agent"]
        projects = [row for row in rows if row["kind"] == "project"]
        if not agents and not projects:
            return " " + _fg("no agents", MUTED, plain)
        left = _fg(f"{_plural(len(projects), 'project')} \u00b7 {_plural(len(agents), 'agent')}", MUTED, plain)
        left = " " + left
        waiting = sum(1 for row in agents if row["status"] == "blocked")
        if waiting:
            for words in (f"{waiting} waiting for response", f"{waiting} waiting"):
                right = _fg("\u25cf", YELLOW, plain) + " " + _fg(words, MUTED, plain) + " "
                if width(left) + width(right) + 2 <= self.cols:
                    return spread(left, right, self.cols)
        return left

    # ---- tree ------------------------------------------------------------------------
    def _margins(self):
        """Columns between the selection bar and the status glyph, and glyph and title."""
        if self.cols >= 72:
            return 4, 3
        if self.cols >= 44:
            return 2, 2
        return 0, 1

    def _agent_tree_lines(self, rows, index, budget, plain, tall):
        """(text, hit, background) lines of the project groups that fit `budget`: the selected
        row whole, and its project heading kept on top when the list scrolls past it."""
        if not rows:
            word = "No matching agents" if self.search.text or self.status_filter != "all" else "No open agents"
            return [(" " + _fg(word, MUTED, plain), None, None)]
        flat = []  # (row number, (text, hit, background), is a spacer)
        for number, row in enumerate(rows):
            selected = number == index
            color = SELECTED_BG if selected else None
            if row["kind"] == "agent":
                if number and rows[number - 1]["kind"] == "agent":
                    flat.append((number, ("", None, None), True))
                texts = self._agent_lines(row, selected, plain)
            else:
                if tall and number:
                    flat.append((number, ("", None, None), True))
                texts = [self._heading_line(row, rows, number, selected, plain)]
            flat.extend((number, (text, row, color), False) for text in texts)
        if len(flat) <= budget:
            return [line for _, line, _ in flat]
        first = next(i for i, (number, _, spacer) in enumerate(flat) if number == index and not spacer)
        last = max(i for i, (number, _, _) in enumerate(flat) if number == index) + 1
        head = next((number for number in range(index, -1, -1) if rows[number]["kind"] != "agent"), index)
        head_line = max(i for i, (number, _, _) in enumerate(flat) if number == head)
        start = self._scroll_start(flat, first, last, budget)
        if start <= head_line or budget < 3:
            return [line for _, line, _ in flat[start : start + budget]]
        after = [entry for entry in flat if entry[0] > head]
        base = len(flat) - len(after)
        start = self._scroll_start(after, first - base, last - base, budget - 1)
        return [flat[head_line][1]] + [line for _, line, _ in after[start : start + budget - 1]]

    def _scroll_start(self, flat, first, last, room):
        """First line shown: the selected lines end the window, which never opens inside a row
        or on a spacer."""
        start = min(max(0, last - room), max(0, len(flat) - room))
        start = min(start, first)
        while 0 < start < first and (flat[start][2] or (flat[start][0] == flat[start - 1][0] and not flat[start - 1][2])):
            start += 1
        return start

    def _heading_line(self, row, rows, number, selected, plain):
        if row["kind"] == "project":
            mark = "\u25b8" if row["project_key"] in self.folded_projects else "\u25be"
            name = _fg(row["label"], PINK, plain, bold=True)
            count = row["agent_count"]
        else:
            mark = "\u25e6"
            name = _fg(row["label"], MUTED, plain, bold=True)
            count = len(rows) - number - 1
        left = self._bar(selected, False, plain) + " " + _fg(mark, MUTED, plain) + " " + name
        return spread(left, _fg(_plural(count, "agent"), MUTED, plain) + " ", self.cols)

    def _bar(self, selected, targeted, plain):
        if selected:
            return _fg(BAR, ACCENT, plain)
        return _fg("\u2022", MUTED, plain) if targeted else " "

    def _files_text(self, row):
        """Changed files of the agent's workspace, or why they are not known (never `clean`)."""
        info = self._ws_info(row)
        status = info.get("status") if info else None
        if status is None:
            word = self._ws_word(info)
            return "reading\u2026" if word == "loading" else sanitize(word.split(":")[0])
        return "clean" if status.changed == 0 else _plural(status.changed, "file")

    def _status_group(self, row, label):
        """Right-hand texts of the first line, fullest first: status, files, age. The status
        word is never dropped; files and age go before the title is squeezed."""
        files = self._files_text(row)
        changed_at = row.get("changed_at")
        age = age_label(max(0.0, self.clock() - changed_at)) if changed_at is not None and self.cols >= LONG_STATUS else None
        groups = [[label, files, age], [label, files], [label]]
        return [[part for part in group if part] for group in groups]

    def _identity_line(self, row, room, plain):
        """Harness, model, effort in `room` cells. The harness is its official icon alone where
        the terminal shows it, else its name. A long model is cut before the effort is dropped;
        the effort label shortens before it goes."""
        runtime = row.get("runtime")
        name = harness_name(runtime)
        icon = self.icons.cells(runtime)
        head, sep = (icon, " ") if icon else (_fg(name, INK, plain), " \u00b7 ")
        model = row.get("model") or UNKNOWN
        effort = row.get("effort") or UNKNOWN
        dot = " \u00b7 "
        lead = head + _fg(sep, MUTED, plain)
        for text in ("effort: " + effort, effort):
            space = room - width(lead) - width(dot) - width(text)
            if space >= min(width(model), MODEL_MIN):
                shown = clip(model, space)
                label, _, value = text.rpartition(" ")
                tail = (_fg(label + " ", MUTED, plain) if label else "") + _fg(value, MUTED if effort == UNKNOWN else INK, plain)
                return lead + _fg(shown, MUTED if model == UNKNOWN else INK, plain) + _fg(dot, MUTED, plain) + tail
        space = room - width(lead)
        if space >= 4:
            return lead + _fg(clip(model, space), MUTED if model == UNKNOWN else INK, plain)
        if icon and room >= width(icon):
            return icon
        return _fg(clip(name, room), INK, plain)

    def _agent_lines(self, row, selected, plain):
        cols = self.cols
        indent, gap = self._margins()
        glyph, color, long_label, short_label = status_view(row["status"])
        targeted = bool(self.target) and tuple(self.target["key"]) == tuple(row["key"])
        lead = " " * indent
        prefix = width(" ") + indent + 1 + gap
        title = sanitize(row.get("title") or row.get("label") or "")
        label = long_label if cols >= LONG_STATUS else short_label
        dot = " \u00b7 "
        for parts in self._status_group(row, label):
            tail = "  " + CHEVRON + " "
            title_room = max(1, cols - prefix - width(dot.join(parts) + tail) - 1)
            if title_room >= min(width(title), TITLE_MIN):
                break
        title = clip(title, title_room)
        title_paint = _fg(title, BRIGHT if selected else INK, plain, bold=selected)
        shown = _fg(parts[0], color, plain)
        shown += "".join(_fg(dot + part, MUTED, plain) for part in parts[1:])
        shown += "  " + _fg(CHEVRON, INK if selected else MUTED, plain) + " "
        first = self._bar(selected, targeted, plain) + lead + _fg(glyph, color, plain) + " " * gap + title_paint
        first += " " * max(0, cols - width(first) - width(shown)) + shown
        second = self._bar(selected, False, plain) + " " * (prefix - 1) + self._identity_line(row, max(1, cols - prefix - 1), plain)
        return [first, second]

    # ---- bottom ----------------------------------------------------------------------
    def _context_line(self, row, plain):
        if row is None:
            return " " + _fg("No agent selected", MUTED, plain)
        room = self.cols - 2
        if row["kind"] != "agent":
            return " " + _fg(clip(sanitize(row.get("label") or "[other]"), room), ACCENT, plain, bold=True)
        project = sanitize(row.get("project_label") or "[other]")
        label = sanitize(row.get("label"))
        branch = sanitize(self._branch_of(row) or "")
        if branch and width(project) + 3 + width(label) + 2 + width(branch) > room:
            return " " + _fg(clip(branch, room), INK, plain)  # too narrow for both: the branch is what only this line shows
        if width(project) + 3 + width(label) > room:
            label = clip(label, max(0, room - width(project) - 3))
            if not label:
                project = clip(project, room)
        left = " " + _fg(project, ACCENT, plain, bold=True)
        if label:
            left += _fg(" / ", MUTED, plain) + _fg(label, ACCENT, plain, bold=True)
        return spread(left, _fg(branch, MUTED, plain) + " ", self.cols) if branch else left

    def _composer_text(self, row):
        """What the field shows: the text being written, else the agent's unsent message."""
        if self.focus == "composer" and self.draft is not None:
            return self.composer.text
        if row is None or row["kind"] != "agent":
            return ""
        target = self._agent_target(row)
        if self.draft is not None and self.draft.get("target") == target:
            return self.composer.text
        draft = next((item for item in self.drafts.open_drafts() if item.get("target") == target and not item.get("anchor")), None)
        return (draft.get("body") or "") if draft else ""

    def _input_lines(self, row, plain, tall):
        cols = self.cols
        focused = self.focus == "composer" and self.draft is not None
        text = self._composer_text(row)
        placeholder = COMPOSE_PLACEHOLDER if row is not None and row["kind"] == "agent" else COMPOSE_NO_AGENT
        button = 5 if tall else 3
        box = max(6, cols - button - 3)
        hit = {"kind": "composer", "key": ("composer",), "send_at": box + 2}
        tint = ACCENT if focused else BORDER
        if not tall:
            limit = max(1, box - 4)
            field = self._field_text(text, placeholder, focused, self.composer.cursor, limit, plain)
            inner = " " + _fg(CHEVRON, ACCENT if focused else MUTED, plain) + " " + field
            inner += " " * max(0, box - width(inner))
            send = _on(_fg(" " + CHEVRON + " ", BRIGHT, plain, bold=True), SEND_BG, plain) if not plain else "[" + CHEVRON + "]"
            return [(" " + (inner if plain else _on(inner, FIELD_BG, plain)) + " " + send + " ", hit, None)]
        limit = max(1, box - 4)
        field = self._field_text(text, placeholder, focused, self.composer.cursor, limit, plain)
        side = _fg("\u2502", tint, plain)
        top = _fg("\u256d" + "\u2500" * (box - 2) + "\u256e", tint, plain)
        bottom = _fg("\u2570" + "\u2500" * (box - 2) + "\u256f", tint, plain)
        middle = side + " " + field
        middle += " " * max(0, box - 1 - width(middle)) + side
        if plain:
            buttons = ("\u256d\u2500\u2500\u2500\u256e", "\u2502 " + CHEVRON + " \u2502", "\u2570\u2500\u2500\u2500\u256f")
        else:
            fill = lambda text: _on(_fg(text, BRIGHT, plain, bold=True), SEND_BG, plain)
            buttons = (fill("     "), fill("  " + CHEVRON + "  "), fill("     "))
        return [
            (" " + top + " " + buttons[0] + " ", hit, None),
            (" " + middle + " " + buttons[1] + " ", hit, None),
            (" " + bottom + " " + buttons[2] + " ", hit, None),
        ]

    def _status_line(self, plain):
        return " " + _fg(sanitize(self.status), INK, plain)

    def _hint_line(self, row, plain):
        if self.focus == "composer":
            hints = [("Enter", "Preview and send"), ("Esc", "Back")]
        elif self.focus == "search":
            hints = [("Enter", "Keep"), ("Esc", "Clear")]
        else:
            enter = "Fold" if row is not None and row["kind"] == "project" else "Workspace"
            hints = [("\u2191\u2193", "Select"), ("Enter", enter), ("g", "Focus"), ("Tab", "Message"), ("/", "Search"), ("f", "Filter")]
        out, used = "", 1
        for key, word in hints:
            piece = (2 if out else 0) + width(key) + 1 + width(word)
            if used + piece > self.cols - 1:
                break
            out += ("  " if out else "") + _fg(key, INK, plain) + " " + _fg(word, MUTED, plain)
            used += piece
        return " " + out
