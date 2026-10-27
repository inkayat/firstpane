"""Test utilities. Not part of the plugin."""

import json
import re
import shutil
from pathlib import Path


class CmdResult:
    def __init__(self, code, stdout="", stderr="", timed_out=False):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class FakeTerminal:
    def __init__(self):
        self.raw = False
        self.log = []

    def enter_raw(self):
        self.raw = True
        self.log.append("raw")

    def leave_raw(self):
        self.raw = False
        self.log.append("leave")

    def enable_mouse(self):
        self.log.append("mouse-on")

    def disable_mouse(self):
        self.log.append("mouse-off")

    def pixel_mouse(self, on):
        self.log.append("pixel-mouse-on" if on else "pixel-mouse-off")

    def hover_mouse(self, on):
        self.log.append("hover-mouse-on" if on else "hover-mouse-off")

    def page_keyboard(self, on):
        self.log.append("page-keys-on" if on else "page-keys-off")


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.socket_calls = []
        self.snapshot = {
            "version": "0.9.0",
            "protocol": 22,
            "workspaces": [],
            "tabs": [],
            "panes": [],
            "layouts": [],
            "agents": [],
        }
        self.snapshot_error = None
        self.pane_get = {}
        self.handlers = []
        self.editor_body = "hello"
        self.editor_code = 0
        self.editor_raises = None
        self.editor_seen_raw = []
        self.editor_seen_text = []
        self.browser = []
        self.socket_handlers = {}

    def socket_call(self, method, params, timeout):
        self.socket_calls.append(
            {"method": method, "params": dict(params), "timeout": timeout}
        )
        if method == "session.snapshot":
            if self.snapshot_error is not None:
                raise self.snapshot_error
            return {
                "id": "t",
                "result": {"type": "session_snapshot", "snapshot": self.snapshot},
            }
        if method == "pane.get":
            payload = self.pane_get.get(params.get("pane_id"))
            if payload is None:
                return {
                    "id": "t",
                    "error": {"code": "pane_not_found", "message": "missing"},
                }
            if isinstance(payload, dict) and payload.get("error"):
                return {"id": "t", "error": payload["error"]}
            return {"id": "t", "result": {"type": "pane_info", "pane": payload}}
        if method == "pane.focus":
            return {
                "id": "t",
                "result": {
                    "type": "pane_focused",
                    "pane_id": params.get("pane_id"),
                    "workspace_id": "w",
                },
            }
        if method in self.socket_handlers:
            return self.socket_handlers[method](params)
        raise AssertionError("unexpected socket method " + method)

    def run(self, argv, stdin=None, env=None, timeout=None, cwd=None, detach=False):
        call = {
            "argv": list(argv),
            "stdin": stdin,
            "env": None if env is None else dict(env),
            "timeout": timeout,
            "cwd": cwd,
            "detach": detach,
        }
        self.calls.append(call)
        for pred, fn in self.handlers:
            if pred(argv):
                return fn(call)
        return CmdResult(127, "", "unexpected " + " ".join(argv))

    def open_editor(self, path, terminal):
        self.editor_seen_raw.append(terminal.raw)
        self.editor_seen_text.append(Path(path).read_text())
        if self.editor_raises is not None:
            raise self.editor_raises
        if self.editor_code == 0 and self.editor_body is not None:
            Path(path).write_text(self.editor_body)
        return self.editor_code

    def browser_open(self, url):
        self.browser.append(url)
        return CmdResult(0, "", "")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def pane(**overrides):
    row = {
        "pane_id": "w1:p1",
        "terminal_id": "term_a",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "focused": False,
        "agent_status": "working",
        "revision": 1,
        "cwd": "/work/alpha",
        "foreground_cwd": "/work/alpha",
        "agent": "claude",
    }
    row.update(overrides)
    return row


def agent_for(row, name="claude"):
    return {
        "name": name,
        "agent": row.get("agent"),
        "agent_status": row["agent_status"],
        "cwd": row.get("cwd"),
        "foreground_cwd": row.get("foreground_cwd"),
        "pane_id": row["pane_id"],
        "terminal_id": row["terminal_id"],
        "workspace_id": row["workspace_id"],
        "tab_id": row["tab_id"],
        "focused": row["focused"],
        "revision": row["revision"],
    }


