"""Browser tab: each agent's own browser, opened where Settings says (a new tab in the front Ghostty
window by default, or drawn inside Firstpane's Browser tab), driven through the App.

Herdr is the session snapshot plus the FirstMate primary's pi message box (tests.support.FakePi).
Ghostty is a fake `osascript` at the transport boundary whose `new tab in` the front window really
runs its command through /bin/sh. Git runs for real against fixture worktrees, one per agent.
terminal-browser is a fake that records how it was started, serves a registry entry pointing at a
loopback fake CDP endpoint, keeps a helper child, and runs until it is told to stop. Started
inside Firstpane it also speaks the embed host protocol: it joins over PIXEL_EMBED, logs every
host message with its arrival time, reports its placement and writes one shared-memory frame to
PIXEL_TTY for each init or size message; while a `stream` file sits in its install root it also
streams 15 shared-memory frames a second, each naming a new real POSIX object it logs. The
terminal's answers to Firstpane's frame queries are fed in as input
bytes. "The user types an address in the browser" is played by rewriting that registry tab and
the fake page. "The user picks an element with the toolbar's select tool" is played by a
`grab.json` the fake then sends the way terminal-browser 0.13.4's sendGrab does: to the agent
bridge in its environment (POST /agent-text with the bearer token), else nowhere. No real
browser, Ghostty window or tab, pane, profile, inbox or network is used.
"""

import base64
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

from firstpane import browser, embed, placed
from firstpane.agents_view import humanize
from firstpane.app import App
from firstpane.config import Config
from firstpane.embed import CaptureError
from firstpane.frames import FILE_QUERY_ID, SHM_QUERY_ID
from firstpane.kitty import PLACEHOLDER
from firstpane.transport import LiveTransport
from tests.gitfixture import GitFixture
from tests.support import (Clock, CmdResult, FakePi, FakeTerminal, FakeTransport, agent_for, note_json, pane,
                           ghostty_argv, remove_launch_runtimes, selected_line, snapshot, workspace, write_meta)
from tests.test_embed import FakeCdp, frame, shm_exists, shm_remove

FRAME = b"\x1b_Ga=T,f=32,t=s,s=4,v=4,i=%d,q=2,U=1,c=4,r=1;L3B4LTEtMC0x\x1b\\"
FAKE_TB = r'''#!{python}
import base64, json, os, signal, socket, subprocess, sys, threading, time, urllib.error, urllib.request
home, runtime = os.environ["HOME"], os.environ["XDG_RUNTIME_DIR"]
def log(name, item):
    with open(os.path.join(home, name), "a") as handle:
        handle.write(json.dumps(item) + "\n")
if sys.argv[1:] == ["ls", "--all", "--json"]:
    sys.stdout.write(open(os.path.join(runtime, "registry.json")).read())
    sys.exit(0)
if sys.argv[1:2] != ["open"]:
    sys.exit(2)
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
def stop(*_):
    helper.kill()
    os._exit(0)
signal.signal(signal.SIGTERM, stop)
url = sys.argv[2] if len(sys.argv) > 2 else "about:blank"
registry = {{"browsers": [{{"key": "fake-1", "pid": os.getpid(), "cdpPort": {port},
             "tabs": [{{"id": 1, "url": url, "active": True, "targetId": "PAGE1"}}]}}]}}
open(os.path.join(runtime, "registry.json"), "w").write(json.dumps(registry))
# Logged last: tests wait for this line and then read the registry.
log("launch.jsonl", {{"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid(), "helper": helper.pid}})
def stream(tty, image):
    # While `stream` exists next to bin/: 15 fps of shared-memory frames, one new POSIX object per
    # frame (as terminal-browser), each name logged before its frame is written.
    import _posixshmem
    flag = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))), "stream")
    n = 0
    while os.path.exists(flag):
        name = "/fpt-%d-%d" % (os.getpid(), n)
        fd = _posixshmem.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode=0o600)
        os.ftruncate(fd, 4)
        os.close(fd)
        log("shm.jsonl", name)
        payload = base64.b64encode(name.encode()).decode()
        os.write(tty, ("\x1b_Ga=T,f=32,s=4,v=4,t=s,i=%d,U=1,c=4,r=1,q=2;%s\x1b\\" % (image, payload)).encode())
        n += 1
        time.sleep(1 / 15)
    log("shm.jsonl", None)
def embed_host():
    # The embed client: join Firstpane's socket, then place the page and write one frame to the
    # pty for the init and for every size change (each message is logged with its arrival time).
    host = socket.socket(socket.AF_UNIX)
    host.connect(os.environ["PIXEL_EMBED"])
    host.sendall(json.dumps({{"type": "join", "pane": "p", "name": "fake", "pid": os.getpid()}}).encode() + b"\n")
    tty = os.open(os.environ["PIXEL_TTY"], os.O_WRONLY)
    image = None
    for line in host.makefile("rb"):
        message = json.loads(line)
        log("host.jsonl", dict(message, at=time.monotonic()))
        if message["type"] == "init":
            image = message["imageId"]
            threading.Thread(target=stream, args=(tty, image), daemon=True).start()
        if message["type"] in ("init", "size"):
            host.sendall(json.dumps({{"type": "placed", "imageId": image, "cols": message["cols"],
                                     "rows": message["rows"]}}).encode() + b"\n")
            os.write(tty, {frame!r} % image)
if os.environ.get("PIXEL_EMBED"):
    threading.Thread(target=embed_host, daemon=True).start()
def grab_send(pick):
    # 0.13.4 sendGrab: the agent bridge first (POST <bridge>/agent-text, bearer token);
    # without one (or refused) nothing reaches an agent, the pick is only copied.
    bridge = os.environ.get("TERMINAL_BROWSER_AGENT_BRIDGE")
    if not bridge:
        log("bridge.jsonl", {{"status": None}})
        return
    token = pick.get("token", os.environ.get("TERMINAL_BROWSER_AGENT_TOKEN"))
    payload = {{"text": pick["text"]}}
    if pick.get("context"):
        payload["context"] = pick["context"]
    request = urllib.request.Request(bridge.rstrip("/") + "/agent-text", method="POST",
                                     data=json.dumps(payload).encode(),
                                     headers={{"content-type": "application/json", "authorization": f"Bearer {{token}}"}})
    try:
        status = urllib.request.build_opener(urllib.request.ProxyHandler({{}})).open(request, timeout=30).status
    except urllib.error.HTTPError as error:
        status = error.code
    log("bridge.jsonl", {{"status": status}})
pick = os.path.join(home, "grab.json")
while True:
    if os.path.exists(pick):
        # The select tool is on (toolbar icon) and the user clicked an element.
        chosen = json.load(open(pick))
        os.remove(pick)
        grab_send(chosen)
    time.sleep(0.03)
'''
URL_A = "http://127.0.0.1:8001/"
URL_B = "http://127.0.0.1:8002/"
SHM_OK = f"\x1b_Gi={SHM_QUERY_ID};OK\x1b\\".encode()
NOTHING_ACCEPTED = f"\x1b_Gi={SHM_QUERY_ID};EINVAL:bad\x1b\\\x1b_Gi={FILE_QUERY_ID};EINVAL:bad\x1b\\".encode()


