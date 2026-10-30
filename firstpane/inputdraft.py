"""Paste a confirmed message into the FirstMate primary's input box, unsent.

The primary is the process holding `<firstmate_home>/state/.lock` and the one pane in this
Herdr session that runs it. Only a pi box is filled: the paste is one bracketed paste through
`pane.send_text`, with every control removed, so it cannot end the paste or press Enter.
Working or idle is not a condition. Existing box text is left in place and the draft is
appended. A dialog, a scrolled pane, or the wrong recipient is refused before anything is sent.
"""

import re
import time
from pathlib import Path

from firstpane.sanitize import sanitize

SUPPORTED = ("pi",)
TIMEOUT = 3
POLLS = 10
POLL_SECONDS = 0.1
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
MARKER = re.compile(r"\[paste #\d+ (?:\+(\d+) lines|(\d+) chars)\]")
UNREACHABLE = "Herdr unreachable"


def lock_pid(home):
    try:
        text = (Path(home) / "state" / ".lock").read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def paste_text(body):
    """Paste content: LF line ends, no control or escape that could submit or end the paste."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    return sanitize(text).rstrip("\n")


def _pi_kept(text):
    clean = (text or "").replace("\t", "    ")
    return "".join(char for char in clean if char == "\n" or ord(char) >= 32)


def _collapsed(text):
    kept = _pi_kept(text)
    rows = kept.split("\n")
    units = len(kept.encode("utf-16-le")) // 2
    if len(rows) > 10:
        return f"+{len(rows)} lines"
    if units > 1000:
        return f"{units} chars"
    return None


def _border(line):
    """Hidden-line count for a pi editor rule, including the Working rule, or None."""
    if len(line) < 10 or line.count("\u2500") < 8 or line.count("\u2500") < len(line) // 2:
        return None
    match = re.search(r"([\u2191\u2193]) (\d+) more", line)
    return int(match.group(2)) if match else 0


def composer_box(screen):
    """(lines, hidden above, hidden below) of pi's editor, or None when a dialog replaced it."""
    lines = [line.rstrip() for line in (screen or "").split("\n")]
    borders = [index for index, line in enumerate(lines) if _border(line) is not None]
    if len(borders) < 2:
        return None
    top, bottom = borders[-2], borders[-1]
    if "\u2193" in lines[top] or "\u2191" in lines[bottom]:
        return None
    if not any(line.strip() for line in lines[bottom + 1:]):
        return None
    return lines[top + 1:bottom], _border(lines[top]), _border(lines[bottom])


def box_text(box):
    lines, _above, _below = box
    if lines == [""]:
        return ""
    return "\n".join(lines)


def _drawing_at_end(visible, pasted):
    """True when `visible` already ends with what pasting `pasted` into pi draws."""
    kind = _collapsed(pasted)
    if kind:
        match = MARKER.search(visible)
        if not match:
            return False
        got = f"+{match.group(1)} lines" if match.group(1) else f"{match.group(2)} chars"
        return got == kind and visible.endswith(match.group(0))
    kept = _pi_kept(pasted)
    return bool(kept) and (visible == kept or visible.endswith(kept))


def _shows_append(box, before, pasted):
    """True when the visible editor is `before` plus the paste, and the cursor end is on screen."""
    lines, above, below = box
    if below or above:
        return False
    visible = box_text(box)
    kind = _collapsed(pasted)
    if kind:
        if not visible.endswith("]") or MARKER.search(visible) is None:
            return False
        match = MARKER.search(visible)
        prefix = visible[: match.start()]
        got = f"+{match.group(1)} lines" if match.group(1) else f"{match.group(2)} chars"
        # A screen read strips a trailing space from `before`; the text before the marker keeps it.
        return prefix.rstrip(" ") == before and got == kind
    kept = _pi_kept(pasted)
    expected = before + kept
    # A screen read strips a space that was at the end of the previous line.
    spaced = before + " " + kept if before else expected
    return visible == expected or visible == spaced


