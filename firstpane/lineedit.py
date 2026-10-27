"""Editable text with a cursor, for the inline message editor and one-line prompts."""

from firstpane.sanitize import sanitize


class LineEdit:
    def __init__(self, text="", multiline=True):
        self.multiline = multiline
        self.text = ""
        self.cursor = 0
        self.insert(text)

    def set(self, text):
        self.text = ""
        self.cursor = 0
        self.insert(text)

    def insert(self, text, paste=False):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        text = sanitize(text)
        if not self.multiline:
            text = text.replace("\n", " ")
        if not paste and len(text) == 1 and text not in ("\n", "\t") and not text.isprintable():
            return
        self.text = self.text[: self.cursor] + text + self.text[self.cursor :]
        self.cursor += len(text)

    def key(self, name):
        """Apply one editing key; returns True when the key was an editing key."""
        if name == "backspace":
            if self.cursor:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                self.cursor -= 1
        elif name == "delete":
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
        elif name == "left":
            self.cursor = max(0, self.cursor - 1)
        elif name == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif name in ("home", "ctrl-a"):
            self.cursor = self.text.rfind("\n", 0, self.cursor) + 1
        elif name in ("end", "ctrl-e"):
            end = self.text.find("\n", self.cursor)
            self.cursor = len(self.text) if end < 0 else end
        elif name == "ctrl-u":
            start = self.text.rfind("\n", 0, self.cursor) + 1
            self.text = self.text[:start] + self.text[self.cursor :]
            self.cursor = start
        else:
            return False
        return True
