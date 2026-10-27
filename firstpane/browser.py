"""The in-tab browser: the selected agent context's terminal-browser, drawn inside the Browser
tab body when Settings says Inside Firstpane (Ghostty tab opens it elsewhere: firstpane/placed.py).
`Profiles` (contexts, profiles, remembered addresses, the browser's environment) is shared.

A context is an agent identity together with its source worktree (`context`): it owns a
private profile (cookies, storage, terminal-browser settings) and the last address its page
was at. Two agents of one project, or one agent after it moves to another worktree, are
different contexts; one context's browser is shown at a time. Before Firstpane closes a
context's browser it reads that page's address (the launched instance only, as capture does)
and remembers it for the context; one address used by two worktrees is flagged
(`same_address`).

The browser is a child process of this Firstpane, started from the configured terminal-browser
root (never from PATH) with an environment built from scratch (no HERDR_* variables, no personal
HOME) plus terminal-browser's embed host protocol: PIXEL_EMBED names a unix socket Firstpane
listens on, PIXEL_TTY a pseudo-terminal whose output Firstpane forwards to its own pane, so
Firstpane stays the only writer of that pane. When the browser joins, Firstpane answers with the
image id, the cell grid of the tab body and the frame transport its terminal accepts
(firstpane/frames.py); every frame then arrives as kitty graphics for that image, and Firstpane
draws unicode placeholders where the image shows. EMBED_GATE_QUIET_MS=0 turns off the engine's
wait for a quiet tty, which only protects a tty with two writers.
Its environment also names a loopback agent bridge (embed.AgentBridge) with a random token:
terminal-browser's select tool hands each pick there, and the Browser tab turns it into a message
to FirstMate (`take_picks`, `answer_pick`).

Leaving the Browser tab removes the placeholders and takes the keyboard from the page, but the
browser is never told it is hidden: terminal-browser 0.13.4 drops the page's keyboard routing
when its view is hidden and does not restore it when shown, so a page that had been hidden would
ignore keys until clicked. The browser keeps running (and rendering an animated page) while the
tab is not shown.

Unlike the Ghostty tab, the browser belongs to this Firstpane: closing Firstpane, switching to
another context or `x` ends it (its own process group only); profiles and addresses stay.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import tty
import urllib.parse
from pathlib import Path

from firstpane.embed import AgentBridge, CaptureError, FrameStream, browser_env, capture_context
from firstpane.frames import LABELS
from firstpane.kitty import MAX_PLACEHOLDER, placeholder_rows

IMAGE_ID = 0x7E4301
DEFAULT_CELL = (9, 19)
SCHEMES = ("http", "https", "file")
LOOPBACK = re.compile(r"^(localhost|127(?:\.\d{1,3}){3}|\[::1\])(?=[:/?#]|$)", re.IGNORECASE)
CAPTURE_TIMEOUT = 3
SEND_TIMEOUT = 0.5
SETTINGS = {
    "telemetry.usage": "off",
    "telemetry.crashReports": "off",
    "updates.check": "off",
    "search.suggestions": "off",
}
SHORTCUTS = {"grab.toggle": None, "record.toggle": None}
PROFILE_DIRS = ("home", "data", "state", "cache", "config", "tbconfig", "interop", "appdata", "skills", "logs")
# terminal-browser shortcuts that would act outside the page (send to an agent, record, quit,
# devtools, close the tab) are not forwarded.
RESERVED = {
    "ctrl-g": "send-to-agent", "ctrl-r": "record", "ctrl-q": "quit", "ctrl-d": "devtools",
    "ctrl-w": "close-tab", "super-q": "quit", "super-w": "close-tab",
}
KEY_NAMES = {
    "enter": "enter", "tab": "tab", "backspace": "backspace", "esc": "escape", "delete": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end",
    "pageup": "pageup", "pagedown": "pagedown", "backtab": "tab",
}
CLIPBOARD_LIMIT = 1 << 20


def _page_address(url):
    """`url` when it is a web or file address a browser page can be (re)opened at, else None."""
    if not isinstance(url, str) or "://" not in url or not url.isprintable():
        return None
    scheme = url.split("://", 1)[0].lower()
    if scheme not in SCHEMES:
        return None
    parts = urllib.parse.urlsplit(url)
    if scheme == "file":
        return url if parts.netloc in ("", "localhost") and parts.path else None
    if not parts.hostname or any(char.isspace() for char in parts.netloc):
        return None
    try:
        port = parts.port
    except ValueError:
        port = 0
    return None if port == 0 else url


def _origin(url):
    parts = urllib.parse.urlsplit(url or "")
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if LOOPBACK.match(host) or host == "::1":
        host = "loopback"
    return scheme, host, port


def who(context):
    """`label (worktree name)`: whose browser, in words."""
    source = context.get("source") if context else None
    label = (context or {}).get("label") or "an earlier Firstpane browser"
    return f"{label} ({Path(source).name})" if source else f"{label} (source unknown)"


def split_key(name):
    """(base key, mods) of a decoded key name such as `ctrl-shift-a` or `super-l`."""
    mods = {"shift": False, "alt": False, "ctrl": False, "super": False}
    while True:
        head, sep, rest = name.partition("-")
        if sep and rest and head in mods:
            mods[head] = True
            name = rest
        else:
            return name, mods


class Profiles:
    """The configured terminal-browser install and the agent contexts' private profiles under
    `base_dir`: who a context is, the address it remembers, and the environment its browser runs
    in. Shared by the in-tab browser below and the placed one (firstpane/placed.py)."""

    def __init__(self, root, base_dir, env=None, clock=time.time):
        self.root = os.path.realpath(root) if root else None
        self.bin = os.path.join(self.root, "bin", "terminal-browser") if self.root else None
        self.base = Path(base_dir)
        self.base_env = dict(env or {})
        self.clock = clock
        self.capture_fn = capture_context

    @property
    def configured(self):
        return bool(self.root) and bool(self.bin) and os.access(self.bin, os.X_OK)

    def unconfigured(self):
        """Why no browser can start ("" when the install is usable)."""
        if not self.root:
            return "browser not configured: set terminal_browser_root in config.json"
        if not self.configured:
            return "browser unavailable: check terminal_browser_root in config.json"
        return ""

    # ---- contexts ----------------------------------------------------------------------
    def _context_dir(self, context_id):
        return self.base / "contexts" / context_id

    def context(self, identity, label, source):
        """The browser context of one agent identity in one source worktree."""
        key = json.dumps([[str(part) for part in identity], source])
        context_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        stored = self._read(self._context_dir(context_id) / "context.json")
        url = stored.get("url")
        return {"id": context_id, "identity": [str(part) for part in identity], "label": label,
                "source": source, "url": url if isinstance(url, str) else None}

    def _remember(self, context, url):
        path = self._context_dir(context["id"]) / "context.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_name(f".context.{os.getpid()}.tmp")
        temp.write_text(json.dumps({"identity": context["identity"], "label": context["label"],
                                    "source": context["source"], "url": url, "updated_at": self.clock()}))
        os.replace(temp, path)

    @staticmethod
    def _read(path):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def same_address(self, context, url):
        """Other worktrees' contexts whose remembered address reaches the same server as `url`."""
        origin = _origin(url)
        if origin is None:
            return []
        found = []
        for path in sorted(self.base.glob("contexts/*/context.json")):
            if path.parent.name == context["id"]:
                continue
            stored = self._read(path)
            if stored.get("source") != context["source"] and _origin(stored.get("url")) == origin:
                found.append({"label": stored.get("label"), "source": stored.get("source")})
        return found

    # ---- profile -----------------------------------------------------------------------
    @staticmethod
    def _dirs(profile):
        return {name: profile / name for name in PROFILE_DIRS}

    def _prepare_profile(self, profile):
        dirs = self._dirs(profile)
        for path in dirs.values():
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, wanted in (("settings.json", SETTINGS), ("shortcuts.json", SHORTCUTS)):
            path = dirs["tbconfig"] / name
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            if any(current.get(key, object()) != value for key, value in wanted.items()):
                current.update(wanted)
                path.write_text(json.dumps(current), encoding="utf-8")
        # terminal-browser's first run links agent skills and edits terminal settings files (in
        # the private home here); record that setup as declined and done instead.
        state = dirs["state"] / "terminal-browser"
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (state / "skills-choice").exists():
            (state / "skills-choice").write_text("no\n", encoding="utf-8")
        try:
            version = (Path(self.root) / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            version = ""
        if version:
            (state / "setup-version").write_text(f"{version} {self.root}\n", encoding="utf-8")

    def child_env(self, runtime, profile):
        """Built from scratch: nothing from the pane (no HERDR_*, terminal ids or personal HOME)."""
        dirs = {name: str(path) for name, path in self._dirs(profile).items()}
        return {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": dirs["home"],
            "XDG_DATA_HOME": dirs["data"],
            "XDG_STATE_HOME": dirs["state"],
            "XDG_CACHE_HOME": dirs["cache"],
            "XDG_CONFIG_HOME": dirs["config"],
            "XDG_RUNTIME_DIR": runtime,
            "TERMINAL_BROWSER_CONFIG_DIR": dirs["tbconfig"],
            "TERMINAL_BROWSER_INTEROP_DIR": dirs["interop"],
            "TERMINAL_BROWSER_APPDATA": dirs["appdata"],
            "AGENT_SKILLS_HOME": dirs["skills"],
            "TERMINAL_BROWSER_NO_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "TERMINAL_BROWSER_NO_MERGE": "1",
            "AGENT_BROWSER_AUTOSAVE_INTERVAL_MS": "0",
            "AGENT_BROWSER_ALLOW_FILE_ACCESS": "1",
            "TERM": "xterm-256color",
            "LANG": self.base_env.get("LANG") or "en_US.UTF-8",
        }


class Browser(Profiles):
    def __init__(self, root, base_dir, env=None, clock=time.time, popen=subprocess.Popen):
        super().__init__(root, base_dir, env, clock)
        self.popen = popen
        self.medium = None
        self.pending = None
        self.shown = None
        self.child = None
        self.child_env_used = None
        self.runtime = None
        self.listener = None
        self.conn = None
        self.conn_buf = b""
        self.bridge = None
        self.picks = []
        self.master = None
        self.stream = FrameStream(IMAGE_ID)
        self.grid = None
        self.title = None
        self.region = None
        self.cell = DEFAULT_CELL
        self.sent = None
        self.drawn = None
        self.layout = None
        self.visible = False
        self.focused = False
        self.notice = ""
        self.out = []
        self.state = "closed"
        self.reason = ""

    # ---- state -----------------------------------------------------------------------
    @property
    def running(self):
        return self.child is not None and self.child.poll() is None

    def owner(self):
        """Whose browser runs in this tab: {id, label, source}, or None."""
        if self.shown is None or not self.running:
            return None
        return {"id": self.shown["id"], "label": self.shown.get("label"), "source": self.shown.get("source")}

    def describe(self):
        if not self.root:
            return "not configured"
        if not self.configured:
            return "unavailable: terminal-browser not found under terminal_browser_root"
        if self.pending is not None and self.medium is None:
            return "starting: checking how frames reach this terminal"
        if self.state == "running" and self.medium != "shm":
            return f"running · {LABELS.get(self.medium, self.medium)}"
        return f"{self.state} ({self.reason})" if self.reason and self.state != "running" else self.state

    # ---- actions ---------------------------------------------------------------------
    def set_medium(self, medium):
        """The frame transport the pane's terminal accepts; starts a browser waiting for it."""
        self.medium = medium
        if self.pending is not None:
            context, self.pending = self.pending, None
            return self.open(context)
        return ""

    def open(self, context):
        """Show `context`'s browser in the tab: the running one when it is that context's, else a
        new one at the context's remembered address (or blank). Another context's browser is
        ended first; its address is remembered and its profile kept."""
        refusal = self.unconfigured()
        if refusal:
            return refusal
        self.refresh()
        if self.running and self.shown and self.shown["id"] == context["id"]:
            return ""
        if self.running:
            self._keep_address()
            self.shutdown()
        if self.medium is None:
            self.pending = context
            return ""
        return self._launch(context)

    def _launch(self, context):
        url = _page_address(context["url"])
        profile = self._context_dir(context["id"])
        self._prepare_profile(profile)
        # A short private directory: terminal-browser puts its unix sockets here, and so does the
        # embed listener.
        self.runtime = tempfile.mkdtemp(prefix="fpb.", dir="/tmp")
        sock = os.path.join(self.runtime, "embed.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(sock)
        self.listener.listen(1)
        self.listener.setblocking(False)
        self.master, slave = os.openpty()
        tty.setraw(slave)
        os.set_blocking(self.master, False)
        self.child_env_used = self.child_env(self.runtime, profile)
        env = browser_env(self.child_env_used, sock, os.ttyname(slave))
        log = open(profile / "logs" / "child.log", "ab")
        try:
            self.bridge = AgentBridge()
            self.child = self.popen([self.bin, "open"] + ([url] if url else []), env=dict(env, **self.bridge.env()),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(profile),
                                    start_new_session=True)
        except OSError as error:
            self._release()
            self.state, self.reason = "failed", str(error)
            return f"browser failed to start: {error}"
        finally:
            log.close()
            os.close(slave)
        self.shown = context
        self.stream = FrameStream(IMAGE_ID)
        self.sent = None
        self.grid = None
        self.title = None
        self.state, self.reason = "starting", ""
        return ""

    def _keep_address(self):
        """Remember the address the shown browser's page is at, for its context. Only the launched
        instance is read; a blank, internal or unreadable page leaves it as it was."""
        if not self.running or self.shown is None or self.child_env_used is None:
            return
        try:
            value = self.capture_fn(self.bin, self.root, self.child_env_used, timeout=CAPTURE_TIMEOUT,
                                    owner_pid=self.child.pid)
        except (CaptureError, OSError, ValueError, KeyError, TypeError):
            return
        url = _page_address(value.get("url") if isinstance(value, dict) else None)
        if url:
            self._remember(self.shown, url)

    def close(self):
        """End the shown browser; a start still waiting for the terminal's answer is dropped too."""
        self.pending = None
        if not self.running:
            return "no browser to close"
        owner = self.shown
        self._keep_address()
        self.shutdown()
        self.state, self.reason = "closed", "closed from Firstpane"
        return f"{who(owner)}'s browser closed (profile kept)"

    def capture(self, context, timeout=20):
        """URL, title and marked selection of exactly the launched browser (registry + CDP), only
        while it shows `context`'s page."""
        self.refresh()
        if not self.running or self.child_env_used is None:
            raise CaptureError("no browser running in this tab")
        if self.shown is None or self.shown["id"] != context["id"]:
            raise CaptureError(f"capture refused: this tab shows {who(self.owner())}'s browser, not {who(context)}'s")
        return self.capture_fn(self.bin, self.root, self.child_env_used, timeout=timeout, owner_pid=self.child.pid)

    def refresh(self):
        if self.child is not None and self.child.poll() is not None:
            code = self.child.returncode
            self.child = None
            self.grid = None
            self.out.append(self.stream.close())
            self._release()
            self.state, self.reason = "exited", f"code {code}"

    def shutdown(self, wait=3):
        """End this browser: its own process group only, then the pty and socket."""
        child = self.child
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                child.terminate()
            try:
                child.wait(wait)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except OSError:
                    child.kill()
                child.wait(wait)
        self.child = None
        self.grid = None
        self.focused = False
        self.stream.discard()
        self._release()

    def _release(self):
        for name in ("conn", "listener"):
            item = getattr(self, name)
            if item is not None:
                item.close()
                setattr(self, name, None)
        for conn, _ in self.picks:
            AgentBridge.reply(conn, 409)
        self.picks = []
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        if self.runtime:
            shutil.rmtree(self.runtime, ignore_errors=True)
            self.runtime = None

    # ---- host <-> browser --------------------------------------------------------------
    def send(self, message):
        if self.conn is None:
            return
        try:
            self.conn.sendall((json.dumps(message) + "\n").encode())
        except OSError:
            self._disconnect("page stopped reading; press x to close")

    def _disconnect(self, reason):
        self.conn.close()
        self.conn = None
        self.sent = None
        self.grid = None
        self.focused = False
        self.state, self.reason = "disconnected", reason

    def _size_message(self, kind):
        rows, cols = self.region or (1, 1)
        cw, ch = self.cell
        return {"type": kind, "cols": cols, "rows": rows, "width": cols * cw, "height": rows * ch, "cell": [cw, ch]}

    def set_region(self, rows, cols):
        """The cell grid the page should fill; the page hears of it when `place` allows."""
        self.region = (max(1, min(rows, MAX_PLACEHOLDER)), max(1, min(cols, MAX_PLACEHOLDER)))

    def set_cell(self, width, height):
        if width > 0 and height > 0:
            self.cell = (width, height)

    def place(self, view, now):
        """Whether a screen showing the page image at `view` ((top row, rows, cols) of its
        placeholders, or None) may be written now; then the page is sent its new size.

        Herdr turns its fast image path off for its client for good when the image's
        placement or size changes while a frame is still on its way there, so while the image
        is placed, a change waits until no frame or change went out for LAYOUT_QUIET; frames
        are held meanwhile and for LAYOUT_QUIET after it (FrameStream.placed). A frame already
        due goes first, so neither frames nor changes starve while both keep coming. The
        caller does not block: it renders and asks again by `deadline`."""
        layout = (view, self.region, self.cell)
        if layout != self.layout and self.layout is not None and self.layout[0] is not None:
            if self.stream.ready(now):
                self.stream.fenced = False
                return False
            if not self.stream.settled(now):
                self.stream.fenced = True
                return False
        if self.conn is not None and self.sent is not None and (self.region, self.cell) != self.sent:
            self.send(self._size_message("size"))
            self.sent = (self.region, self.cell)
        if layout != self.layout and view is not None:
            self.stream.placed(now)
        self.stream.fenced = False
        self.layout = layout
        return True

    def set_visible(self, value):
        """Whether the tab shows the page (placeholders drawn). The browser is not told."""
        self.visible = value
        if not value:
            self.set_focus(False)

    def set_focus(self, value):
        if value != self.focused:
            self.focused = value
            self.send({"type": "focus", "focused": value})

    def key(self, name):
        """Forward one decoded key; returns a notice when it is reserved and not forwarded."""
        if name in RESERVED:
            return f"{name} is reserved ({RESERVED[name]}), not forwarded"
        base, mods = split_key(name)
        if len(base) == 1 and base.isprintable():
            message = {"type": "key", "key": base.lower() if mods["ctrl"] or mods["super"] else base,
                       "kind": "press", "mods": dict(mods, shift=mods["shift"] or base != base.lower())}
            if not (mods["ctrl"] or mods["alt"] or mods["super"]):
                message["text"] = base
            self.send(message)
        elif base in KEY_NAMES:
            self.send({"type": "key", "key": KEY_NAMES[base], "kind": "press",
                       "mods": dict(mods, shift=mods["shift"] or base == "backtab")})
        return ""

    def paste(self, text):
        self.send({"type": "paste", "text": text})

    def mouse(self, kind, button, x, y, mods=None):
        """A mouse event at (x, y) pixels from the image's top-left corner."""
        self.send({"type": "mouse", "kind": kind, "button": button,
                   "mods": mods or {"shift": False, "alt": False, "ctrl": False, "super": False},
                   "x": max(0, int(x)), "y": max(0, int(y))})

    def cell_center(self, column, row):
        cw, ch = self.cell
        return column * cw + cw // 2, row * ch + ch // 2

    def _message(self, line):
        try:
            message = json.loads(line)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "join":
            init = self._size_message("init")
            init.update(imageId=IMAGE_ID, transport=self.medium or "inline", focused=self.focused)
            self.send(init)
            self.sent = (self.region, self.cell)
            self.state = "running"
        elif kind == "placed":
            self.grid = message
            self.state = "running"
        elif kind == "title":
            text = message.get("text")
            self.title = text if isinstance(text, str) else None
        elif kind == "clipboard":
            text = message.get("text")
            if isinstance(text, str) and text and len(text.encode()) <= CLIPBOARD_LIMIT:
                self.out.append(b"\x1b]52;c;" + base64.b64encode(text.encode()) + b"\x07")

    # ---- event loop hooks ------------------------------------------------------------
    def fds(self):
        fds = []
        if self.master is not None:
            fds.append(self.master)
        if self.listener is not None and self.conn is None:
            fds.append(self.listener)
        if self.conn is not None:
            fds.append(self.conn)
        if self.bridge is not None:
            fds.extend(self.bridge.fds())
        return fds

    def deadline(self):
        return self.stream.deadline()

    def pump(self, ready, now):
        """Handle readable browser fds; returns bytes for the pane tty (whole sequences only)."""
        out, self.out = self.out, []
        if self.listener is not None and self.listener in ready and self.conn is None:
            try:
                self.conn, _ = self.listener.accept()
                self.conn.settimeout(SEND_TIMEOUT)
                self.conn_buf = b""
            except OSError:
                self.conn = None
        if self.conn is not None and self.conn in ready:
            try:
                data = self.conn.recv(1 << 20)
            except OSError:
                data = b""
            if not data:
                self._disconnect("page closed the connection; press x to close")
            else:
                self.conn_buf += data
                while b"\n" in self.conn_buf:
                    line, self.conn_buf = self.conn_buf.split(b"\n", 1)
                    self._message(line)
        if self.master is not None and self.master in ready:
            try:
                data = os.read(self.master, 1 << 20)
            except (BlockingIOError, OSError):
                data = b""
            if data:
                out.append(self.stream.feed(data, now))
        out.append(self.stream.due(now))
        if self.bridge is not None:
            self.picks.extend(self.bridge.pump(ready))
        self.refresh()
        out.extend(self.out)
        self.out = []
        return b"".join(out)

    def take_picks(self):
        """Picks the select tool handed over since the last call, as (connection, text); each
        must get `answer_pick`."""
        picks, self.picks = self.picks, []
        return picks

    def answer_pick(self, conn, taken):
        AgentBridge.reply(conn, 200 if taken else 409)

    def body_lines(self, rows, cols):
        """The page image's placeholder rows (at most `rows` x `cols`); `drawn` keeps their size."""
        self.drawn = None
        if self.grid is None or not self.visible:
            return []
        grid_rows = int(self.grid.get("rows") or rows)
        grid_cols = int(self.grid.get("cols") or cols)
        image = int(self.grid.get("imageId") or IMAGE_ID)
        self.drawn = (min(rows, grid_rows), min(cols, grid_cols))
        return placeholder_rows(image, *self.drawn)