class InputDraft:
    def __init__(self, transport, home, sleep=time.sleep):
        self.transport = transport
        self.home = home
        self.sleep = sleep

    def _call(self, method, params):
        try:
            response = self.transport.socket_call(method, params, TIMEOUT)
        except (OSError, ValueError):
            return None, UNREACHABLE
        if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
            error = response.get("error") if isinstance(response, dict) else None
            code = error.get("code") if isinstance(error, dict) else "bad reply"
            return None, f"Herdr refused {method}: {sanitize(code)}"
        return response["result"], None

    def _pids(self, pane_id):
        result, refusal = self._call("pane.process_info", {"pane_id": pane_id})
        if refusal == UNREACHABLE:
            return None, refusal
        if refusal:
            return set(), None
        info = (result or {}).get("process_info") or {}
        pids = {info.get("shell_pid")}
        pids.update(process.get("pid") for process in info.get("foreground_processes") or [] if isinstance(process, dict))
        return pids, None

    def locate(self):
        """(target, None) for the lock owner's pane, or (None, why not)."""
        if not self.home:
            return None, "FirstMate not configured"
        pid = lock_pid(self.home)
        if pid is None:
            return None, f"FirstMate is not running: no session lock in {self.home}/state"
        result, refusal = self._call("session.snapshot", {})
        if refusal:
            return None, refusal
        panes = (result.get("snapshot") or {}).get("panes") or []
        matches = []
        for row in panes:
            if not isinstance(row, dict) or not row.get("agent"):
                continue
            pids, refusal = self._pids(row.get("pane_id"))
            if refusal:
                return None, refusal
            if pid in pids:
                matches.append(row)
        if not matches:
            return None, f"FirstMate (pid {pid}) is not open in this Herdr session"
        if len(matches) > 1:
            return None, f"FirstMate (pid {pid}) matches more than one pane"
        row = matches[0]
        if row.get("agent") not in SUPPORTED:
            return None, f"FirstMate runs {sanitize(row.get('agent'))}: only a pi input box can be filled"
        target = {name: row.get(name) for name in ("pane_id", "terminal_id", "workspace_id", "tab_id", "agent")}
        target["pid"] = pid
        return target, None

    def _screen(self, pane_id):
        result, refusal = self._call("pane.read", {"pane_id": pane_id, "source": "visible"})
        if refusal:
            return None, refusal
        return (result.get("read") or {}).get("text"), None

    def _ready(self, target):
        """Why the live input box cannot take a paste, or None. Busy is not a reason."""
        result, refusal = self._call("pane.get", {"pane_id": target["pane_id"]})
        if refusal:
            return refusal, None
        row = result.get("pane") or {}
        if (row.get("terminal_id"), row.get("agent")) != (target["terminal_id"], target["agent"]):
            return "FirstMate's pane changed since the preview", None
        pids, refusal = self._pids(target["pane_id"])
        if refusal:
            return refusal, None
        if target["pid"] not in pids:
            return "FirstMate's pane changed since the preview", None
        if (row.get("scroll") or {}).get("offset_from_bottom"):
            return "FirstMate's pane is scrolled back; the input box is not visible", None
        screen, refusal = self._screen(target["pane_id"])
        if refusal:
            return refusal, None
        box = composer_box(screen)
        if box is None:
            return "FirstMate's input box is not showing (a dialog or menu is open)", None
        _lines, above, below = box
        if above or below:
            return "FirstMate's input box is not fully visible; the draft stays here", None
        return None, box

    def transfer(self, target, body):
        """(outcome, detail): transferred, refused (nothing pasted), or unconfirmed."""
        current, refusal = self.locate()
        if refusal:
            return "refused", refusal
        if (current["pane_id"], current["terminal_id"]) != (target["pane_id"], target["terminal_id"]):
            return "refused", "FirstMate's pane changed since the preview"
        refusal, box = self._ready(target)
        if refusal:
            return "refused", refusal
        text = paste_text(body)
        if not text:
            return "refused", "nothing to paste"
        before = box_text(box)
        if _drawing_at_end(before, text):
            return "transferred", "already in FirstMate's input box; not pasted again"
        try:
            response = self.transport.socket_call(
                "pane.send_text",
                {"pane_id": target["pane_id"], "text": PASTE_START + text + PASTE_END},
                TIMEOUT,
            )
        except (OSError, ValueError):
            return "unconfirmed", "the paste may have reached FirstMate; look at its input box before trying again"
        if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
            error = response.get("error") if isinstance(response, dict) else None
            code = error.get("code") if isinstance(error, dict) else "bad reply"
            return "refused", f"Herdr refused the paste: {sanitize(code)}"
        for _ in range(POLLS):
            screen, refusal = self._screen(target["pane_id"])
            seen = composer_box(screen) if not refusal else None
            if seen is not None and _shows_append(seen, before, text):
                return "transferred", "in FirstMate's input box; not sent. Press Enter there to send"
            self.sleep(POLL_SECONDS)
        return "unconfirmed", "FirstMate's input box does not show the draft; look there before trying again"
