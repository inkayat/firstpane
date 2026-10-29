"""Terminal tab: a new empty shell in the selected agent's directory.

The shell lives in Firstpane's own Herdr workspace, never in the agent's, and Firstpane never
focuses, attaches to, or reuses the agent's terminal. Closing it requires an explicit y.
"""

import base64
import binascii
import json
import os
import signal
import subprocess
from pathlib import Path

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


class TerminalControl:
    """One `herdr terminal session control` child. No takeover unless the user asks."""

    def __init__(self, popen, kill=None):
        self.popen = popen
        self.kill = kill or (lambda pid, sig: None)
        self.child = None
        self.stdin = None
        self.closed_shell = False
        self._paste = None

    def start(self, herdr, terminal_id, cols, rows, env, takeover=False):
        argv = [herdr or "herdr", "terminal", "session", "control", terminal_id, "--cols", str(cols), "--rows", str(rows)]
        if takeover:
            argv.append("--takeover")
        self.child = self.popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            shell=False,
        )
        self.stdin = self.child.stdin
        self.closed_shell = False
        return argv

    def _send(self, payload):
        if self.stdin is None or self.child is None:
            return
        try:
            self.stdin.write((json.dumps(payload) + "\n").encode())
            self.stdin.flush()
        except (OSError, ValueError):
            self.stdin = None

    def resize(self, cols, rows):
        self._send({"type": "terminal.resize", "cols": cols, "rows": rows})

    def input_text(self, text):
        self._send({"type": "terminal.input", "text": text})

    def input_bytes(self, data):
        self._send({"type": "terminal.input", "bytes": base64.b64encode(data).decode()})

    def feed_keys(self, data):
        """Return 'firstpane' when Ctrl-O outside a paste gives the keyboard back."""
        if self._paste is not None:
            self._paste += data
            end = self._paste.find(PASTE_END)
            if end == -1:
                return None
            body = self._paste[len(PASTE_START):end]
            self._paste = None
            self.input_text(body.decode("utf-8", "replace"))
            return None
        if data.startswith(PASTE_START):
            self._paste = data
            return self.feed_keys(b"")
        if data == b"\x0f":
            return "firstpane"
        self.input_bytes(data)
        return None

    def read_frames(self):
        """Non-blocking stdout lines already available. Returns applied frame count."""
        child = self.child
        if child is None or child.stdout is None:
            return 0
        import select
        ready, _, _ = select.select([child.stdout], [], [], 0)
        if not ready:
            return 0
        chunk = child.stdout.readline()
        if not chunk:
            return 0
        self._frames = getattr(self, "_frames", b"") + chunk
        return 1

    def release(self):
        """Detach. The shell keeps whatever the user had not sent."""
        if self.child is None:
            return
        self._send({"type": "terminal.release"})
        if self.stdin is not None:
            try:
                self.stdin.close()
            except OSError:
                pass
        try:
            self.child.wait(1)
        except Exception:
            self.kill(self.child.pid, signal.SIGTERM)
        self.child = None

    def confirm_close(self):
        """The shell is closed only after an explicit y. This records that consent."""
        self.closed_shell = True

from firstpane.sanitize import sanitize
from firstpane.termframe import TermGrid

CREATE_TIMEOUT = 3


def terminal_key(directory):
    return os.path.realpath(directory)


class TerminalBook:
    """One shell record per (session, directory). Unknown panes are never adopted."""

    def __init__(self, path, session):
        self.path = Path(path)
        self.session = session or ""
        self.records = self._load()

    def _load(self):
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        rows = payload.get(self.session) if isinstance(payload, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def save(self):
        try:
            payload = json.loads(self.path.read_text()) if self.path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload[self.session] = self.records
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".terminals.{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload))
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)

    def find(self, directory):
        key = terminal_key(directory)
        return next((row for row in self.records if row.get("key_dir") == key), None)

    def pending(self, directory):
        row = self.find(directory)
        return row if row and row.get("state") == "pending" else None

    def note(self, directory, **fields):
        key = terminal_key(directory)
        row = self.find(directory) or {"key_dir": key}
        row.update(fields)
        row["key_dir"] = key
        self.records = [item for item in self.records if item.get("key_dir") != key] + [row]
        self.save()
        return row

    def drop(self, directory):
        key = terminal_key(directory)
        self.records = [item for item in self.records if item.get("key_dir") != key]
        self.save()


