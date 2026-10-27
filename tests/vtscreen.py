"""A minimal terminal screen for the escape sequences Firstpane writes. Test utility only."""

import re
import unicodedata

_CSI = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")
_OTHER = re.compile(r"\x1b[\]_P^X].*?(?:\x07|\x1b\\)", re.S)


class Screen:
    def __init__(self, rows, cols):
        self.resize(rows, cols)
        self.modes = {}
        self.pending = ""

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0

    def feed(self, data):
        text = self.pending + data
        self.pending = ""
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\x1b":
                match = _CSI.match(text, index)
                if match:
                    self._csi(match.group(1), match.group(3))
                    index = match.end()
                    continue
                other = _OTHER.match(text, index)
                if other:
                    index = other.end()
                    continue
                self.pending = text[index:]
                return
            if char == "\n":
                self.row = min(self.rows - 1, self.row + 1)
                self.col = 0
            elif char == "\r":
                self.col = 0
            elif unicodedata.combining(char):
                pass
            elif char >= " ":
                if self.row < self.rows and self.col < self.cols:
                    self.grid[self.row][self.col] = char
                self.col += 1
            index += 1

    def _csi(self, params, final):
        if final == "H":
            parts = [int(p) if p else 1 for p in params.split(";")] if params else [1, 1]
            self.row = min(self.rows, max(1, parts[0])) - 1
            self.col = min(self.cols, max(1, parts[1] if len(parts) > 1 else 1)) - 1
        elif final == "J" and params == "2":
            self.grid = [[" "] * self.cols for _ in range(self.rows)]
        elif final == "K" and params == "2":
            self.grid[self.row] = [" "] * self.cols
        elif final in "hl" and params.startswith("?"):
            for mode in params[1:].split(";"):
                self.modes[mode] = final == "h"

    def lines(self):
        return ["".join(row).rstrip() for row in self.grid]

    def text(self):
        return "\n".join(self.lines())