def workspace(workspace_id, label, worktree=None, number=1):
    return {
        "workspace_id": workspace_id,
        "number": number,
        "label": label,
        "focused": False,
        "pane_count": 1,
        "tab_count": 1,
        "active_tab_id": workspace_id + ":t1",
        "agent_status": "unknown",
        "worktree": worktree,
    }


def worktree(repo_key, repo_name, repo_root, checkout_path=None, linked=False):
    return {
        "repo_key": repo_key,
        "repo_name": repo_name,
        "repo_root": repo_root,
        "checkout_path": checkout_path or repo_root,
        "is_linked_worktree": linked,
    }


def snapshot(workspaces, panes, agents=None):
    if agents is None:
        agents = [agent_for(row) for row in panes if row.get("agent")]
    return {
        "version": "0.9.0",
        "protocol": 22,
        "workspaces": workspaces,
        "tabs": [],
        "panes": panes,
        "layouts": [],
        "agents": agents,
    }


def write_meta(home, task, **fields):
    state = Path(home) / "state"
    state.mkdir(parents=True, exist_ok=True)
    text = "".join(f"{key}={value}\n" for key, value in fields.items())
    (state / f"{task}.meta").write_text(text)
    return state / f"{task}.meta"


def gh_env_ok(call):
    env = call["env"] or {}
    return env.get("GH_PROMPT_DISABLED") == "1" and env.get("GH_NO_UPDATE_NOTIFIER") == "1"


def argv_has(call, *parts):
    argv = call["argv"]
    return all(part in argv for part in parts)


def note_json(outcome, note_id="n1", request_id="firstpane-deadbeef"):
    return json.dumps(
        {
            "schema": "fm-inbox-note.v1",
            "outcome": outcome,
            "id": note_id,
            "request_id": request_id,
            "saved": outcome != "failed",
            "announced": outcome in ("created", "replay"),
            "acknowledged": False,
            "path": "/tmp/n1.note",
        },
        separators=(",", ":"),
    )


