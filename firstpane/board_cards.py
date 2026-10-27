"""Board cards and lanes: pure drawing from a task and facts about it to terminal rows.

Nothing here reads state or writes a task. A card says only what the board projection
(`fm-board list --json` plus its hold reason) or an already-read live agent workspace says;
what is not known is left out, not guessed.
"""

from firstpane.agents_view import ACCENT, BORDER, BRIGHT, GREEN, INK, MUTED, PINK, YELLOW, _fg, _on, _plural
from firstpane.board import COLUMN_GLYPH
from firstpane.cells import clip, pad, width
from firstpane.sanitize import sanitize
from firstpane.style import SELECTED_BG

BLOCKED_RED = (220, 120, 120)
BAR = "\u258c"
RULE = "\u2500"
DIVIDER = "\u2502"
GO = "\u2197"
CARD_LINES = 4
COMPACT_ROOM = 32
CARD_ROWS = CARD_LINES + 1


def glyph_color(state):
    if state in ("in_progress", "done"):
        return GREEN
    if state == "waiting_review":
        return YELLOW
    if state == "blocked":
        return BLOCKED_RED
    return ACCENT


def rule(cells, plain):
    return _fg(RULE * max(0, cells), BORDER, plain)


def meta_text(task, files):
    """The fourth card line: awaiting reply, changed files, then what the task is doing or why it
    waits. Each part exists only when the projection or the live workspace says it."""
    parts = []
    if task.get("captain_review"):
        parts.append(("Awaiting reply", YELLOW))
    if files:
        parts.append((files, INK))
    detail = " ".join(sanitize(task.get("running") or task.get("hold_reason") or "").split())
    if detail:
        parts.append((detail, MUTED))
    return parts


def files_text(changed):
    """`3 files changed`, or None when the workspace was not read (never a guessed zero)."""
    if changed is None:
        return None
    return "No file changes" if changed == 0 else _plural(changed, "file") + " changed"