def should_create(active, workspace_state, record):
    """A shell is created only for the open Terminal tab, a verified directory, and no record."""
    return bool(active and workspace_state == "ok" and record is None)


def agent_terminal_refused(record, agent_terminal_id):
    return bool(record and agent_terminal_id and record.get("terminal_id") == agent_terminal_id)


class TerminalMixin:
    def _init_terminal(self):
        self.terminal_status = ""
        self.terminal_keyboard = False
        self.terminal_grid = TermGrid()
        self.terminal_control = None
        self._terminal_attached = None
        self._terminal_size = None
        self._terminal_book = None
        self._terminal_calls = []

    def _terminal_book_for(self):
        if self._terminal_book is None:
            session = getattr(self, "session", None) or ""
            self._terminal_book = TerminalBook(Path(self.state_dir) / "terminals.json", session)
        return self._terminal_book

    def _terminal_directory(self):
        info = self._ws_info(self.target) if self.target else None
        workspace = info.get("workspace") if info else None
        if workspace is None:
            return None, "loading"
        if workspace.state != "ok" or not workspace.path:
            return None, workspace.reason or workspace.state
        return os.path.realpath(workspace.path), None

    def _terminal_body(self, plain, budget):
        directory, reason = self._terminal_directory()
        if reason:
            self._terminal_reset()
            self.terminal_status = f"Terminal unavailable: {reason}"
            return [self.terminal_status], [None]
        record = self._terminal_book_for().find(directory)
        agent_terminal = (self.target or {}).get("terminal_id")
        if agent_terminal_refused(record, agent_terminal):
            self._terminal_reset()
            self.terminal_status = "Firstpane will not attach to an agent's terminal"
            return [self.terminal_status], [None]
        if record and record.get("state") == "pending":
            self._terminal_reset()
            self.terminal_status = f"Creating the terminal may have succeeded; a Herdr tab '{record.get('label')}' may exist. n creates a new one."
            return [self.terminal_status], [None]
        if record is None and should_create(True, "ok", record):
            self._terminal_reset()
            self._terminal_create(directory)
            record = self._terminal_book_for().find(directory)
        if record and record.get("state") == "ready" and record.get("terminal_id"):
            if not self.terminal_status.startswith("Terminal output"):
                self.terminal_status = ""
            self._terminal_attach(record)
            self._terminal_sync_size()
        else:
            self._terminal_reset()
        lines = self.terminal_grid.rows(self.terminal_keyboard)[:budget]
        if self.terminal_status:
            lines = [self.terminal_status] + lines
        return (lines or [""])[:budget], [None] * budget

    def _terminal_create(self, directory):
        """Create a shell in Firstpane's own workspace. Never focuses it and never retries a timeout."""
        pane_id = getattr(self.config, "herdr_pane_id", None)
        if not pane_id:
            self.terminal_status = "Terminal unavailable: not running in a Herdr pane"
            return
        label = "Terminal · " + sanitize((self.target or {}).get("label") or "agent")[:69]
        book = self._terminal_book_for()
        try:
            own = self.transport.socket_call("pane.get", {"pane_id": pane_id}, CREATE_TIMEOUT)
        except Exception:
            book.note(directory, state="pending", label=label)
            self.terminal_status = f"Creating the terminal may have succeeded; a Herdr tab '{label}' may exist. n creates a new one."
            return
        workspace_id = ((own.get("result") or {}).get("pane") or {}).get("workspace_id")
        if not workspace_id:
            book.note(directory, state="pending", label=label)
            self.terminal_status = f"Creating the terminal may have succeeded; a Herdr tab '{label}' may exist. n creates a new one."
            return
        params = {"workspace_id": workspace_id, "cwd": directory, "focus": False, "label": label}
        self._terminal_calls.append(params)
        try:
            created = self.transport.socket_call("tab.create", params, CREATE_TIMEOUT)
        except Exception:
            book.note(directory, state="pending", label=label)
            self.terminal_status = f"Creating the terminal may have succeeded; a Herdr tab '{label}' may exist. n creates a new one."
            return
        root = ((created.get("result") or {}).get("root_pane") or {})
        if not root.get("pane_id"):
            book.note(directory, state="pending", label=label)
            return
        try:
            info = self.transport.socket_call("pane.get", {"pane_id": root["pane_id"]}, CREATE_TIMEOUT)
            pane = (info.get("result") or {}).get("pane") or {}
        except Exception:
            book.note(directory, state="pending", label=label)
            return
        book.note(
            directory,
            state="ready",
            label=label,
            terminal_id=pane.get("terminal_id"),
            pane_id=pane.get("pane_id") or root.get("pane_id"),
            tab_id=pane.get("tab_id"),
            workspace_id=workspace_id,
        )
        self.terminal_status = ""

    def _terminal_attach(self, record):
        if getattr(self, "terminal_control", None) is not None:
            if self._terminal_attached == record["terminal_id"]:
                return
            self._terminal_reset()
        popen = getattr(self, "terminal_popen", None) or subprocess.Popen
        herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
        self.terminal_control = TerminalControl(popen)
        self.terminal_control.start(herdr, record["terminal_id"], max(2, self.cols), max(2, self.rows - 2), self.env)
        self._terminal_attached = record["terminal_id"]

    def _terminal_sync_size(self):
        control = getattr(self, "terminal_control", None)
        if control is None:
            return
        size = (max(2, self.cols), max(2, self.rows - 2))
        if getattr(self, "_terminal_size", None) == size:
            return
        self._terminal_size = size
        control.resize(*size)

    def _terminal_pump(self):
        control = getattr(self, "terminal_control", None)
        if control is None or not control.read_frames():
            return
        raw = getattr(control, "_frames", b"")
        control._frames = b""
        for line in raw.splitlines():
            try:
                payload = json.loads(line)
                if payload.get("type") != "terminal.frame":
                    continue
                data = base64.b64decode(payload.get("bytes") or "", validate=True)
                width = int(payload.get("width") or self.cols)
                height = int(payload.get("height") or max(2, self.rows - 2))
            except (ValueError, TypeError, AttributeError, binascii.Error):
                self.terminal_status = "Terminal output not understood; showing the last good frame"
                continue
            if self.terminal_grid.apply(data, bool(payload.get("full")), width, height):
                self.terminal_status = "Terminal output not understood; showing the last good frame"
            elif self.terminal_status.startswith("Terminal output"):
                self.terminal_status = ""

    def _terminal_release(self):
        control = getattr(self, "terminal_control", None)
        if control is not None:
            control.release()
            self.terminal_control = None
        self._terminal_attached = None
        self.terminal_keyboard = False

    def _terminal_reset(self):
        """Detach and forget the last frame, so the next shell never shows this one's screen."""
        if getattr(self, "terminal_control", None) is None and not self.terminal_grid.cells:
            return
        self._terminal_release()
        self.terminal_grid = TermGrid()
        self._terminal_size = None

    def _terminal_raw(self, data):
        control = getattr(self, "terminal_control", None)
        if control is None:
            self.terminal_keyboard = False
            return
        if control.feed_keys(data) == "firstpane":
            self.terminal_keyboard = False

    def _terminal_key(self, key):
        if key == "n" and self.terminal_status.startswith("Creating"):
            directory, reason = self._terminal_directory()
            if directory and not reason:
                self._terminal_book_for().drop(directory)
                self._terminal_create(directory)
            return True
        if key == "x" and not self.terminal_keyboard:
            self._arm_terminal_close()
            return True
        if key in ("f", "enter") and not self.terminal_keyboard:
            self.terminal_keyboard = True
            return True
        if key == "ctrl-o" and self.terminal_keyboard:
            self.terminal_keyboard = False
            return True
        return True

    def _arm_terminal_close(self):
        directory, reason = self._terminal_directory()
        record = self._terminal_book_for().find(directory) if directory else None
        label = (record or {}).get("label") or "terminal"
        self.pending = {"kind": "terminal-close", "directory": directory, "label": label}
        self.screen = "confirm-terminal"
        self.detail_text = f"Close terminal '{label}' at {directory}? Running: shell only. y closes it and ends those processes."

    def _close_terminal(self):
        pending = self.pending or {}
        directory = pending.get("directory")
        self.screen = "tree"
        self.pending = None
        record = self._terminal_book_for().find(directory) if directory else None
        self._terminal_reset()
        if not record or not record.get("pane_id"):
            self.terminal_status = "terminal already gone"
            if directory:
                self._terminal_book_for().drop(directory)
            return
        self.transport.socket_call("pane.close", {"pane_id": record["pane_id"]}, CREATE_TIMEOUT)
        self._terminal_book_for().drop(directory)
        self.terminal_status = "terminal closed"