def strip_sgr(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def ghostty_argv(command):
    """How Ghostty 1.3 on macOS runs an AppleScript surface `command` (after `login -flp <user>`):
    bash, no rc files, `exec -l` prepended to the command text."""
    return ["/bin/bash", "--noprofile", "--norc", "-c", "exec -l " + command]


def remove_launch_runtimes(base):
    """Remove the /tmp/fpb.* runtime directories that browser launches under `base` created
    (they live outside the test's temporary directory)."""
    for spec in Path(base).glob("**/launch/*/spec.json"):
        try:
            runtime = json.loads(spec.read_text())["env"]["XDG_RUNTIME_DIR"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if Path(runtime).parent == Path("/tmp") and Path(runtime).name.startswith("fpb."):
            shutil.rmtree(runtime, ignore_errors=True)


def selected_line(frame):
    """The first line of the selected row on screen: the Agents tab marks it with a left bar,
    the Pull Requests tab with `>`."""
    for line in strip_sgr(frame).splitlines():
        if line.startswith("\u258c") or line.startswith(">"):
            return line
    raise AssertionError("no selected row in:\n" + strip_sgr(frame))
RULE = "\u2500" * 40
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"


class FakePi:
    """The FirstMate primary's pi composer behind a Herdr pane, at the socket boundary.

    Input handling follows pi-tui's editor (bracketed paste buffered until its end marker,
    normalized, controls filtered, >10 lines or >1000 UTF-16 units collapsed to a
    `[paste #N ...]` marker; LF is a new line, CR submits). The screen is pi's layout: the
    editor between two rules with the footer below; with more lines than `rows`, it shows the
    last ones (the cursor's) under a `─── ↑ N more ───` border. `dialog` replaces the editor
    (like the trust prompt): input goes to it, and no footer is drawn."""

    def __init__(self, transport, home, pane_id="w5:p1", terminal_id="term_main", pid=4242, agent="pi",
                 status="idle", text="", lock=True):
        self.transport = transport
        self.pane = pane(pane_id=pane_id, terminal_id=terminal_id, workspace_id="w5", tab_id="w5:t1",
                         agent_status=status, agent=agent, cwd="/fm", foreground_cwd="/fm",
                         scroll={"offset_from_bottom": 0, "max_offset_from_bottom": 0, "viewport_rows": 30})
        self.pid = pid
        self.text = text
        self.pastes = 0
        self.submitted = []
        self.received = []
        self.dialog = None
        self.render = True
        self.in_paste = False
        self.buffer = ""
        self.rows = None
        state = Path(home) / "state"
        state.mkdir(parents=True, exist_ok=True)
        if lock:
            (state / ".lock").write_text(f"{pid}\n")
        transport.snapshot["panes"].append(self.pane)
        transport.pane_get[pane_id] = self.pane
        transport.socket_handlers.update({
            "pane.process_info": self.process_info,
            "pane.read": self.read,
            "pane.send_text": self.send_text,
        })

    # -- socket methods ----------------------------------------------------------------
    def process_info(self, params):
        if params["pane_id"] != self.pane["pane_id"]:
            return {"id": "t", "result": {"type": "pane_process_info", "process_info": {
                "pane_id": params["pane_id"], "shell_pid": 100, "foreground_processes": [{"pid": 100, "name": "zsh"}]}}}
        return {"id": "t", "result": {"type": "pane_process_info", "process_info": {
            "pane_id": self.pane["pane_id"], "shell_pid": 77,
            "foreground_processes": [{"pid": self.pid, "name": "node", "argv0": self.pane.get("agent")}]}}}

    def read(self, params):
        assert params["pane_id"] == self.pane["pane_id"], params
        return {"id": "t", "result": {"type": "pane_read", "read": {
            "pane_id": params["pane_id"], "workspace_id": "w5", "tab_id": "w5:t1", "source": params.get("source"),
            "format": "text", "text": self.screen(), "revision": 1, "truncated": False}}}

    def send_text(self, params):
        assert params["pane_id"] == self.pane["pane_id"], params
        self.received.append(params["text"])
        self.feed(params["text"])
        return {"id": "t", "result": {"type": "ok"}}

    # -- pi editor model ---------------------------------------------------------------
    def feed(self, data):
        if self.dialog is not None:
            return
        while data:
            if self.in_paste:
                self.buffer += data
                end = self.buffer.find(PASTE_END)
                if end < 0:
                    return
                self.paste(self.buffer[:end])
                data, self.buffer, self.in_paste = self.buffer[end + len(PASTE_END):], "", False
                continue
            if data.startswith(PASTE_START):
                self.in_paste, data = True, data[len(PASTE_START):]
                continue
            char, data = data[0], data[1:]
            if char == "\r":
                self.submitted.append(self.text)
                self.text = ""
            elif char == "\n" or ord(char) >= 32:
                self.text += char

    def paste(self, raw):
        clean = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
        clean = "".join(char for char in clean if char == "\n" or ord(char) >= 32)
        lines = clean.split("\n")
        units = len(clean.encode("utf-16-le")) // 2
        if len(lines) > 10 or units > 1000:
            self.pastes += 1
            clean = f"[paste #{self.pastes} +{len(lines)} lines]" if len(lines) > 10 else f"[paste #{self.pastes} {units} chars]"
        self.text += clean

    def screen(self):
        top = ["", " pi v1.1.0", " escape interrupt", ""]
        if self.dialog is not None:
            return "\n".join(top + [RULE, "", self.dialog, "", RULE, "", ""])
        body = self.text.split("\n") if (self.text and self.render) else [""]
        top_border = RULE
        if self.rows and len(body) > self.rows:
            hidden = len(body) - self.rows
            body = body[hidden:]
            top_border = "\u2500" * 13 + f" \u2191 {hidden} more " + "\u2500" * 14
        return "\n".join(top + [top_border] + body + [RULE, "/fm", "0.0%/0 (auto)                  model"])