def card_lines(task, room, selected, plain, live=False, files=None):
    """The four content lines of one card, each at most `room` cells."""
    state = task.get("state") or "unknown"
    ident = sanitize(task.get("id") or "")
    title = sanitize(task.get("title") or "") or ident
    glyph = COLUMN_GLYPH.get(state, "?")
    ident_room = min(width(ident), max(6, room // 2))
    id_text = clip(ident, ident_room)
    title_room = room - width(id_text) - 2
    project = sanitize(task.get("project") or "")
    title_paint = lambda text: _fg(text, BRIGHT if selected else INK, plain, bold=selected)
    if room >= COMPACT_ROOM:
        head = _fg(id_text, MUTED, plain) + "  " + title_paint(clip(title, title_room))
        second = _fg(clip(project, room), PINK, plain) if project else _fg("no project", MUTED, plain)
    else:
        # A narrow lane (every card in it alike): the title gets the whole first line, the identifier follows the project.
        head = title_paint(clip(title, room))
        where = _fg(project or "no project", PINK if project else MUTED, plain)
        second = clip(where + _fg(" \u00b7 ", MUTED, plain) + _fg(ident, MUTED, plain), room)
    agent = sanitize(task.get("agent") or "")
    if agent:
        arrow = " " + GO if live else ""
        third = _fg("\u25c6 ", glyph_color(state), plain) + _fg(clip(agent, max(1, room - 2 - width(arrow))), INK, plain) + _fg(arrow, ACCENT, plain)
    else:
        third = _fg("\u25cb No agent assigned", MUTED, plain)
    fourth = ""
    used = 0
    for text, color in meta_text(task, files):
        sep = " \u00b7 " if fourth else ""
        if used + width(sep) + width(text) > room:
            text = clip(text, max(0, room - used - width(sep)))
            if not text:
                break
        fourth += _fg(sep, MUTED, plain) + _fg(text, color, plain)
        used += width(sep) + width(text)
    return [head, second, third, fourth]


def card_block(lines, cells, selected, plain, padded=False):
    """Card lines on `cells` columns: an accent bar and fill when selected, a bar-width margin
    otherwise, so the text never shifts. `padded` adds a blank line above and below."""
    rows = []
    for line in [""] * padded + lines + [""] * padded:
        bar = _fg(BAR, ACCENT, plain) if selected else " "
        row = pad(bar + " " + line, cells)
        rows.append(_on(row, SELECTED_BG, plain) if selected else row)
    return rows


def lane_header(key, label, count, cells, plain):
    head = _fg(COLUMN_GLYPH[key], glyph_color(key), plain) + " " + _fg(label, glyph_color(key), plain, bold=True) + " " + _fg(str(count), MUTED, plain)
    return [pad(" " + clip(head, cells - 1), cells), rule(cells, plain)]


def lane_rows(cards, selected_id, cells, room, plain, start, facts, padded=False):
    """(rows, start): the cards of one lane that fit `room` rows as (text, task id) pairs.

    Cards are whole or absent. When some are cut, the last row says how many are above and
    below, and the window keeps the selected card in view without jumping when it does not move.
    """
    if not cards:
        return [(pad(" " + _fg("No tasks", MUTED, plain), cells), None)], 0
    each = CARD_ROWS + 2 * padded
    fits = max(1, (room + 1) // each)
    marker = len(cards) > fits
    if marker:
        fits = max(1, room // each)
    index = next((i for i, task in enumerate(cards) if task.get("id") == selected_id), None)
    start = max(0, min(start, len(cards) - fits))
    if index is not None:
        if index < start:
            start = index
        elif index >= start + fits:
            start = index - fits + 1
    shown = cards[start : start + fits]
    rows = []
    for offset, task in enumerate(shown):
        selected = task.get("id") == selected_id
        live, files = facts(task)
        for text in card_block(card_lines(task, cells - 2, selected, plain, live, files), cells, selected, plain, padded):
            rows.append((text, task.get("id")))
        if offset < len(shown) - 1:
            rows.append((rule(cells, plain), None))
    if marker:
        rows.append((more_row(start, len(cards) - start - fits, cells, plain), None))
    return rows, start


def more_row(above, below, cells, plain):
    words = " \u00b7 ".join(
        word for word in (f"\u25b2 {above} above" if above else "", f"\u25bc {below} below" if below else "") if word
    )
    return pad(" " + _fg(words, MUTED, plain), cells)


def done_row(task, selected, cells, plain):
    """One line for a finished task under an opened Done strip."""
    ident = sanitize(task.get("id") or "")
    title = sanitize(task.get("title") or "") or ident
    project = sanitize(task.get("project") or "")
    text = _fg("\u2713 ", GREEN, plain) + _fg(clip(ident, max(6, cells // 4)), MUTED, plain) + "  "
    text += _fg(clip(title, max(1, cells - width(text) - 3 - (width(project) + 3 if project else 0))), BRIGHT if selected else INK, plain, bold=selected)
    if project:
        text += _fg(" \u00b7 ", MUTED, plain) + _fg(clip(project, max(1, cells - width(text) - 3)), PINK, plain)
    bar = _fg(BAR, ACCENT, plain) if selected else " "
    row = pad(bar + text, cells)
    return _on(row, SELECTED_BG, plain) if selected else row


def window(rows, selected, room):
    """(shown, above, below): `rows` cut to `room` rows (one of them left for a `more_row`
    when something is cut), keeping row index `selected` visible."""
    if len(rows) <= room:
        return rows, 0, 0
    keep = max(1, room - 1)
    start = 0 if selected is None or selected < keep else selected - keep + 1
    return rows[start : start + keep], start, len(rows) - start - keep


def join_lanes(lanes, cells, plain):
    """Rows of side-by-side lanes: (text, spans) where spans are (first column, last column,
    task id, lane key) in 1-based screen columns."""
    height = max(len(rows) for _, rows in lanes)
    out = []
    divider = " " + _fg(DIVIDER, BORDER, plain) + " "
    for offset in range(height):
        parts, spans, column = [], [], 2
        for index, (key, rows) in enumerate(lanes):
            text, task_id = rows[offset] if offset < len(rows) else (" " * cells[index], None)
            parts.append(pad(text, cells[index]))
            spans.append((column, column + cells[index] - 1, task_id, key))
            column += cells[index] + 3
        out.append((" " + divider.join(parts), spans))
    return out


def spacious(counts, room):
    """True when every lane's cards fit with a blank line above and below each."""
    return all(count * (CARD_ROWS + 2) - 1 <= room for count in counts)


def lane_widths(cols, count):
    """Cells of each lane: one margin on each side, three cells between lanes."""
    room = cols - 2 - 3 * (count - 1)
    base, extra = divmod(room, count)
    return [base + (1 if index < extra else 0) for index in range(count)]