def plain(frame):
    return re.sub(r"\x1b\[[0-9;]*m", "", frame)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


class Transport(FakeTransport):
    """Fake Herdr snapshot, inbox and Ghostty; real git. Nothing else runs."""

    def __init__(self):
        super().__init__()
        self.live = LiveTransport(None)
        self.inbox = []
        self.osascript = []
        self.focused = []
        self.closed = []
        self.procs = []

    def spawn(self, command, cwd, argv=None):
        proc = subprocess.Popen(argv or ["/bin/sh", "-c", command], cwd=cwd, start_new_session=True,
                                env={"PATH": "/usr/bin:/bin", "HOME": "/personal/home", "TERM_PROGRAM": "ghostty"})
        self.procs.append(proc)
        return proc

    def run(self, argv, stdin=None, env=None, timeout=None, cwd=None, detach=False):
        if argv and str(argv[0]).endswith("fm-inbox.sh"):
            self.inbox.append({"argv": list(argv), "stdin": stdin})
            return CmdResult(0, note_json("created"), "")
        if argv and argv[0] == "osascript":
            self.osascript.append(list(argv))
            script, args = argv[2], argv[3:]
            if "new tab in" in script:
                self.spawn(args[0], args[1], argv=ghostty_argv(args[0]))
                return CmdResult(0, f"HW-1\nHT-{len(self.osascript) + 1}\nGT-{len(self.osascript)}\n", "")
            (self.closed if "close (" in script else self.focused).append(args[0])
            return CmdResult(0, "", "")
        if argv and argv[0] != "git":
            raise AssertionError("unexpected command " + str(argv[0]))
        return self.live.run(argv, stdin=stdin, env=env, timeout=timeout, cwd=cwd, detach=detach)

    def windows(self):
        """The Ghostty tabs Firstpane asked for (`new tab in` the front window)."""
        return [call for call in self.osascript if "new tab in" in call[2]]

    def stop(self):
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(5)


