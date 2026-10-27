"""The Browser tab's browser outside the tab, where Settings says: a new tab in the Ghostty window
in front (`ghostty`, the default) or a Herdr pane beside Firstpane (`beside`, the placement before
the browser was drawn inside the tab). One agent context's browser at a time.

Contexts, private profiles, remembered addresses and the same-address warning are the in-tab
browser's (browser.Profiles), so an address remembered there is reopened here. Each launch
writes a spec.json (mode 0600) holding the remembered address, the private environment and the
select tool's agent bridge (embed.AgentBridge, served by this Firstpane); the only text that
reaches a shell is one fixed command,

    <python> -I -B <firstpane>/browser_launch.py <spec.json>

which records its pid for the launch's nonce and replaces itself with `terminal-browser open
[<url>]` in that environment.

ghostty: one `osascript` call asks Ghostty (1.3+, macOS) for a new tab, running the command, in
the window that is Ghostty's front window at that moment. Firstpane cannot ask Ghostty which window
it runs in (Ghostty's AppleScript ties no window to a tty, process or Herdr pane), so this is the
window in front when the tab is chosen, normally the one its keys are typed in; it depends on
focus. The script reads that window's id once and then creates the tab in the window with that id
(Ghostty refuses rather than picking another window when the id no longer resolves), and returns
the id it read before the tab, not one read again afterwards. Only while Ghostty is the front
application with a window; otherwise nothing opens and no other window is guessed or created.
Ghostty on macOS runs the command as
`login -flp <user> bash --noprofile --norc -c "exec -l <command>"`, so the command itself carries
no `exec` (that made bash look for a program named `exec`).
It is never retried: osascript can fail after Ghostty already opened the tab, so the launch
record is written first and the tab counts as starting until the launcher's pid appears or
START_TIMEOUT passes. The tab is this Firstpane's browser only while that pid still runs the
configured terminal-browser. Focus and close use only the terminal id Ghostty returned for that
tab; close ends the launched process and the processes it started, then closes that terminal,
which takes its tab with it: never the host window or another tab.

beside: Herdr splits Firstpane's own pane to the right (focus stays here) and `exec` plus the
command is typed into the new pane's shell. The pane is this Firstpane's browser only while it
re-proves:
same pane, terminal, tab and workspace as when split, and a shell pid equal to the launched pid.
A Herdr that cannot be reached keeps the record and refuses every action.

The record (`companion.json`, the name the beside-only Firstpane used, whose records still load)
outlives Firstpane, so a restarted Firstpane reuses a browser that re-proves instead of opening
another; that browser's select tool then reaches no Firstpane until it is reopened. Closing
Firstpane does not close the browser. Profiles and launch records stay after it closes.
"""

import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from firstpane.browser import Profiles, _page_address, who
from firstpane.embed import AgentBridge, CaptureError
from firstpane.sanitize import sanitize

LAUNCHER = Path(__file__).resolve().parent / "browser_launch.py"
PLACEMENTS = ("ghostty", "beside")
DEFAULT_PLACEMENT = "ghostty"
PLACEMENT_LABELS = {"ghostty": "Ghostty tab", "beside": "Beside Firstpane"}
SOCKET_ERRORS = (TimeoutError, OSError, ConnectionError, ValueError)
TIMEOUT = 3
START_TIMEOUT = 20
OSASCRIPT_TIMEOUT = 10
STOP_WAIT = 3
# Arguments arrive as argv; nothing is spliced into the script text. Item 3 is a window id to use
# instead of the front window; Firstpane passes "" (PlacedBrowser.ghostty_window).
NEW_TAB = """on run argv
set launchCommand to item 1 of argv
set launchDirectory to item 2 of argv
set hostId to item 3 of argv
tell application "Ghostty"
if hostId is "" then
if not frontmost then return "refused: Ghostty is not the front application"
if (count of windows) is 0 then return "refused: Ghostty has no window"
set hostId to id of front window
else if (count of (windows whose id is hostId)) is 0 then
return "refused: Ghostty has no window " & hostId
end if
set cfg to new surface configuration
set command of cfg to launchCommand
set initial working directory of cfg to launchDirectory
set browserTab to new tab in window id hostId with configuration cfg
return hostId & linefeed & (id of browserTab) & linefeed & (id of focused terminal of browserTab)
end tell
end run"""
FOCUS = """on run argv
set wanted to item 1 of argv
tell application "Ghostty"
activate
focus (first terminal whose id is wanted)
end tell
end run"""
CLOSE = """on run argv
set wanted to item 1 of argv
tell application "Ghostty"
close (first terminal whose id is wanted)
end tell
end run"""


