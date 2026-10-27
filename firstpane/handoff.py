"""Fill the FirstMate primary's own message box with a message, unsent; the captain presses Enter.

The primary is the process holding `<firstmate_home>/state/.lock` (FirstMate's session lock)
and the one pane in this Herdr session that runs it. Only a pi primary is supported: the
paste and the box check below follow pi-tui's editor. The message is one bracketed paste
through `pane.send_text` with every control removed, so it can neither end the paste early nor
press a key; there is no Enter. Nothing is pasted unless the box is visibly empty, and the
transfer is reported only when the box is then seen holding the message.
"""

import re
import time
from pathlib import Path

from firstpane.sanitize import sanitize

SUPPORTED = ("pi",)
READY = ("idle", "done")
TIMEOUT = 3
POLLS = 10
POLL_SECONDS = 0.1
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
BORDER = re.compile(r"\u2500+(?: ([\u2191\u2193]) (\d+) more \u2500*)?")
MARKER = re.compile(r"\[paste #\d+ (?:\+(\d+) lines|(\d+) chars)\]")
UNREACHABLE = "Herdr unreachable"


def lock_pid(home):
    try:
        text = (Path(home) / "state" / ".lock").read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def paste_text(body):
    """The message as paste content: LF line ends, no control or escape character at all."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    return sanitize(text).rstrip("\n")


def _border(line):
    """None, or the lines a pi editor border says are scrolled past it (0 for a plain rule)."""
    match = BORDER.fullmatch(line) if len(line) >= 10 else None
    return (int(match.group(2)) if match.group(2) else 0) if match else None


def composer_box(screen):
    """(lines, hidden above, hidden below) of pi's editor - between its last two borders,
    with the footer below - or None when the screen does not show that editor (a dialog,
    menu or other program)."""
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


def _pi_kept(text):
    clean = text.replace("\t", "    ")
    return "".join(char for char in clean if char == "\n" or ord(char) >= 32)


def shows(box, text):
    """True when pi's editor box is what pasting `text` into an empty box draws: the whole
    text, its `[paste #N ...]` marker, or - scrolled to the cursor at its end - its tail."""
    lines, above, below = box
    kept = _pi_kept(text)
    rows = kept.split("\n")
    units = len(kept.encode("utf-16-le")) // 2
    drawn = "".join(line.strip() for line in lines)
    if len(rows) > 10 or units > 1000:
        match = MARKER.fullmatch(drawn)
        if not match or above or below:
            return False
        return int(match.group(1)) == len(rows) if len(rows) > 10 else match.group(2) is not None and int(match.group(2)) == units
    seen, whole = "".join(drawn.split()), "".join(kept.split())
    if below or not seen:
        return False
    return whole.endswith(seen) if above else seen == whole


class Handoff:
    def __init__(self, transport, home, sleep=time.sleep):
        self.transport = transport
        self.home = home
        self.sleep = sleep

    def _call(self, method, params):
        """(result, refusal). A refusal is a definite answer or an unreachable Herdr."""
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
        """The pane's shell and foreground pids; none for a pane Herdr no longer has."""
        result, refusal = self._call("pane.process_info", {"pane_id": pane_id})
        if refusal == UNREACHABLE:
            return None, refusal
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
            return None, f"FirstMate runs {sanitize(row.get('agent'))}: only a pi message box can be filled"
        target = {name: row.get(name) for name in ("pane_id", "terminal_id", "workspace_id", "tab_id", "agent")}
        target["pid"] = pid
        return target, None

    def _screen(self, pane_id):
        result, refusal = self._call("pane.read", {"pane_id": pane_id, "source": "visible"})
        if refusal:
            return None, refusal
        return (result.get("read") or {}).get("text"), None

    def check(self, target):
        """Why the box cannot take a paste now, or None: the same pane and terminal still runs
        the lock owner, idle, scrolled to the bottom, with pi's editor showing and empty."""
        result, refusal = self._call("pane.get", {"pane_id": target["pane_id"]})
        if refusal:
            return refusal
        row = result.get("pane") or {}
        if (row.get("terminal_id"), row.get("agent")) != (target["terminal_id"], target["agent"]):
            return "FirstMate's pane changed since the preview"
        pids, refusal = self._pids(target["pane_id"])
        if refusal:
            return refusal
        if target["pid"] not in pids:
            return "FirstMate's pane changed since the preview"
        status = row.get("agent_status")
        if status not in READY:
            return f"FirstMate is {sanitize(status)}; try again when it is idle"
        if (row.get("scroll") or {}).get("offset_from_bottom"):
            return "FirstMate's pane is scrolled back; scroll it to the bottom first"
        screen, refusal = self._screen(target["pane_id"])
        if refusal:
            return refusal
        box = composer_box(screen)
        if box is None:
            return "FirstMate's message box is not showing (a dialog or menu is open)"
        lines, above, below = box
        if above or below or any(line.strip() for line in lines):
            return "FirstMate's message box already has text; send or clear it there first"
        return None

    def transfer(self, target, body):
        """(outcome, detail): transferred (seen in the box), refused (nothing pasted) or
        unconfirmed (pasted, not seen: look before trying again)."""
        current, refusal = self.locate()
        if refusal:
            return "refused", refusal
        if (current["pane_id"], current["terminal_id"]) != (target["pane_id"], target["terminal_id"]):
            return "refused", "FirstMate's pane changed since the preview"
        refusal = self.check(target)
        if refusal:
            return "refused", refusal
        text = paste_text(body)
        try:
            response = self.transport.socket_call(
                "pane.send_text", {"pane_id": target["pane_id"], "text": PASTE_START + text + PASTE_END}, TIMEOUT
            )
        except (OSError, ValueError):
            return "unconfirmed", "the paste may have reached FirstMate; look at its message box before trying again"
        if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
            error = response.get("error") if isinstance(response, dict) else None
            code = error.get("code") if isinstance(error, dict) else "bad reply"
            return "refused", f"Herdr refused the paste: {sanitize(code)}"
        for _ in range(POLLS):
            self.sleep(POLL_SECONDS)
            screen, refusal = self._screen(target["pane_id"])
            box = composer_box(screen) if not refusal else None
            if box is not None and shows(box, text):
                return "transferred", f"in FirstMate's message box (pane {target['pane_id']}): press Enter there to send"
        return "unconfirmed", "FirstMate's message box does not show the message; look there before trying again"
