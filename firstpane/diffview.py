"""Shared diff motions. Pure functions over a diff_view dict. No I/O.

The diff is line-based, so v and V both select a contiguous line range. There is no language
server: gd and gI report that, and do not move. Ctrl-o pops a jump the caller pushed.
"""

SCROLL_STEP = 8
NO_SYMBOL = "no symbol support"


def push_jump(view, frame):
    view.setdefault("jumps", []).append(tuple(frame))


def _line(view):
    lines = view.get("lines") or []
    if not lines:
        return ""
    cursor = min(max(view.get("cursor") or 0, 0), len(lines) - 1)
    line = lines[cursor]
    return getattr(line, "text", "") or ""


def _words(text):
    starts = []
    in_word = False
    for index, char in enumerate(text):
        word = char.isalnum() or char == "_"
        if word and not in_word:
            starts.append(index)
        in_word = word
    return starts


def _clamp_cursor(view):
    last = max(0, len(view.get("lines") or []) - 1)
    view["cursor"] = min(max(view.get("cursor") or 0, 0), last)
    return last


def _set_col(view, col):
    text = _line(view)
    col = min(max(col, 0), len(text))
    view["col"] = col
    view["xoff"] = col


def apply_key(view, key, rows=24):
    """Apply one viewport key. Returns handled, status, and a restored jump frame."""
    view.setdefault("xoff", 0)
    view.setdefault("col", view.get("xoff") or 0)
    view.setdefault("cursor", 0)
    result = {"handled": False, "status": None, "jumped": None}
    if view.get("prefix") == "g":
        view["prefix"] = None
        result["handled"] = True
        if key == "g":
            view["cursor"] = 0
        elif key in ("d", "I"):
            result["status"] = NO_SYMBOL
        return result
    if key == "g":
        view["prefix"] = "g"
        result["handled"] = True
        return result
    if key == "ctrl-o":
        jumps = view.get("jumps") or []
        result["handled"] = True
        if not jumps:
            result["status"] = "no earlier position"
            return result
        result["jumped"] = jumps.pop()
        view["jumps"] = jumps
        return result
    last = _clamp_cursor(view)
    page = max(1, rows // 2)
    if key in ("j", "down", "k", "up", "shift-down", "shift-up", "ctrl-d", "ctrl-u", "pagedown", "pageup"):
        if key.startswith("shift-") and view.get("mark") is None:
            view["mark"] = view["cursor"]
        step = page if key in ("ctrl-d", "pagedown") else -page if key in ("ctrl-u", "pageup") else 0
        if key in ("j", "down", "shift-down"):
            step = 1
        elif key in ("k", "up", "shift-up"):
            step = -1
        view["cursor"] = min(max(view["cursor"] + step, 0), last)
        result["handled"] = True
        return result
    if key == "G":
        view["cursor"] = last
        result["handled"] = True
        return result
    if key in ("v", "V"):
        if key == "v" and view.get("mark") is not None:
            view["mark"] = None
        elif view.get("mark") is None:
            view["mark"] = view["cursor"]
        result["handled"] = True
        return result
    if key == "esc" and (view.get("mark") is not None or view.get("prefix")):
        view["mark"] = None
        view["prefix"] = None
        result["handled"] = True
        return result
    text = _line(view)
    if key in ("h", "left", "l", "right"):
        step = -SCROLL_STEP if key in ("h", "left") else SCROLL_STEP
        view["xoff"] = max(0, view.get("xoff", 0) + step)
        view["col"] = view["xoff"]
        result["handled"] = True
        return result
    if key == "0":
        _set_col(view, 0)
        result["handled"] = True
        return result
    if key == "$":
        _set_col(view, len(text))
        result["handled"] = True
        return result
    if key in ("w", "b"):
        col = view.get("col") or 0
        starts = _words(text)
        if key == "w":
            nxt = next((start for start in starts if start > col), col)
        else:
            prev = [start for start in starts if start < col]
            nxt = prev[-1] if prev else 0
        _set_col(view, nxt)
        result["handled"] = True
        return result
    return result