def _ps(*args):
    try:
        return subprocess.run(["ps", *args], capture_output=True, text=True, timeout=TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _descendants(pid):
    """Every process started (directly or not) by `pid`, from one `ps` listing."""
    children = {}
    for line in _ps("-axo", "pid=,ppid=").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, frontier = [], list(children.get(pid, []))
    while frontier:
        current = frontier.pop()
        found.append(current)
        frontier.extend(children.get(current, []))
    return found


def _alive(pid):
    """Still running (a zombie has ended)."""
    stat = _ps("-p", str(pid), "-o", "stat=").strip()
    return bool(stat) and not stat.startswith("Z")


class PlacedBrowser(Profiles):
    # The Ghostty window a tab is created in, by id; "" is Ghostty's front window. Not a setting:
    # only a caller that created its own window (a native proof) sets it.
    ghostty_window = ""

    def __init__(self, root, base_dir, own_pane_id, transport, env=None, clock=time.time, python=sys.executable,
                 platform=sys.platform):
        super().__init__(root, base_dir, env, clock)
        self.own_pane_id = own_pane_id
        self.transport = transport
        self.python = python
        self.platform = platform
        self.record = None
        self.reason = ""
        self.state = "none"
        self.bridge = None
        self.picks = []
        self._load()

    # ---- record ----------------------------------------------------------------------
    @property
    def pane_id(self):
        return self.record.get("pane_id") if self.record else None

    @property
    def placement(self):
        return self.record["placement"] if self.record else None

    def _record_path(self):
        return self.base / "companion.json"

    def _load(self):
        data = self._read(self._record_path())
        if not data or data.get("closed"):
            return
        data.setdefault("placement", "beside")  # written by the beside-only Firstpane
        if data["placement"] == "beside" and not isinstance(data.get("pane_id"), str):
            return
        if data["placement"] not in PLACEMENTS or not isinstance(data.get("spec"), str):
            return
        self.record = data
        self.state = "starting"

    def _save(self):
        self.base.mkdir(parents=True, exist_ok=True)
        path = self._record_path()
        temp = path.with_name(f".companion.{os.getpid()}.tmp")
        temp.write_text(json.dumps(self.record))
        os.replace(temp, path)

    def _forget(self, reason, ended=False):
        """Drop the record; `ended`: its browser is known gone, so its runtime directory goes too."""
        if self.record is not None:
            if ended:
                runtime = (self._spec_env() or {}).get("XDG_RUNTIME_DIR")
                if runtime and Path(runtime).parent == Path("/tmp") and Path(runtime).name.startswith("fpb."):
                    shutil.rmtree(runtime, ignore_errors=True)
            self.record = dict(self.record, closed=True, closed_reason=reason, closed_at=self.clock())
            self._save()
            self.record = None
        self.release()
        self.state = "closed"
        self.reason = reason

    def _spec_env(self):
        try:
            env = json.loads(Path(self.record["spec"]).read_text())["env"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return env if isinstance(env, dict) else None

    def owner(self):
        """Whose browser the record is: {id, label, source}, or None without one."""
        if self.record is None:
            return None
        return {"id": self.record.get("context"), "label": self.record.get("label"), "source": self.record.get("source")}

    def _shows(self, context):
        return self.record is not None and self.record.get("context") == context["id"]

    def where(self):
        if self.placement == "beside":
            return f"pane {self.pane_id} beside Firstpane"
        return "a Ghostty tab"

    def describe(self):
        if not self.root:
            return "not configured"
        if not self.configured:
            return "unavailable: terminal-browser not found under terminal_browser_root"
        if self.state == "open":
            text = f"{who(self.owner())}'s browser is open in {self.where()}"
            if self.bridge is None:
                text += "; its select tool does not reach this Firstpane (x, then 3, reconnects it)"
            return text
        if self.state == "starting":
            return f"{who(self.owner())}'s browser is starting in {self.where()}"
        if self.state == "failed":
            return f"{who(self.owner())}'s browser did not start in {self.where()}" if self.record else (
                f"did not start ({self.reason})" if self.reason else "did not start")
        if self.state == "unreachable":
            return f"browser in {self.where()} is unverified (Herdr unreachable)"
        if self.state == "closed":
            return f"no browser open ({self.reason})" if self.reason else "no browser open"
        return "no browser open"

    # ---- proof -----------------------------------------------------------------------
    def _request(self, method, params):
        """(answer, result): answer is "ok", "missing" (Herdr says there is no such pane) or
        "unreachable" (no usable answer: timeout, connect error, unreadable or unexpected reply)."""
        try:
            response = self.transport.socket_call(method, params, TIMEOUT)
        except SOCKET_ERRORS:
            return "unreachable", None
        if not isinstance(response, dict):
            return "unreachable", None
        error = response.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            return ("missing" if code == "pane_not_found" else "unreachable"), None
        result = response.get("result")
        return ("ok", result) if isinstance(result, dict) else ("unreachable", None)

    def _call(self, method, params):
        answer, result = self._request(method, params)
        return result if answer == "ok" else None

    def _launched(self):
        try:
            data = json.loads((Path(self.record["spec"]).parent / "launched.json").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("nonce") != self.record.get("nonce") or not isinstance(data.get("pid"), int):
            return None
        return data["pid"]

    def _runs_browser(self, pid):
        """True while `pid` runs the configured terminal-browser, None while it is still the
        launcher, False otherwise (gone, a zombie, or another program under a reused pid)."""
        line = _ps("-p", str(pid), "-o", "stat=,command=").strip()
        if not line or line.startswith("Z"):
            return False
        if self.root.rstrip("/") + "/" in line:
            return True
        return None if str(LAUNCHER) in line else False

    def _prove(self):
        """None when the recorded browser is still exactly ours, else why it is not ("starting"
        and "unreachable" are not verdicts: the record is kept)."""
        if self.placement == "ghostty":
            pid = self._launched()
            if pid is None:
                return "starting"
            running = self._runs_browser(pid)
            if running is None:
                return "starting"
            if not running:
                return "browser window closed"
            self.record["pid"] = pid
            return None
        answer, result = self._request("pane.get", {"pane_id": self.record["pane_id"]})
        if answer == "missing":
            return "pane closed"
        pane = result.get("pane") if result else None
        if not isinstance(pane, dict):
            return "unreachable"
        for name in ("terminal_id", "tab_id", "workspace_id"):
            if pane.get(name) != self.record.get(name):
                return "pane replaced" if name == "terminal_id" else "pane moved"
        pid = self._launched()
        if pid is None:
            return "starting"
        answer, result = self._request("pane.process_info", {"pane_id": self.record["pane_id"]})
        if answer == "missing":
            return "pane closed"
        info = result.get("process_info") if result else None
        if not isinstance(info, dict):
            return "unreachable"
        if info.get("shell_pid") != pid:
            return "pane no longer runs the launched browser"
        self.record["pid"] = pid
        return None

    def refresh(self):
        if self.record is None:
            if self.state not in ("closed", "failed"):
                self.state = "none"
            return
        reason = self._prove()
        if reason is None:
            self.state = "open"
        elif reason == "unreachable":
            self.state = "unreachable"
        elif reason == "starting":
            started = self.record.get("created_at") or 0
            self.state = "starting" if self.clock() - started < START_TIMEOUT else "failed"
        else:
            self._forget(reason, ended=reason == "browser window closed")

    # ---- actions ---------------------------------------------------------------------
    def _unsupported(self, placement):
        if placement == "ghostty" and self.platform != "darwin":
            return "Ghostty tab needs macOS (Ghostty's AppleScript); choose Beside Firstpane in Settings (6)"
        if placement == "beside" and not self.own_pane_id:
            return ("browser refused: Firstpane does not know its own Herdr pane (HERDR_PANE_ID); "
                    "choose Ghostty tab in Settings (6)")
        if placement not in PLACEMENTS:
            return f"browser refused: unknown placement {placement!r}"
        return ""

    def _unverified(self, action):
        if self.state == "unreachable":
            return f"browser in {self.where()} is unverified (Herdr unreachable); {action} refused"
        return ""

    def _other(self, context, action):
        if self._shows(context):
            return ""
        return f"{action} refused: {self.where()} shows {who(self.owner())}'s browser, not {who(context)}'s"

    def open(self, context, placement):
        """Show `context`'s browser in `placement`: the one already there (re-proven), else a new
        one at the context's remembered address, or blank. A browser of another context or
        placement is ended first (only once it re-proves); every profile stays."""
        refusal = self.unconfigured() or self._unsupported(placement)
        if refusal:
            return refusal
        self.refresh()
        refusal = self._unverified("open")
        if refusal:
            return refusal
        if self.record is not None:
            same = self._shows(context) and self.placement == placement
            if self.state == "starting":
                if same:
                    return f"{who(context)}'s browser is still starting in {self.where()}"
                return (f"busy: {who(self.owner())}'s browser is still starting in {self.where()}; "
                        f"nothing else opens until it runs or {START_TIMEOUT} s pass")
            if same and self.state == "open":
                return f"{who(context)}'s browser is in {self.where()}; f brings it forward"
            refusal, _ = self._end(f"replaced by {who(context)}'s browser")
            if refusal:
                return f"{refusal}; {who(context)}'s browser not opened"
        if placement == "ghostty":
            return self._open_ghostty(context)
        return self._open_beside(context)

    def _new_record(self, placement, context, spec, url):
        return {"placement": placement, "spec": str(spec["path"]), "nonce": spec["nonce"], "url": url,
                "created_at": self.clock(), "context": context["id"], "identity": context["identity"],
                "label": context["label"], "source": context["source"]}

    def _command(self, spec):
        return " ".join(shlex.quote(part) for part in (self.python, "-I", "-B", str(LAUNCHER), str(spec["path"])))

    def _open_ghostty(self, context):
        url = _page_address(context["url"])
        profile = self._context_dir(context["id"])
        spec = self._write_spec(url, profile)
        # Written before Ghostty is asked: whatever osascript answers, a tab it opened is
        # found by its launcher's pid instead of being asked for again.
        self.record = dict(self._new_record("ghostty", context, spec, url), terminal=None)
        self._save()
        self.state = "starting"
        result = self.transport.run(["osascript", "-e", NEW_TAB, self._command(spec), str(profile),
                                     self.ghostty_window],
                                    timeout=OSASCRIPT_TIMEOUT)
        if result.code == 127 and not result.timed_out:
            self._forget("osascript did not run")
            self.state = "failed"
            return "Ghostty tab unavailable: osascript did not run; choose Beside Firstpane in Settings (6)"
        answer = (result.stdout or "").strip()
        if result.code == 0 and answer.startswith("refused:"):
            # Checked before anything was created: no tab exists, nothing to wait for.
            why = sanitize(answer[len("refused:"):].strip())[:160]
            self._forget(why)
            self.state = "failed"
            return (f"Ghostty tab not opened: {why}. It opens in the Ghostty window in front: bring "
                    "Firstpane's Ghostty window forward and choose the tab again, or choose Beside Firstpane "
                    "in Settings (6)")
        ids = answer.splitlines()
        if result.code != 0 or result.timed_out or len(ids) != 3 or not all(ids):
            lines = (result.stderr or "").strip().splitlines()
            why = "timed out" if result.timed_out else sanitize(lines[-1] if lines else "")[:160]
            self.record["ghostty_error"] = why
            self._save()
            return (f"Ghostty tab not confirmed ({why or 'no answer'}); not asked again: "
                    f"{who(context)}'s browser counts as starting for up to {START_TIMEOUT} s")
        self.record.update(window=ids[0], tab=ids[1], terminal=ids[2])
        self._save()
        return f"opening {who(context)}'s browser in a Ghostty tab"

    def _open_beside(self, context):
        own = self._call("pane.get", {"pane_id": self.own_pane_id})
        own = own.get("pane") if own else None
        if not isinstance(own, dict):
            return "browser refused: this Firstpane pane is not visible to Herdr"
        url = _page_address(context["url"])
        profile = self._context_dir(context["id"])
        spec = self._write_spec(url, profile)
        result = self._call("pane.split", {"target_pane_id": self.own_pane_id, "direction": "right",
                                           "focus": False, "cwd": str(profile)})
        pane = result.get("pane") if result else None
        if not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str):
            self.release()
            return "browser pane not created: Herdr refused the split"
        if pane.get("tab_id") != own.get("tab_id") or pane.get("workspace_id") != own.get("workspace_id"):
            # The response naming a pane does not prove that pane is ours: record it, touch nothing.
            evidence = Path(spec["path"]).parent / "refused-split.json"
            evidence.write_text(json.dumps({"own": own, "pane": pane, "at": self.clock()}))
            self.release()
            return (f"browser refused: Herdr reported the new pane {pane['pane_id']} outside this tab; "
                    "nothing was sent to it or closed")
        self.record = dict(self._new_record("beside", context, spec, url), pane_id=pane["pane_id"],
                           terminal_id=pane.get("terminal_id"), tab_id=pane.get("tab_id"),
                           workspace_id=pane.get("workspace_id"))
        self._save()
        self.state = "starting"
        if self._call("pane.send_input", {"pane_id": pane["pane_id"], "text": "exec " + self._command(spec),
                                          "keys": ["enter"]}) is None:
            return f"browser pane {pane['pane_id']} created, but the launch was not accepted; x closes it"
        return f"opening {who(context)}'s browser in pane {pane['pane_id']} beside Firstpane"

    def _end(self, reason):
        """Close the recorded browser (its address kept first) and forget it: (refusal or "",
        whether a Ghostty tab of it is left open for the user to close)."""
        if self.placement == "beside":
            pane_id = self.pane_id
            self._keep_address()
            if self._call("pane.close", {"pane_id": pane_id}) is None:
                return f"browser pane {pane_id} not closed", False
            self._forget(reason, ended=True)
            return "", False
        if self.state == "open":
            self._keep_address()
            if not self._stop(self.record["pid"]):
                return f"{who(self.owner())}'s browser in its Ghostty tab did not stop", False
            # Ghostty keeps a tab whose command ended; close only the terminal it named for us.
            terminal = self.record.get("terminal")
            closed = bool(terminal) and self.transport.run(["osascript", "-e", CLOSE, terminal],
                                                           timeout=OSASCRIPT_TIMEOUT).code == 0
            self._forget(reason, ended=True)
            return "", not closed
        self._forget(reason)
        return "", False

    def _stop(self, pid):
        """End the launched (re-proven) browser: that process and every process it started,
        as seen just before the signal. terminal-browser's browser process runs in a session of
        its own and outlives a command that alone is signalled."""
        pids = [pid] + _descendants(pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for target in pids:
                try:
                    os.kill(target, sig)
                except OSError:
                    pass
            deadline = time.monotonic() + STOP_WAIT
            while time.monotonic() < deadline:
                pids = [target for target in pids if _alive(target)]
                if not pids:
                    return True
                time.sleep(0.05)
        return not [target for target in pids if _alive(target)]

    def _keep_address(self):
        """Remember the address the shown browser's page is at, for its context. Only the
        launched, re-proven instance is read (as capture does); a blank, internal or unreadable
        page leaves the remembered address as it was."""
        if self.state != "open" or not self.record.get("identity"):
            return
        try:
            value = self.capture_fn(self.bin, self.root, self._spec_env(), timeout=TIMEOUT, owner_pid=self.record["pid"])
        except (CaptureError, OSError, ValueError, KeyError, TypeError):
            return
        url = _page_address(value.get("url") if isinstance(value, dict) else None)
        if url:
            self._remember({"id": self.record["context"], "identity": self.record["identity"],
                            "label": self.record.get("label"), "source": self.record.get("source")}, url)

    def focus(self, context):
        self.refresh()
        if self.record is None:
            return "no browser open; the Browser tab (3) opens one"
        refusal = self._unverified("focus") or self._other(context, "focus")
        if refusal:
            return refusal
        if self.state != "open":
            return f"{who(context)}'s browser is still starting in {self.where()}"
        if self.placement == "beside":
            if self._call("pane.focus", {"pane_id": self.pane_id}) is None:
                return f"browser pane {self.pane_id} not focused"
            return f"focused browser pane {self.pane_id}"
        terminal = self.record.get("terminal")
        if not terminal:
            return "cannot bring the Ghostty tab forward: Ghostty did not name it; it is still open there"
        result = self.transport.run(["osascript", "-e", FOCUS, terminal], timeout=OSASCRIPT_TIMEOUT)
        if result.code != 0:
            return "Ghostty tab not brought forward: " + sanitize((result.stderr or "no answer").strip())[:160]
        return f"brought {who(context)}'s Ghostty tab forward"

    def close(self):
        self.refresh()
        if self.record is None:
            return "no browser open to close"
        refusal = self._unverified("close")
        if refusal:
            return refusal
        if self.state == "starting" and self.placement == "ghostty":
            return f"busy: {who(self.owner())}'s browser is still starting in {self.where()}; x again once it runs"
        where, owner = self.where(), self.owner()
        refusal, window_left = self._end("closed from Firstpane")
        if refusal:
            return refusal
        return f"{who(owner)}'s browser in {where} closed (profile kept)" + (
            "; close its Ghostty tab there" if window_left else "")

    def capture(self, context, timeout=20):
        """URL, title and marked selection of exactly the launched browser (registry + CDP),
        only while it shows `context`'s page."""
        self.refresh()
        if self.state != "open":
            raise CaptureError("no open browser from this Firstpane")
        refusal = self._other(context, "capture")
        if refusal:
            raise CaptureError(refusal)
        return self.capture_fn(self.bin, self.root, self._spec_env(), timeout=timeout, owner_pid=self.record["pid"])

    # ---- launch spec -------------------------------------------------------------------
    def _write_spec(self, url, profile):
        self._prepare_profile(profile)
        nonce = secrets.token_hex(8)
        launch = profile / "launch" / nonce
        launch.mkdir(mode=0o700, parents=True)
        # A short private directory: terminal-browser puts its unix sockets here.
        runtime = tempfile.mkdtemp(prefix="fpb.", dir="/tmp")
        env = self.child_env(runtime, profile)
        self.release()
        try:
            self.bridge = AgentBridge()
            env.update(self.bridge.env())
        except OSError:
            self.bridge = None
        spec = {"bin": self.bin, "url": url, "cwd": str(profile), "nonce": nonce, "env": env}
        path = launch / "spec.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(spec, handle)
        return {"path": path, "nonce": nonce}

    # ---- the select tool's bridge ----------------------------------------------------------
    def fds(self):
        return self.bridge.fds() if self.bridge is not None else []

    def pump(self, ready):
        if self.bridge is not None:
            self.picks.extend(self.bridge.pump(ready))

    def take_picks(self):
        """Picks the select tool handed over since the last call, as (connection, text, note);
        each must get `answer_pick`."""
        picks, self.picks = self.picks, []
        return picks

    def answer_pick(self, conn, taken):
        AgentBridge.reply(conn, 200 if taken else 409)

    def release(self):
        """Stop serving the select tool (pending picks are refused); the browser keeps running."""
        for conn, *_ in self.picks:
            AgentBridge.reply(conn, 409)
        self.picks = []
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None
