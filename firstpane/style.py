"""Status and project colors. Color never replaces the status word."""

import hashlib

PALETTE = ("96", "94", "95", "93", "92", "91")
STATUS_SGR = {
    "working": "33",
    "blocked": "1;31",
    "done": "32",
    "idle": "",
    "unknown": "35",
    "unobserved": "2",
    "stale": "2",
    "unproven": "35",
}


def no_color(env):
    return bool(env.get("NO_COLOR"))


def project_sgr(project_key):
    digest = hashlib.sha256(project_key.encode()).digest()
    return PALETTE[int.from_bytes(digest[:4], "big") % len(PALETTE)]


def paint(text, sgr, enabled_no_color):
    if enabled_no_color or not sgr:
        return text
    return f"\x1b[{sgr}m{text}\x1b[0m"


# Agents tab surface. Every tab paints onto these; color never replaces a word or glyph.
BG = (16, 22, 35)
SELECTED_BG = (27, 44, 82)
FIELD_BG = (21, 30, 49)
BORDER = (52, 68, 104)
INK = (226, 233, 248)
BRIGHT = (254, 255, 255)
MUTED = (125, 142, 186)
ACCENT = (130, 195, 247)
PINK = (244, 143, 201)
YELLOW = (238, 203, 83)
GREEN = (110, 231, 183)
VIOLET = (200, 130, 230)
SEND_BG = (95, 145, 246)
# Diff bands: dark enough to sit on BG, still readable as added or deleted.
ADD_BG = (28, 62, 42)
DEL_BG = (72, 30, 42)
ADD_PICK_BG = (34, 78, 52)
DEL_PICK_BG = (88, 36, 48)
HUNK_BG = (24, 32, 50)
HUNK_GUTTER_BG = (12, 18, 30)
MENU_BG = (20, 28, 44)


def rgb_fg(color, bold=False):
    return ("1;" if bold else "") + "38;2;%d;%d;%d" % color


def rgb_bg(color):
    return "48;2;%d;%d;%d" % color


def frame_lines(lines, cols, plain):
    """Each line on the Agents surface. `NO_COLOR` drops color and does not change the words.

    Lines are not padded to `cols`: the pane writer fills the rest of each row with this
    background, so a preview keeps the characters that will be sent.
    """
    if cols < 1:
        return ""
    if plain:
        return "\n".join(lines)
    code = "\x1b[%s;%sm" % (rgb_fg(INK), rgb_bg(BG))
    return "\n".join(code + line.replace("\x1b[0m", "\x1b[0m" + code) + "\x1b[0m" for line in lines)
