"""Turn untrusted text into terminal-inert display text."""

import re

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_BIDI = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def sanitize(text):
    """Drop terminal controls. Bidi overrides become the visible token <bidi>."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = text.replace("\x1b", "")
    out = []
    for char in text:
        if char in "\t\n":
            out.append(char)
            continue
        code = ord(char)
        if code < 32 or code == 127 or 0x80 <= code <= 0x9F:
            continue
        if char in _BIDI:
            out.append("<bidi>")
            continue
        out.append(char)
    return "".join(out)
