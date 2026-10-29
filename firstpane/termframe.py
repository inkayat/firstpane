"""Decode a Herdr terminal.frame into Firstpane rows.

The vocabulary is the one Herdr 0.9.0–0.9.5 actually sends (CSI cursor, reset-form SGR,
OSC 8, kitty graphics). Anything else is an error so the caller can resync, not a guess.
"""

from firstpane.cells import char_width


class TermGrid:
    def __init__(self):
        self.width = 0
        self.height = 0
        self.cells = []
        self.row = 0
        self.col = 0
        self.sgr = "0"
        self.visible = True

    def apply(self, data, full, width, height):
        if width != self.width or height != self.height:
            self._alloc(width, height)
        elif full:
            self._blank()
        if not data:
            return None
        try:
            self._feed(data.decode("utf-8"))
        except UnicodeDecodeError:
            return "unsupported"
        except ValueError:
            return "unsupported"
        return None

    def rows(self, show_cursor):
        lines = []
        for r, cells in enumerate(self.cells):
            parts = []
            current = None
            for c, (text, sgr) in enumerate(cells):
                shown = text
                if show_cursor and self.visible and r == self.row and c == self.col and shown:
                    shown = shown
                    sgr = "7"
                if sgr != current:
                    if current is not None:
                        parts.append("\x1b[0m")
                    parts.append(f"\x1b[{sgr}m")
                    current = sgr
                parts.append(shown or " ")
            if current is not None:
                parts.append("\x1b[0m")
            lines.append("".join(parts))
        return lines

    def _alloc(self, width, height):
        self.width = max(1, width)
        self.height = max(1, height)
        self._blank()

    def _blank(self):
        self.cells = [[(" ", "0") for _ in range(self.width)] for _ in range(self.height)]
        self.row = self.col = 0
        self.sgr = "0"

    def _put(self, text):
        for char in text:
            wide = char_width(char)
            if wide == 0:
                if self.col > 0:
                    prev, sgr = self.cells[self.row][self.col - 1]
                    self.cells[self.row][self.col - 1] = (prev + char, sgr)
                continue
            if self.row >= self.height or self.col >= self.width:
                raise ValueError("outside")
            self.cells[self.row][self.col] = (char, self.sgr)
            if wide == 2:
                if self.col + 1 >= self.width:
                    raise ValueError("outside")
                self.cells[self.row][self.col + 1] = ("", self.sgr)
            self.col += wide

    def _feed(self, text):
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\x1b":
                index = self._escape(text, index)
                continue
            if ord(char) < 32:
                raise ValueError("c0")
            self._put(char)
            index += 1

    def _escape(self, text, index):
        if index + 1 >= len(text):
            raise ValueError("short")
        nxt = text[index + 1]
        if nxt == "[":
            return self._csi(text, index + 2)
        if nxt == "]":
            return self._osc(text, index + 2)
        if nxt == "_":
            return self._kitty(text, index + 2)
        raise ValueError("esc")

    def _csi(self, text, index):
        start = index
        while index < len(text) and text[index] in "0123456789:;<=>? ":
            index += 1
        if index >= len(text):
            raise ValueError("csi")
        params = text[start:index]
        final = text[index]
        if len(params) > 64:
            raise ValueError("csi")
        if final == "H" and params.count(";") <= 1 and "?" not in params:
            row, _, col = (params or "1;1").partition(";")
            self.row = max(0, int(row or "1") - 1)
            self.col = max(0, int(col or "1") - 1)
            if self.row >= self.height or self.col >= self.width:
                raise ValueError("outside")
            return index + 1
        if final == "J" and params == "2":
            self._blank()
            return index + 1
        if final == "m" and "?" not in params:
            self.sgr = params or "0"
            return index + 1
        if final in "hl" and params in ("?2026", "?25"):
            if params == "?25":
                self.visible = final == "h"
            return index + 1
        if final == "q" and params.endswith(" "):
            return index + 1
        raise ValueError("csi")

    def _osc(self, text, index):
        # OSC 8 hyperlinks are dropped. Other OSC is unsupported.
        if not text.startswith("8;", index):
            raise ValueError("osc")
        end = text.find("\x1b\\", index)
        bel = text.find("\x07", index)
        if end == -1 and bel == -1:
            raise ValueError("osc")
        if bel != -1 and (end == -1 or bel < end):
            return bel + 1
        return end + 2

    def _kitty(self, text, index):
        end = text.find("\x1b\\", index)
        if end == -1:
            raise ValueError("kitty")
        return end + 2
