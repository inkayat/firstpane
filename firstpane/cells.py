"""Terminal cell widths. Combining marks take no cell; wide CJK takes two; SGR takes none."""

import unicodedata

RESET = "\x1b[0m"


def char_width(char):
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def width(text):
    total = 0
    index = 0
    while index < len(text):
        if text.startswith("\x1b[", index):
            end = text.find("m", index)
            if end == -1:
                break
            index = end + 1
            continue
        total += char_width(text[index])
        index += 1
    return total


def fit(text, cols):
    """Cut `text` to `cols` cells, keeping SGR codes; reset color if a painted run was cut."""
    out = []
    used = 0
    index = 0
    painted = False
    while index < len(text):
        if text.startswith("\x1b[", index):
            end = text.find("m", index)
            if end == -1:
                break
            code = text[index : end + 1]
            out.append(code)
            painted = code != RESET
            index = end + 1
            continue
        step = char_width(text[index])
        if used + step > cols:
            if painted:
                out.append(RESET)
            break
        out.append(text[index])
        used += step
        index += 1
    return "".join(out)


def tail(text, cols):
    """The last `cols` cells of plain `text`."""
    out = []
    used = 0
    for char in reversed(text):
        step = char_width(char)
        if used + step > cols:
            break
        out.append(char)
        used += step
    return "".join(reversed(out))


def spread(left, right, cols):
    """`left` and `right` on one line, right-aligned when there is room."""
    gap = cols - width(left) - width(right)
    if gap < 1:
        return fit(left + "  " + right, cols)
    return left + " " * gap + right


def wrap(text, cols):
    """Split `text` into lines of at most `cols` cells; SGR state carries across breaks."""
    cols = max(1, cols)
    lines, current, used, active = [], [], 0, ""
    index = 0
    while index < len(text):
        if text.startswith("\x1b[", index):
            end = text.find("m", index)
            if end == -1:
                break
            code = text[index : end + 1]
            current.append(code)
            active = "" if code == RESET else code
            index = end + 1
            continue
        step = char_width(text[index])
        if used + step > cols and used:
            lines.append("".join(current) + (RESET if active else ""))
            current, used = ([active] if active else []), 0
        current.append(text[index])
        used += step
        index += 1
    lines.append("".join(current))
    return lines


def clip(text, cols):
    """`text` in at most `cols` cells; a cut ends in an ellipsis."""
    if width(text) <= cols:
        return text
    if cols < 1:
        return ""
    return fit(text, cols - 1) + "\u2026"


def pad(text, cols):
    """`text` followed by spaces up to `cols` cells."""
    return text + " " * max(0, cols - width(text))
