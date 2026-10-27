"""Terminal input decoding. Mouse clicks and pastes are data, not commands.

Besides keys, mouse and paste, the pane's terminal answers Firstpane's own queries in this
stream: the cell size (`CSI 6;h;w t`), DECRQM mode reports (`CSI ? mode;value $ y`) and kitty
graphics replies (`ESC _G ... ESC \\`). While the page in the Browser tab has the keyboard,
Firstpane asks for kitty keyboard reporting, so modified keys arrive as `CSI code;mods u`; keys
with modifiers are named `ctrl-`, `alt-`, `super-`, `shift-` + key, in that order.
In SGR-pixel mouse mode (`pixel_cell` set to the cell size) mouse events carry their pixel
position as a last `(x, y)` element and their cell is computed from it. Motion with no button
held (any-event tracking, on while the Browser tab shows the page) is a `hover` event.
"""

import codecs
import re

_MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([mM])")
_CSI = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]")
_CSI_U = re.compile(r"\x1b\[(\d+)(?::[\d:]*)?(?:;(\d+)(?::\d+)?)?(?:;[\d:]*)?u")
_CELL = re.compile(r"\x1b\[6;(\d+);(\d+)t")
_MODE = re.compile(r"\x1b\[\?(\d+);(\d+)\$y")
_APC = re.compile(r"\x1b_G([^;\x1b]*);([^\x1b]*)\x1b\\")
_SS3 = re.compile(r"\x1bO[A-Za-z]")
_FINALS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "backtab"}
_TILDES = {"1": "home", "7": "home", "4": "end", "8": "end", "3": "delete", "5": "pageup", "6": "pagedown"}
_CODEPOINTS = {27: "esc", 13: "enter", 9: "tab", 127: "backspace", 8: "backspace"}
MOD_NAMES = (("ctrl", 4), ("alt", 2), ("super", 8), ("shift", 1))
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"
PASTE_LIMIT = 1 << 20
REPLY_LIMIT = 4096


class InputDecoder:
    """Decodes terminal input across reads: split UTF-8, unterminated pastes and unterminated
    graphics replies wait for the rest."""

    def __init__(self):
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._held = ""
        self.pixel_cell = None

    def feed(self, data):
        if data is None:
            return []
        if isinstance(data, bytes):
            data = self._utf8.decode(data)
        text = self._held + data
        self._held = ""
        events = []
        index = 0
        while index < len(text):
            if text.startswith(PASTE_START, index):
                end = text.find(PASTE_END, index + len(PASTE_START))
                if end < 0:
                    if len(text) - index > PASTE_LIMIT:
                        events.append(("paste", text[index + len(PASTE_START) :]))
                        return events
                    self._held = text[index:]
                    return events
                events.append(("paste", text[index + len(PASTE_START) : end]))
                index = end + len(PASTE_END)
                continue
            if text.startswith("\x1b_G", index):
                reply = _APC.match(text, index)
                if reply:
                    events.append(_graphics(reply))
                    index = reply.end()
                    continue
                if "\x1b\\" not in text[index:] and len(text) - index < REPLY_LIMIT:
                    self._held = text[index:]
                    return events
            if text.startswith("\x1b[<", index):
                match = _MOUSE.match(text, index)
                if not match:
                    events.append(("key", "esc"))
                    index += 1
                    continue
                events.extend(_mouse(match, self.pixel_cell))
                index = match.end()
                continue
            for pattern, build in ((_CELL, _cell), (_MODE, _mode), (_CSI_U, _csi_u)):
                match = pattern.match(text, index)
                if match:
                    event = build(match)
                    if event:
                        events.append(event)
                    index = match.end()
                    break
            else:
                csi = _CSI.match(text, index)
                if csi:
                    name = _csi_key(csi.group(0))
                    if name:
                        events.append(("key", name))
                    index = csi.end()
                    continue
                ss3 = _SS3.match(text, index)
                if ss3:
                    name = _FINALS.get(ss3.group(0)[-1])
                    if name:
                        events.append(("key", name))
                    index = ss3.end()
                    continue
                events.append(("key", _char_key(text[index])))
                index += 1
        return events


def _graphics(match):
    keys = dict(part.split("=", 1) for part in match.group(1).split(",") if "=" in part)
    try:
        image_id = int(keys.get("i", "0"))
    except ValueError:
        image_id = 0
    return ("graphics", image_id, match.group(2))


def _cell(match):
    return ("cell", int(match.group(2)), int(match.group(1)))


def _mode(match):
    return ("mode", int(match.group(1)), int(match.group(2)))


def with_mods(name, mods):
    """`name` prefixed with the modifiers in the CSI modifier parameter `mods` (1 = none)."""
    bits = max(0, mods - 1)
    return "".join(f"{label}-" for label, bit in MOD_NAMES if bits & bit) + name


def _csi_u(match):
    code = int(match.group(1))
    mods = int(match.group(2) or 1)
    name = _CODEPOINTS.get(code)
    if name is None:
        if code < 32 or code > 0x10FFFF or 0xE000 <= code <= 0xF8FF:
            return None
        name = chr(code)
    return ("key", with_mods(name, mods))


def _mouse(match, pixel_cell=None):
    button = int(match.group(1))
    column = int(match.group(2))
    row = int(match.group(3))
    pressed = match.group(4) == "M"
    extra = ()
    if pixel_cell:
        x, y = max(0, column - 1), max(0, row - 1)
        column, row = x // pixel_cell[0] + 1, y // pixel_cell[1] + 1
        extra = ((x, y),)
    if button & 64:
        if pressed and button & 3 in (0, 1):
            return [("wheel", "up" if button & 3 == 0 else "down", column, row, *extra)]
        return []
    if button & 32:
        if pressed and button & 3 == 0:
            return [("drag", column, row, *extra)]
        return [("hover", column, row, *extra)] if pressed and button & 3 == 3 else []
    if button & 4:
        return [("shift-click", column, row, *extra)] if pressed and button & 3 == 0 else []
    return [("mouse", button & 3, column, row, pressed, *extra)]


def _csi_key(sequence):
    final = sequence[-1]
    params = sequence[2:-1].split(";")
    mods = int(params[1]) if len(params) == 2 and params[1].isdigit() else 1
    if final == "~":
        name = _TILDES.get(params[0])
    else:
        name = _FINALS.get(final)
    return with_mods(name, mods) if name else None


def _char_key(char):
    if char in ("\r", "\n"):
        return "enter"
    if char == "\x1b":
        return "esc"
    if char == "\t":
        return "tab"
    if char in ("\x7f", "\x08"):
        return "backspace"
    code = ord(char)
    if 1 <= code <= 26:
        return "ctrl-" + chr(code + 96)
    return char


def decode_input(text):
    """Turn one complete chunk of terminal input into key, paste, mouse, wheel and reply events."""
    return InputDecoder().feed(text)