class BrowserTabTests(unittest.TestCase):
    URL = "file:///fixture/page.html"

    def setUp(self):
        # The real capture runs subprocesses; the production 3s bound lets a loaded runner
        # time out and silently lose the remembered address.
        for module, name in ((placed, "TIMEOUT"), (browser, "CAPTURE_TIMEOUT")):
            patcher = mock.patch.object(module, name, 60)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fx = GitFixture()
        self.addCleanup(self.fx.cleanup)
        self.worktrees = {"alpha-task": self.fx.worktree("wt-a", "fm/alpha-task"),
                          "beta-task": self.fx.worktree("wt-b", "fm/beta-task")}
        self.page = {"id": "PAGE1", "url": self.URL, "href": self.URL, "title": "Fixture \x1b]0;pwn\x07Title",
                     "selection": "marked \x1b[2Jtext\nsecond"}
        self.cdp = FakeCdp(self.page)
        self.addCleanup(self.cdp.close)
        self.tb = self.root / "tb"
        (self.tb / "bin").mkdir(parents=True)
        script = self.tb / "bin" / "terminal-browser"
        script.write_text(FAKE_TB.format(python=sys.executable, port=self.cdp.port, frame=FRAME))
        script.chmod(0o755)
        (self.tb / "VERSION").write_text("v9.9.9\n")
        self.home = self.root / "home"
        self.agent_panes = {
            "alpha-task": pane(pane_id="w2:p1", terminal_id="term_a", workspace_id="w2", tab_id="w2:t1", agent_status="idle"),
            "beta-task": pane(pane_id="w3:p1", terminal_id="term_b", workspace_id="w3", tab_id="w3:t1", agent_status="working"),
        }
        for task in self.agent_panes:
            self.write_meta(task, self.worktrees[task])
        self.config = Config(
            firstmate_home=str(self.home),
            firstmate_root=str(self.root / "tool"),
            socket_path="/Users/x/.config/herdr/sessions/lab/herdr.sock",
            herdr_pane_id="w1:p1",
            terminal_browser_root=str(self.tb),
            project_allowlist=(str(self.fx.base),),
        )
        (self.root / "tool" / "bin").mkdir(parents=True)
        (self.root / "tool" / "bin" / "fm-inbox.sh").write_text("#!/bin/sh\n")
        self.transport = Transport()
        self.addCleanup(self.transport.stop)
        self.addCleanup(remove_launch_runtimes, self.root)
        rows = list(self.agent_panes.values())
        self.transport.snapshot = snapshot([workspace("w2", "alpha"), workspace("w3", "beta", number=2)], rows,
                                           [agent_for(row) for row in rows])
        self.main = FakePi(self.transport, self.home)
        self.pane_bytes = b""
        self.app = self.make_app(self.config)

    def tearDown(self):
        self.app.browser.shutdown(wait=1)
        self.app.frames.cleanup()
        self.app.placed.release()

    def write_meta(self, task, worktree):
        row = self.agent_panes[task]
        write_meta(self.home, task, project=str(self.fx.primary), herdr_session="lab",
                   herdr_workspace_id=row["workspace_id"], herdr_tab_id=row["tab_id"],
                   herdr_pane_id=row["pane_id"], endpoint_task_id=task, worktree=str(worktree))

    def make_app(self, config, cols=110):
        self.clock = Clock()
        app = App(config=config, transport=self.transport, state_dir=self.root / "state",
                  env=dict(self.fx.env, LANG="en_US.UTF-8"), clock=self.clock, terminal=FakeTerminal(),
                  rows=30, cols=cols)
        # Ghostty is faked at the transport here, so the Ghostty placement is exercised on any host
        # (CI runs Linux, where the real one refuses; test_placed_browser covers that refusal).
        app.placed.platform = "darwin"
        app.refresh(manual=True)
        return app

    # ---- helpers ---------------------------------------------------------------------
    def frame(self):
        return plain(self.app.render())

    def select(self, task):
        self.app.handle_key("1")
        for _ in range(30):
            self.app.handle_key("k")
        for _ in range(30):
            if humanize(task) in selected_line(self.frame()):
                self.app.handle_key("enter")
                return
            self.app.handle_key("j")
        raise AssertionError(self.frame())

    def pump(self, until, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.placed.refresh()
            self.app.render()
            self.app.layout_ready(time.monotonic())  # as the main loop does before each write
            fds = self.app.io_fds()
            ready, _, _ = select.select(fds, [], [], 0.05) if fds else ([], [], [])
            self.pane_bytes += self.app.pump(ready, time.monotonic())
            if until():
                return
            if not fds:
                time.sleep(0.02)
        raise AssertionError(f"timed out; frame:\n{self.frame()}")

    def drive(self, seconds, actions):
        """The main loop's order for `seconds`: render, write the screen only when
        `layout_ready` allows it, wait for input or the next deadline, pump the browser. At each
        offset in `actions` (seconds from now) its callable runs, as input would. Returns events
        (kind, monotonic time, detail): `screen` (image rows on each written screen), `frame`
        (one per page frame written to the pane) and `action`."""
        start = time.monotonic()
        pending = sorted(actions.items())
        events = []
        while time.monotonic() - start < seconds:
            now = time.monotonic()
            while pending and now - start >= pending[0][0]:
                events.append(("action", now, pending[0][0]))
                pending.pop(0)[1]()
            screen = self.app.render()
            now = time.monotonic()
            if self.app.layout_ready(now):
                events.append(("screen", now, [i for i, line in enumerate(screen.split("\n")) if PLACEHOLDER in line]))
            deadline = self.app.io_deadline()
            wait = 0.02 if deadline is None else max(0.0, min(0.02, deadline - time.monotonic()))
            ready, _, _ = select.select(self.app.io_fds(), [], [], wait)
            now = time.monotonic()
            data = self.app.pump(ready, now)
            self.pane_bytes += data
            events += [("frame", now, None)] * data.count(b"\x1b_Ga=T")
        return events

    def negotiate(self, answer=SHM_OK):
        """Firstpane asks its terminal at start; the terminal answers in the input stream."""
        self.app.terminal_queries()
        self.app.handle_bytes(answer)

    def spec_env(self):
        if self.app.browser.running:
            return self.app.browser.child_env_used
        return json.loads(Path(self.app.placed.record["spec"]).read_text())["env"]

    def profile_home(self):
        return Path(self.spec_env()["HOME"])

    def logged(self, name, home=None):
        path = (home or self.profile_home()) / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def launch(self, home=None):
        return [entry for entry in self.logged("launch.jsonl", home) if "argv" in entry][-1]

    def host(self):
        """What Firstpane told the browser inside it, in order."""
        return self.logged("host.jsonl")

    def page_keys(self):
        return [message.get("text") or message["key"] for message in self.host() if message["type"] == "key"]

    def running(self):
        if self.app.browser_placement == "embedded":
            shown = self.app.browser.grid is not None
        else:
            shown = self.app.placed.state == "open"
        return shown and bool(self.logged("launch.jsonl"))

    def open_tab(self, task, url=None):
        """Select `task`, choose the Browser tab and wait until its browser runs; then go to
        `url` (default the fixture page) in it."""
        self.select(task)
        self.app.handle_key("3")
        self.pump(self.running)
        self.navigate(url or self.URL)

    def navigate(self, url):
        """The user types `url` into the shown browser (its own address bar)."""
        registry = Path(self.spec_env()["XDG_RUNTIME_DIR"]) / "registry.json"
        data = json.loads(registry.read_text())
        data["browsers"][0]["tabs"][0]["url"] = url
        registry.write_text(json.dumps(data))
        self.page.update(url=url, href=url)

    def choose(self, *keys):
        """Settings (6): j/k to a placement, Enter chooses it."""
        for key in ("6", *keys, "enter"):
            self.app.handle_key(key)

    def choose_inside(self):
        self.choose("j")

    def choose_ghostty(self):
        self.choose("k")

    def image_rows(self):
        lines = self.app.render().split("\n")
        return [index + 1 for index, line in enumerate(lines) if PLACEHOLDER in line]

    def capture(self, key="a"):
        self.app.handle_key(key)
        deadline = time.time() + 10
        while self.app.capture_job is not None and time.time() < deadline:
            self.app.poll()

    def pick(self, text, token=None, context=None):
        """With the select tool on, the user clicks an element on the page; the browser hands the
        pick to the agent bridge Firstpane gave it. Returns the bridge's HTTP status (None: no
        bridge at all)."""
        item = {"text": text}
        if token is not None:
            item["token"] = token
        if context:
            item["context"] = context
        before = len(self.logged("bridge.jsonl"))
        (self.profile_home() / "grab.json").write_text(json.dumps(item))
        self.pump(lambda: len(self.logged("bridge.jsonl")) > before, timeout=20)
        return self.logged("bridge.jsonl")[-1]["status"]

    @staticmethod
    def finish(future, fn):
        try:
            future.set_result(fn())
        except Exception as error:
            future.set_exception(error)

    def stored_drafts(self):
        return list((self.root / "state" / "drafts").glob("open-*.json"))

    def defer_submits(self):
        pending, inline, defer = [], self.app.submit, []

        def submit(fn):
            if defer:
                future = Future()
                pending.append((future, fn))
                return future
            return inline(fn)

        self.app.submit = submit
        return pending, defer

    # ---- where the browser opens -----------------------------------------------------
    def test_the_browser_tab_opens_the_selected_agents_browser_in_a_ghostty_tab_by_default(self):
        # Break: the default still draws the slow in-tab page (or opens a pane), one tab visit
        # asks Ghostty for several tabs, or the browser inherits HERDR_* and the personal HOME.
        self.open_tab("alpha-task")
        self.assertEqual(len(self.transport.windows()), 1)
        self.assertNotIn("pane.split", [call["method"] for call in self.transport.socket_calls])
        self.assertNotIn(PLACEHOLDER, self.app.render())
        self.assertIn("in a Ghostty tab", self.frame())
        launch = self.launch()
        self.assertEqual(launch["argv"], ["open"])
        self.assertFalse([name for name in launch["env"] if name.startswith(("HERDR_", "PIXEL_"))])
        self.assertNotEqual(launch["env"]["HOME"], os.environ.get("HOME"))

    def test_with_inside_chosen_the_browser_tab_draws_the_agents_page_in_its_body(self):
        # Break: Inside Firstpane still opens a Ghostty tab or a Herdr pane, the page is not drawn
        # in the tab body (or covers only part of it), frames go inline although the terminal
        # takes shared memory, or the browser inherits HERDR_* and the personal HOME.
        self.negotiate()
        self.choose_inside()
        self.open_tab("alpha-task")
        self.assertEqual(self.transport.osascript, [])
        self.assertFalse([call for call in self.transport.socket_calls if call["method"] == "pane.split"])
        init = self.host()[0]
        self.assertEqual((init["type"], init["transport"]), ("init", "shm"))
        self.pump(lambda: FRAME % init["imageId"] in self.pane_bytes)
        rows = self.image_rows()
        self.assertEqual(rows, list(range(rows[0], 31)))
        self.assertEqual(self.app.render().split("\n")[rows[0] - 1].count(PLACEHOLDER), 109)
        launch = self.launch()
        self.assertEqual(launch["pid"], self.app.browser.child.pid)
        self.assertFalse([name for name in launch["env"] if name.startswith("HERDR_")])
        self.assertNotEqual(launch["env"]["HOME"], os.environ.get("HOME"))

    def test_a_long_message_above_the_page_inside_firstpane_takes_one_row(self):
        # Break: a status that wraps takes several rows from the page, so each message resizes
        # the page image (Herdr's fast image path is lost when the image changes size mid-frame).
        self.negotiate()
        self.choose_inside()
        self.open_tab("alpha-task")
        self.pump(lambda: self.image_rows()[-1] == 30)
        top = self.image_rows()[0]
        self.app.status = "a refusal that is much longer than the pane is wide " * 5
        self.pump(lambda: self.app.browser.grid["rows"] == 30 - top)
        self.assertEqual(self.image_rows(), list(range(top + 1, 31)))

    def test_a_streaming_page_is_bounded_and_layout_changes_wait_for_a_quiet_window(self):
        # Break: every frame of a 15 fps page reaches the pane (Herdr's client queues them ahead
        # of every key once its fast image path is lost); entering, leaving, the status line or
        # a size message lands right after a frame (Herdr turns that path off for good when the
        # image's placement or size changes while a frame is still on its way); a frame follows
        # a layout change at once; the wait blocks or starves the keys; or a dropped frame's
        # shared memory is left behind while a forwarded one is unlinked.
        quiet = embed.LAYOUT_QUIET
        (self.tb / "stream").write_text("")
        self.negotiate()
        self.choose_inside()
        self.open_tab("alpha-task")
        self.pump(lambda: self.image_rows() and len(self.logged("shm.jsonl")) > 3)
        home = self.profile_home()
        status = lambda text: lambda: setattr(self.app, "status", text)
        events = self.drive(6.0, {0.8: status("a refusal"), 1.8: status(""), 2.8: lambda: self.app.handle_key("1"),
                                  3.8: lambda: self.app.handle_key("3"), 4.8: (self.tb / "stream").unlink})
        self.app.browser.shutdown(wait=1)
        names = [name for name in self.logged("shm.jsonl", home) if name]
        self.addCleanup(lambda: [shm_remove(name) for name in names])

        frames = [at for kind, at, _ in events if kind == "frame"]
        screens = [(at, rows) for kind, at, rows in events if kind == "screen"]
        changes = [(at, before, after) for (_, before), (at, after) in zip(screens, screens[1:]) if before != after]
        self.assertGreaterEqual(len(changes), 4, changes)  # status on, status off, leave, enter
        for at, before, after in changes:
            if before:  # the image was placed: nothing may have gone out within the quiet window
                self.assertGreaterEqual(at - max([f for f in frames if f <= at] + [0]), quiet, (at, before, after))
            if after:
                self.assertGreaterEqual(min([f for f in frames if f > at] + [at + quiet]), at + quiet)
        sizes = [message["at"] for message in self.logged("host.jsonl", home) if message["type"] == "size"]
        self.assertGreaterEqual(len(sizes), 2)
        for at in sizes:
            self.assertFalse([f for f in frames if at - quiet <= f < at], at)
        self.assertGreaterEqual(len(names), 50)
        self.assertLessEqual(len(frames), 6.0 / 0.5 + 2)
        for key_at in (2.8, 3.8):
            pressed = next(at for kind, at, offset in events if kind == "action" and offset == key_at)
            before = [rows for at, rows in screens if at < pressed][-1]
            shown = next(at for at, rows in screens if at > pressed and rows != before)
            self.assertLessEqual(shown - pressed, quiet + 0.15)
        forwarded = {base64.b64decode(payload).decode() for payload in re.findall(rb"t=s,i=\d+,U=1,c=4,r=1,q=2;([^\x1b]+)", self.pane_bytes)}
        for name in names:
            self.assertEqual(shm_exists(name), name in forwarded, name)

    def test_a_placement_change_moves_the_open_browser_on_the_next_open_without_a_second_instance(self):
        # Break: after choosing Inside Firstpane the Ghostty tab keeps running next to the page in
        # the tab (two browsers on one profile), going back leaves the in-tab browser running,
        # the new placement is never used, or the moved browser opens at the address the agent
        # had before the one it was just at.
        self.negotiate()
        self.open_tab("alpha-task", URL_A)
        first = self.launch()
        self.choose_inside()
        self.app.handle_key("3")
        self.pump(self.running)
        self.assertFalse(alive(first["pid"]))
        self.assertFalse(alive(first["helper"]))
        self.assertEqual(self.transport.closed, ["GT-1"])
        inside = self.launch()
        self.assertEqual(inside["argv"], ["open", URL_A])
        self.navigate(URL_B)
        self.choose_ghostty()
        self.app.handle_key("3")
        self.pump(lambda: self.running() and self.launch()["pid"] != inside["pid"])
        self.assertFalse(alive(inside["pid"]))
        self.assertFalse(alive(inside["helper"]))
        self.assertEqual(self.image_rows(), [])
        self.assertEqual(len(self.transport.windows()), 2)
        self.assertEqual(self.launch()["argv"], ["open", URL_B])

    def test_inside_firstpane_f_gives_the_page_the_keys_until_ctrl_o(self):
        # Break: `f` asks Ghostty for a tab instead, keys meant for the page are taken as
        # Firstpane shortcuts (3 switches tabs, x closes the browser), or Ctrl-O keeps them.
        self.negotiate()
        self.choose_inside()
        self.open_tab("alpha-task")
        self.app.handle_key("f")
        self.assertEqual(self.app.focus, "browser")
        for key in ("x", "3"):
            self.app.handle_key(key)
        self.pump(lambda: self.page_keys() == ["x", "3"])
        self.assertTrue(self.app.browser.running)
        self.app.handle_key("ctrl-o")
        self.assertEqual(self.app.focus, "list")
        self.assertEqual(self.transport.osascript, [])

    def test_a_browser_still_waiting_for_the_terminal_does_not_start_once_ghostty_tab_is_chosen(self):
        # Break: the in-tab browser, still waiting for the terminal's frame answer, starts later
        # next to the Ghostty tab the user switched to (two browsers on one profile).
        self.app.terminal_queries()
        self.choose_inside()
        self.select("alpha-task")
        self.app.handle_key("3")
        self.choose_ghostty()
        self.app.handle_key("3")
        self.pump(self.running)
        self.app.handle_bytes(SHM_OK)
        self.pump(lambda: True, timeout=0.5)
        self.assertIsNone(self.app.browser.child)
        self.assertEqual(len(self.logged("launch.jsonl")), 1)

    def test_a_terminal_without_fast_frames_gets_the_page_inline_and_says_it_is_slow(self):
        # Break: the in-tab browser starts before the terminal answered, or a terminal that takes
        # neither shared memory nor files gets inline frames silently, as if they were as fast.
        self.app.terminal_queries()
        self.choose_inside()
        self.select("alpha-task")
        self.app.handle_key("3")
        self.assertIsNone(self.app.browser.child)
        self.assertIn("checking how frames reach this terminal", self.frame())
        self.app.handle_bytes(NOTHING_ACCEPTED)
        self.pump(self.running)
        self.assertEqual(self.host()[0]["transport"], "inline")
        self.assertIn("inline frames: slow", self.frame())

    def test_x_closes_the_browser_inside_firstpane_and_its_address_reopens(self):
        # Break: `x` leaves the in-tab browser (or its page image) behind, or the next open
        # starts blank.
        self.negotiate()
        self.choose_inside()
        self.open_tab("alpha-task", URL_A)
        pid = self.launch()["pid"]
        self.app.handle_key("x")
        self.assertIn("closed (profile kept)", self.frame())
        self.assertFalse(alive(pid))
        self.assertEqual(self.image_rows(), [])
        self.app.handle_key("3")
        self.pump(lambda: self.running() and self.launch()["pid"] != pid)
        self.assertEqual(self.launch()["argv"], ["open", URL_A])

    def test_choosing_the_browser_tab_again_keeps_the_running_browser_and_its_page(self):
        # Break: a second click on the tab (or `3` again) opens another window or restarts the
        # browser and throws away the page the user went to.
        self.open_tab("alpha-task")
        pid = self.launch()["pid"]
        first = self.frame().splitlines()[0]
        self.app.handle_mouse(0, first.index("Browser") + 1, 1)
        self.app.handle_key("2")
        self.app.handle_key("3")
        self.pump(lambda: True, timeout=0.5)
        self.assertEqual(self.launch()["pid"], pid)
        self.assertEqual(len(self.logged("launch.jsonl")), 1)
        self.assertEqual(len(self.transport.windows()), 1)

    def test_hiding_the_browser_tab_in_settings_leaves_the_running_browser_and_its_page(self):
        # Break: hiding the tab in Settings ends the agent's browser (or opens a second one
        # later), losing the page the user is on.
        self.open_tab("alpha-task")
        pid = self.launch()["pid"]
        self.app.handle_key("6")
        row = next(i for i, line in enumerate(self.frame().splitlines(), 1) if "[x] Browser" in line)
        self.app.handle_mouse(0, self.frame().splitlines()[row - 1].index("[x]") + 1, row)
        self.assertNotIn("Browser", self.frame().splitlines()[0])
        self.assertEqual(self.app.tab, "settings")
        self.pump(lambda: True, timeout=0.5)
        self.assertTrue(alive(pid))
        self.assertEqual(self.app.placed.state, "open")
        row = next(i for i, line in enumerate(self.frame().splitlines(), 1) if "[ ] Browser" in line)
        self.app.handle_mouse(0, self.frame().splitlines()[row - 1].index("[ ]") + 1, row)
        self.app.handle_key("3")
        self.pump(lambda: True, timeout=0.5)
        self.assertEqual(self.launch()["pid"], pid)
        self.assertEqual(len(self.logged("launch.jsonl")), 1)

    def test_hiding_the_browser_tab_keeps_the_browser_inside_firstpane_and_its_page(self):
        # Break: hiding the tab ends the in-tab browser Firstpane owns (or starts a second one
        # when it is shown again), so the page the user was on is lost.
        self.negotiate()
        self.choose_inside()
        self.open_tab("alpha-task")
        pid = self.launch()["pid"]
        self.app.handle_key("6")
        row = next(i for i, line in enumerate(self.frame().splitlines(), 1) if "[x] Browser" in line)
        self.app.handle_mouse(0, self.frame().splitlines()[row - 1].index("[x]") + 1, row)
        self.pump(lambda: True, timeout=0.5)
        self.assertEqual((self.app.tab, self.app.browser.child.pid), ("settings", pid))
        self.assertEqual(self.image_rows(), [])
        row = next(i for i, line in enumerate(self.frame().splitlines(), 1) if "[ ] Browser" in line)
        self.app.handle_mouse(0, self.frame().splitlines()[row - 1].index("[ ]") + 1, row)
        self.app.handle_key("3")
        self.pump(lambda: bool(self.image_rows()))
        self.assertEqual(self.app.browser.child.pid, pid)
        self.assertEqual(len(self.logged("launch.jsonl")), 1)

    def test_f_brings_the_shown_agents_window_forward(self):
        # Break: `f` focuses another Ghostty terminal, or does nothing.
        self.open_tab("alpha-task")
        self.app.handle_key("f")
        self.assertEqual(self.transport.focused, ["GT-1"])
        self.assertEqual(self.app.focus, "list")

    def test_without_a_selected_agent_the_tab_says_so_and_starts_nothing(self):
        # Break: with no agent selected the browser starts under no agent's profile.
        self.app.handle_key("3")
        self.assertIn("select an agent", self.frame())
        self.assertEqual(self.transport.osascript, [])

    def test_unconfigured_browser_says_so_and_starts_nothing(self):
        # Break: without a configured root Firstpane looks up terminal-browser on PATH.
        self.config.terminal_browser_root = None
        self.app = self.make_app(self.config)
        self.select("alpha-task")
        self.app.handle_key("3")
        self.assertIn("not configured", self.frame())
        self.assertEqual(self.transport.osascript, [])

    # ---- per-agent browsers ------------------------------------------------------------
    def test_each_agent_has_its_own_browser_profile_and_address_and_one_runs_at_a_time(self):
        # Break: switching agents keeps showing the other agent's page as the new agent's,
        # opens the other agent's address, reuses its profile, forgets where each page was,
        # or leaves the previous agent's browser (or its helper) running.
        self.open_tab("alpha-task")
        self.navigate(URL_A)
        alpha_home = self.profile_home()
        alpha = self.launch()
        self.select("beta-task")
        self.app.handle_key("3")
        self.pump(lambda: self.profile_home() != alpha_home and self.running())
        self.assertFalse(alive(alpha["pid"]))
        self.assertFalse(alive(alpha["helper"]))
        self.assertEqual(self.launch()["argv"], ["open"])
        self.navigate(URL_B)
        self.select("alpha-task")
        self.app.handle_key("3")
        self.pump(lambda: self.profile_home() == alpha_home and self.running() and self.launch()["pid"] != alpha["pid"])
        self.assertEqual(self.launch()["argv"], ["open", URL_A])
        self.select("beta-task")
        self.app.handle_key("3")
        self.pump(lambda: self.profile_home() != alpha_home and self.running() and len(self.logged("launch.jsonl")) == 2)
        self.assertEqual(self.launch()["argv"], ["open", URL_B])
        self.assertEqual(self.stored_drafts(), [])

    def test_x_closes_the_browser_and_remembers_its_address(self):
        # Break: `x` leaves the browser running, or the next open starts blank.
        self.open_tab("alpha-task")
        self.navigate(URL_A)
        pid = self.launch()["pid"]
        self.app.handle_key("x")
        self.assertIn("closed (profile kept)", self.frame())
        self.assertEqual(self.transport.closed, ["GT-1"])
        deadline = time.time() + 10
        while alive(pid) and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(alive(pid))
        self.app.handle_key("3")
        self.pump(lambda: self.running() and self.launch()["pid"] != pid)
        self.assertEqual(self.launch()["argv"], ["open", URL_A])

    def test_two_worktrees_at_one_address_are_flagged_as_one_server(self):
        # Break: beta's browser silently shows whatever server alpha started on that port.
        self.open_tab("alpha-task")
        self.navigate(URL_A)
        self.select("beta-task")
        self.app.handle_key("3")
        self.pump(self.running)
        self.navigate(URL_A)
        self.select("alpha-task")
        self.app.handle_key("3")
        self.pump(lambda: self.running() and self.app.placed.owner()["label"] == "alpha-task")
        self.select("beta-task")
        self.app.handle_key("3")
        self.assertIn("same address as alpha-task", self.frame())

    def test_a_worktree_change_gives_the_agent_a_new_browser(self):
        # Break: after the agent moves to another worktree its browser still claims the old
        # worktree's page as the agent's current one, or reopens the old address.
        self.open_tab("alpha-task")
        self.navigate(URL_A)
        self.write_meta("alpha-task", self.fx.worktree("wt-c", "fm/alpha-two"))
        self.app.refresh(manual=True)
        self.app.handle_key("a")
        self.assertIn("refused", self.frame())
        self.app.handle_key("3")
        self.pump(lambda: "wt-c" in (self.app.placed.owner() or {}).get("source", "") and self.running())
        self.assertEqual(self.launch()["argv"], ["open"])

    # ---- capture and inline message --------------------------------------------------
    def test_capture_opens_an_inline_message_for_the_selection_and_fills_firstmates_box(self):
        # Break: the selection lands in a footer composer, hostile page text reaches the
        # screen, the message is not bound to the agent, or what lands in FirstMate's message
        # box differs from the preview or is submitted.
        self.open_tab("alpha-task")
        self.capture()
        item = self.app.draft["attachments"][0]
        self.assertEqual((item["kind"], item["url"], item["title"], item["browser_key"]),
                         ("browser", self.URL, "Fixture Title", "fake-1"))
        self.assertEqual(item["selection"], "marked text\nsecond")
        frame = self.frame()
        self.assertIn("marked text", frame)
        self.assertNotIn("pwn", frame)
        self.assertEqual(self.app.focus, "composer")
        self.app.handle_bytes("bak şuna".encode())
        self.app.handle_key("enter")
        preview = self.frame()
        self.assertIn(f'[1] browser "Fixture Title"  {self.URL}  instance fake-1 tab 1', preview)
        self.assertIn("```text\nmarked text\nsecond\n```", preview)
        self.assertIn("agent: alpha-task", preview)
        self.app.handle_key("y")
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(len(self.main.received), 1)
        self.assertEqual(self.main.submitted, [])
        body = self.main.received[0]
        self.assertIn("bak şuna", body)
        self.assertIn("marked text\nsecond", body)
        self.assertIn("agent: alpha-task", body)
        self.assertIn("press Enter there to send", self.frame())

    def test_ctrl_g_captures_like_a_in_the_browser_tab(self):
        # Break: Ctrl-G, the browser's own send-to-agent key, does nothing in Firstpane's tab.
        self.open_tab("alpha-task")
        self.capture("ctrl-g")
        self.assertEqual(self.app.draft["anchor"]["selection"], "marked text\nsecond")
        self.assertEqual(self.app.focus, "composer")

    def test_a_narrow_side_pane_says_why_a_transfer_was_refused(self):
        # Break: in a side pane the refusal is cut off or pushed out of view, so Enter seems to
        # do nothing and the draft just stays.
        self.app = self.make_app(self.config, cols=60)
        self.main.pane["agent_status"] = "working"
        self.open_tab("alpha-task")
        self.capture("ctrl-g")
        self.app.handle_bytes(b"look here")
        self.app.handle_key("enter")
        self.assertIn("not transferred: FirstMate is working", self.frame())
        self.assertTrue(all(len(line) <= 60 for line in self.frame().splitlines()))
        self.assertEqual(self.main.received, [])
        self.assertEqual(self.app.draft["body"], "look here")

    # ---- the toolbar's select tool -----------------------------------------------------
    def test_picking_an_element_with_the_select_tool_opens_a_firstmate_message_about_it(self):
        # Break: the select tool's pick goes nowhere (terminal-browser finds no agent and only
        # copies it) and no message screen opens; or the message loses the picked element or
        # its page, or what lands in FirstMate's box differs from the preview or is submitted.
        self.open_tab("alpha-task")
        self.assertEqual(self.pick('[<p id="line1">FPBM \x1b[2Jalpha</p> selector: #line1]'), 200)
        self.assertEqual(self.app.focus, "composer")
        anchor = self.app.draft["anchor"]
        self.assertEqual((anchor["url"], anchor["element"]),
                         (self.URL, '[<p id="line1">FPBM alpha</p> selector: #line1]'))
        self.assertIn('<p id="line1">FPBM alpha</p>', self.frame())
        self.app.handle_bytes(b"fix this paragraph")
        self.app.handle_key("enter")
        self.assertIn('[<p id="line1">FPBM alpha</p> selector: #line1]', self.frame())
        self.app.handle_key("y")
        self.assertEqual(len(self.main.received), 1)
        self.assertEqual(self.main.submitted, [])
        body = self.main.received[0]
        self.assertIn("fix this paragraph", body)
        self.assertIn('[<p id="line1">FPBM alpha</p> selector: #line1]', body)
        self.assertIn(self.URL, body)

    def test_the_select_tool_works_the_same_from_the_browser_inside_firstpane(self):
        # Break: only the Ghostty tab's browser gets the agent bridge, so picks made on the page
        # inside Firstpane go nowhere, or the message loses the page they were made on.
        self.negotiate()
        self.choose_inside()
        self.open_tab("alpha-task")
        self.assertEqual(self.pick('[<h1>Inside</h1> selector: h1]'), 200)
        self.assertEqual((self.app.draft["anchor"]["element"], self.app.draft["anchor"]["url"]),
                         ("[<h1>Inside</h1> selector: h1]", self.URL))

    def test_capture_reads_the_page_of_the_browser_inside_firstpane(self):
        # Break: `a` on the page inside Firstpane is refused as "no open browser" (only the
        # Ghostty tab's browser is read), or another browser's page is read.
        self.negotiate()
        self.choose_inside()
        self.open_tab("alpha-task")
        self.capture()
        anchor = self.app.draft["anchor"]
        self.assertEqual((anchor["url"], anchor["title"], anchor["selection"]),
                         (self.URL, "Fixture Title", "marked text\nsecond"))
        self.assertEqual(self.app.focus, "composer")

    def test_a_tooltip_note_is_previewed_with_the_element_and_not_retyped(self):
        # Break: the typed tooltip note is dropped, the element or page is missing, a second
        # editor is required, the note is pasted before y, or a second confirm pastes it again.
        self.open_tab("alpha-task")
        element = '[<div id="alt">Allt</div> selector: #alt]'
        self.assertEqual(self.pick(element, context="show this in English"), 200)
        self.assertNotEqual(self.app.focus, "composer")
        self.assertEqual(self.app.screen, "confirm-send")
        body = self.app.pending["body"]
        self.assertIn("show this in English", body)
        self.assertIn(element, body)
        self.assertIn(self.URL, body)
        self.assertEqual(self.main.received, [])
        self.app.handle_key("n")
        self.assertEqual(self.app.screen, "tree")
        self.assertEqual(self.app.draft["body"], "show this in English")
        self.assertEqual(self.main.received, [])
        self.app.handle_key("m")
        self.assertEqual(self.app.screen, "confirm-send")
        self.app.handle_key("y")
        self.assertEqual(self.main.submitted, [])
        self.assertEqual(len(self.main.received), 1)
        self.assertIn("show this in English", self.main.received[0])
        self.assertIn(element, self.main.received[0])

    def test_a_select_tool_message_waits_for_an_idle_empty_firstmate_box_and_is_put_there_once(self):
        # Break: a picked element's message is pasted while FirstMate works or over text the
        # captain is typing, the refusal is not on screen or loses the draft, or a second
        # message is pasted onto the first one.
        self.open_tab("alpha-task")
        self.assertEqual(self.pick('[<p id="line1">FPBM alpha</p> selector: #line1]'), 200)
        self.app.handle_bytes(b"look here")
        self.main.pane["agent_status"] = "working"
        self.app.handle_key("enter")
        self.assertIn("not transferred: FirstMate is working", self.frame())
        self.main.pane["agent_status"] = "idle"
        self.main.text = "captain typing"
        self.app.handle_key("enter")
        self.assertIn("not transferred: FirstMate's message box already has text", self.frame())
        self.assertEqual((self.main.text, self.main.received, self.app.draft["body"]), ("captain typing", [], "look here"))
        self.main.text = ""
        self.app.handle_key("enter")
        self.app.handle_key("y")
        self.assertEqual(len(self.main.received), 1)
        self.assertEqual(self.pick('[<h1>Other</h1> selector: h1]'), 200)
        self.app.handle_bytes(b"and this")
        self.app.handle_key("enter")
        self.assertIn("already has text", self.frame())
        self.assertEqual((len(self.main.received), self.main.submitted), (1, []))

    def test_the_select_tool_bridge_takes_picks_only_from_the_browser_it_launched(self):
        # Break: any local process that finds the loopback port can open a FirstMate message
        # (a missing or guessed token is accepted).
        self.open_tab("alpha-task")
        self.assertEqual(self.pick("[<div>x</div>]", token="guess"), 401)
        self.pump(lambda: True, timeout=0.5)
        self.assertIsNone(self.app.draft)

    def test_a_pick_whose_page_cannot_be_read_is_refused_back_and_said(self):
        # Break: the browser is told the pick was taken (it then says "Sent to agent") while
        # no message opened, or the reason is not on screen.
        self.open_tab("alpha-task")

        def unreadable(*args, **kwargs):
            raise CaptureError("CDP target page differs from the registry tab")

        def submit(fn):  # like the executor: the failure stays in the future
            future = Future()
            self.finish(future, fn)
            return future

        self.app.submit = submit
        self.app.placed.capture_fn = unreadable
        self.assertEqual(self.pick("[<div>x</div>]"), 409)
        self.assertIsNone(self.app.draft)
        self.assertIn("capture failed: CDP target page differs", self.frame())

    def test_capture_is_bound_to_the_launched_browser_process(self):
        # Break: capture reads a registry entry or CDP port of a different browser than the one
        # this Firstpane started.
        self.open_tab("alpha-task")
        seen = {}

        def fake_capture(tb_bin, tb_root, env, timeout=20, owner_pid=None):
            seen.update(root=tb_root, env=env, owner=owner_pid)
            return {"url": self.URL, "title": "T", "selection": "s"}

        self.app.placed.capture_fn = fake_capture
        self.capture()
        self.assertEqual(seen["owner"], self.launch()["pid"])
        self.assertEqual(seen["root"], os.path.realpath(self.tb))
        self.assertEqual(seen["env"]["HOME"], str(self.profile_home()))

    def test_a_capture_started_for_one_agent_never_lands_in_another_agents_draft(self):
        # Break: the selection read from alpha's page finishes after beta is selected and opens
        # a message bound to beta, or to alpha with beta's context.
        pending, defer = self.defer_submits()
        self.open_tab("alpha-task")
        defer.append(True)
        self.app.handle_key("a")
        defer.clear()
        self.select("beta-task")
        self.app.handle_key("3")
        future, fn = pending[0]
        self.finish(future, fn)
        self.app.poll()
        self.assertIn("discarded", self.frame())
        self.assertIsNone(self.app.draft)
        self.assertEqual(self.stored_drafts(), [])

    def test_a_capture_finishing_after_leaving_the_browser_tab_opens_no_hidden_editor(self):
        # Break: the capture finishes while the Changes tab is showing and opens the browser
        # message editor there, out of sight, so the next keys are typed into it.
        pending, defer = self.defer_submits()
        self.open_tab("alpha-task")
        defer.append(True)
        self.app.handle_key("a")
        defer.clear()
        self.app.handle_key("2")
        future, fn = pending[0]
        self.finish(future, fn)
        self.app.poll()
        self.assertIsNone(self.app.draft)
        self.assertIn("discarded", self.frame())

    def test_an_unsent_browser_message_is_kept_with_its_agent_and_reopens_with_m(self):
        # Break: leaving the editor drops the text, the kept draft stays drawn in the tab, it
        # shows up under another agent, a visit to Settings drops it, or `m` does not bring it
        # back for its own agent.
        self.open_tab("alpha-task")
        self.capture()
        self.app.handle_bytes(b"keep this")
        self.app.handle_key("esc")
        self.assertIsNone(self.app.draft)
        self.assertNotIn("keep this", self.frame())
        self.choose_inside()
        self.select("beta-task")
        self.app.handle_key("3")
        self.app.handle_key("m")
        self.assertIsNone(self.app.draft)
        self.select("alpha-task")
        self.app.handle_key("3")
        self.app.handle_key("m")
        self.assertEqual(self.app.draft["anchor"]["url"], self.URL)
        self.assertIn("keep this", self.frame())

    def test_a_noted_pick_keeps_the_note_when_the_agent_or_tab_changes(self):
        # Break: leaving Browser, or selecting another agent, before capture finishes drops the
        # typed note, or the note opens on the newly selected agent.
        self.open_tab("alpha-task")
        context, _ = self.app._browser_context()
        owner = self.app._agent_target()
        future = Future()
        self.app.capture_job = future
        self.app.capture_for = (context["id"], owner)
        element = '[<div id="alt">Allt</div> selector: #alt]'
        self.app.capture_pick = (object(), element, "show this in English")
        answered = []
        self.app.placed.answer_pick = lambda conn, taken: answered.append(taken)
        self.select("beta-task")
        self.app.handle_key("3")
        future.set_result({"url": self.URL, "title": "T", "selection": ""})
        self.app._poll_capture()
        self.assertEqual(answered, [True])
        self.assertIsNone(self.app.draft)
        stored = [path.read_text() for path in self.stored_drafts()]
        self.assertTrue(any("show this in English" in text and owner in text for text in stored))

    def test_a_second_noted_pick_of_the_same_element_stays_direct(self):
        # Break: a later noted pick of an element already in the editor drops direct and brings
        # the second editor back.
        self.open_tab("alpha-task")
        element = '[<div id="alt">Allt</div> selector: #alt]'
        self.assertEqual(self.pick(element), 200)
        self.assertFalse(self.app.draft["anchor"].get("direct"))
        anchor = self.app.draft["anchor"]
        future = Future()
        future.set_result({
            "url": anchor.get("url"), "title": anchor.get("title"), "selection": anchor.get("selection") or "",
            "browser_key": anchor.get("browser_key"), "tab_id": anchor.get("tab_id"),
            "target_id": anchor.get("target_id"), "captured_at": anchor.get("captured_at"),
        })
        context, _ = self.app._browser_context()
        self.app.capture_job = future
        self.app.capture_for = (context["id"], self.app._agent_target())
        self.app.capture_pick = (object(), element, "show this in English")
        self.app.placed.answer_pick = lambda conn, taken: None
        self.app._poll_capture()
        self.assertTrue(self.app.draft["anchor"].get("direct"))
        self.assertEqual(self.app.draft["body"], "show this in English")
        self.assertNotEqual(self.app.focus, "composer")

    def test_the_browser_tab_has_no_footer_composer(self):
        # Break: the removed bottom message area comes back on the Browser tab.
        self.select("alpha-task")
        self.app.handle_key("3")
        self.assertNotIn("write to FirstMate", self.frame())


class LayoutFenceTests(unittest.TestCase):
    def test_a_pane_that_keeps_resizing_and_a_streaming_page_both_keep_going(self):
        # Break: while the image's rows keep changing (a resize drag), every change holds the
        # frames back so the page freezes until resizing stops, or frames keep every change
        # from being written; or a change lands within the quiet window of a frame or change.
        quiet = embed.LAYOUT_QUIET
        page = browser.Browser(None, tempfile.mkdtemp())
        self.addCleanup(os.rmdir, page.base)
        frames = [frame(n, image=browser.IMAGE_ID, chunks=1, repeat=1) for n in range(60)]
        arrivals = {round(n * 1000 / 15 / 10) * 10: frames[n] for n in range(60)}  # 15 fps, 4 s
        commits, forwarded, written = [], [], None
        for ms in range(0, 4001, 10):
            now = ms / 1000
            view = (1, 10 + (ms // 10) % 2, 100)  # the image's rows change on every tick
            if page.place(view, now) and view != written:
                commits.append(now)
                written = view
            out = page.stream.feed(arrivals[ms], now) if ms in arrivals else page.stream.due(now)
            forwarded += [now] * out.count(b"\x1b_Ga=T")
        self.assertGreaterEqual(len(forwarded), 6)  # about one a second at least, not just the first
        self.assertGreaterEqual(len(commits), 6)
        for at in commits[1:]:
            before = [t for t in forwarded + commits if t < at]
            self.assertGreaterEqual(at - max(before), quiet - 1e-9, at)


if __name__ == "__main__":
    unittest.main()
