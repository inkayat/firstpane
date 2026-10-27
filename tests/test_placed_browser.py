"""The Browser tab's browser outside the tab: a new tab in the front Ghostty window, or a Herdr pane
beside Firstpane (the placement chosen in Settings).

Each test names the production break it exists to catch. Herdr is a fake socket API and Ghostty
is a fake `osascript` at the transport boundary; the launcher really runs (through /bin/sh, as
the pane's shell or Ghostty's command would) with a fake terminal-browser that keeps running
until it is told to stop. No real Ghostty window or tab, Herdr pane or browser is touched.
"""

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from firstpane.embed import CaptureError
from firstpane.placed import LAUNCHER, START_TIMEOUT, PlacedBrowser
from tests.support import Clock, CmdResult, ghostty_argv, remove_launch_runtimes

# Like terminal-browser 0.13.4, the command started in the window or pane keeps a browser process
# that runs in a session of its own: it outlives the command when only the command is signalled.
FAKE_TB = """#!{python}
import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid(), "daemon": daemon.pid}},
          open({out!r}, "w"))
while True:
    time.sleep(0.05)
"""


def _running(pid):
    stat = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


class FakeHost:
    """Herdr's pane API over an in-memory layout, and Ghostty's AppleScript at `osascript`.

    Ghostty has one host window "HW-1" holding a sentinel tab "HT-1" (the user's own tab). A
    Ghostty `new tab in` adds a tab to the window whose id the script was given, or to the front
    window when that id is "", and runs its command the way Ghostty on macOS does
    (bash -c "exec -l <command>", tests.support.ghostty_argv), in the given directory,
    with Ghostty's own environment. Closing a terminal closes only its tab; any AppleScript that
    would open a window or close a window or tab is a test failure. `ghostty_answer` plays what
    osascript says back: "ok" (window, tab and terminal ids), "error-after-window" (the tab
    opened, then the script failed: the -1708 seen in the operational run) or "error" (nothing
    opened)."""

    def __init__(self):
        self.calls = []
        self.runs = []
        self.panes = {"w1:p1": {"pane_id": "w1:p1", "terminal_id": "term_fp", "tab_id": "w1:t1", "workspace_id": "w1"}}
        self.shell_pids = {}
        self.typed = {}
        self.next = 2
        self.split_tab = "w1:t1"
        self.ghostty_answer = "ok"
        self.frontmost = True
        self.front = "HW-1"
        self.host_tabs = {"HW-1": ["HT-1"]}
        self.tabs = []
        self.focused = []
        self.procs = []
        self.closed = []

    # ---- Herdr ---------------------------------------------------------------------
    def socket_call(self, method, params, timeout):
        self.calls.append((method, dict(params)))
        pane_id = params.get("pane_id")
        if method == "pane.split":
            new = f"w1:p{self.next}"
            self.next += 1
            pane = {"pane_id": new, "terminal_id": f"term_{new}", "tab_id": self.split_tab, "workspace_id": "w1"}
            self.panes[new] = pane
            return {"id": "t", "result": {"type": "pane_info", "pane": dict(pane)}}
        if pane_id not in self.panes:
            return {"id": "t", "error": {"code": "pane_not_found", "message": "missing"}}
        if method == "pane.get":
            return {"id": "t", "result": {"type": "pane_info", "pane": dict(self.panes[pane_id])}}
        if method == "pane.send_input":
            self.typed[pane_id] = params.get("text")
            return {"id": "t", "result": {"type": "ok"}}
        if method == "pane.process_info":
            return {"id": "t", "result": {"type": "pane_process_info", "process_info": {
                "pane_id": pane_id, "shell_pid": self.shell_pids.get(pane_id), "foreground_processes": []}}}
        if method == "pane.focus":
            return {"id": "t", "result": {"type": "pane_focused", "pane_id": pane_id}}
        if method == "pane.close":
            del self.panes[pane_id]
            return {"id": "t", "result": {"type": "ok"}}
        raise AssertionError("unexpected method " + method)

    def methods(self):
        return [method for method, _ in self.calls]

    # ---- Ghostty ---------------------------------------------------------------------
    def run(self, argv, stdin=None, env=None, timeout=None, cwd=None, detach=False):
        self.runs.append(list(argv))
        if argv[0] != "osascript":
            raise AssertionError("unexpected command " + argv[0])
        script, args = argv[2], argv[3:]
        if "new window" in script or "close window" in script or "close tab" in script:
            raise AssertionError("AppleScript that opens or closes a whole window or tab: " + script)
        if "new tab in" in script:
            if self.ghostty_answer == "error":
                return CmdResult(1, "", "execution error: Ghostty got an error: Can’t continue. (-1708)")
            command, directory, target = args
            if target:
                if target not in self.host_tabs:
                    return CmdResult(0, f"refused: Ghostty has no window {target}\n", "")
                window = target
            elif not self.frontmost:
                return CmdResult(0, "refused: Ghostty is not the front application\n", "")
            elif self.front is None:
                return CmdResult(0, "refused: Ghostty has no window\n", "")
            else:
                window = self.front
            ghostty_env = {"PATH": "/usr/bin:/bin", "HOME": "/personal/home", "TERM_PROGRAM": "ghostty",
                           "GHOSTTY_RESOURCES_DIR": "/Applications/Ghostty.app/Contents/Resources/ghostty",
                           "TERM": "xterm-ghostty"}
            proc = subprocess.Popen(ghostty_argv(command), cwd=directory, env=ghostty_env, start_new_session=True)
            self.procs.append(proc)
            terminal = f"GT-{len(self.tabs) + 1}"
            tab = f"HT-{len(self.tabs) + 2}"
            self.host_tabs[window].append(tab)
            self.tabs.append({"terminal": terminal, "tab": tab, "window": window, "command": command,
                              "cwd": directory, "pid": proc.pid})
            if self.ghostty_answer == "error-after-window":
                return CmdResult(1, "", "execution error: Ghostty got an error: Can’t continue. (-1708)")
            return CmdResult(0, f"{window}\n{tab}\n{terminal}\n", "")
        if "close (" in script:
            tab = next((item for item in self.tabs if item["terminal"] == args[0]), None)
            self.closed.append((args[0], tab is not None and _running(tab["pid"])))
            if tab is not None and tab["tab"] in self.host_tabs[tab["window"]]:
                self.host_tabs[tab["window"]].remove(tab["tab"])
            return CmdResult(0, "", "")
        if "focus" in script:
            self.focused.append(args[0])
            return CmdResult(0, "", "")
        raise AssertionError("unexpected AppleScript " + script)

    def new_tabs(self):
        return [run for run in self.runs if "new tab in" in run[2]]

    def stop(self):
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(5)


class PlacedBrowserTests(unittest.TestCase):
    URL = "file:///fixture/page.html?q=a b&x='$(touch pwned)'"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        tb = self.root / "tb"
        (tb / "bin").mkdir(parents=True)
        self.out = self.root / "tb-run.json"
        script = tb / "bin" / "terminal-browser"
        script.write_text(FAKE_TB.format(python=sys.executable, out=str(self.out)))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        (tb / "VERSION").write_text("v9.9.9\n")
        self.tb = tb
        self.host = FakeHost()
        self.addCleanup(lambda: self.host.stop())
        self.addCleanup(remove_launch_runtimes, self.root)
        self.profile = self.root / "state" / "browser" / "w1-p1"
        self.clock = Clock()

    def browser(self, own="w1:p1", root=None, platform="darwin"):
        item = PlacedBrowser(str(self.tb) if root is None else root, self.profile, own, self.host,
                             env={"LANG": "tr_TR.UTF-8", "HOME": "/personal/home"}, clock=self.clock,
                             platform=platform)
        self.addCleanup(item.release)
        return item

    def alpha(self, browser):
        return browser.context(("fm", "alpha-task"), "alpha-task", "/src/wt-a")

    def beta(self, browser):
        return browser.context(("fm", "beta-task"), "beta-task", "/src/wt-b")

    def run_pane(self, pane_id):
        """Be the new pane's shell: run exactly the typed text, with a Herdr pane environment."""
        self.out.unlink(missing_ok=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": "/personal/home", "HERDR_PANE_ID": pane_id,
               "HERDR_SOCKET_PATH": "/x/herdr.sock", "HERDR_ENV": "1", "TERM_PROGRAM": "ghostty", "KITTY_WINDOW_ID": "3"}
        proc = subprocess.Popen(["/bin/sh", "-c", self.host.typed[pane_id]], env=env, cwd=self.tmp.name,
                                start_new_session=True)
        self.host.procs.append(proc)
        self.host.shell_pids[pane_id] = proc.pid
        self.wait_run()
        return proc.pid

    def wait_run(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.out.exists():
                try:
                    run = json.loads(self.out.read_text())
                    self.addCleanup(self.kill_quietly, run["daemon"])
                    return run
                except ValueError:
                    pass
            time.sleep(0.02)
        raise AssertionError("the fake terminal-browser never ran")

    @staticmethod
    def kill_quietly(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def wait_open(self, browser):
        deadline = time.time() + 20
        while time.time() < deadline:
            browser.refresh()
            if browser.state == "open":
                return
            time.sleep(0.02)
        raise AssertionError(f"never open: {browser.state} {browser.describe()}")

    def beside(self, context=None, browser=None):
        browser = browser or self.browser()
        context = context or self.alpha(browser)
        status = browser.open(context, "beside")
        pane_id = browser.record["pane_id"]
        pid = self.run_pane(pane_id)
        browser.refresh()
        return browser, status, pane_id, pid

    def native(self, context=None, browser=None):
        browser = browser or self.browser()
        context = context or self.alpha(browser)
        self.out.unlink(missing_ok=True)
        status = browser.open(context, "ghostty")
        run = self.wait_run()
        self.wait_open(browser)
        return browser, status, run

    @staticmethod
    def page_at(browser, url):
        """The launched browser now shows `url` (the user typed it in the browser itself)."""
        browser.capture_fn = lambda *args, **kwargs: {"url": url, "title": "T", "selection": ""}

    def spec(self, browser):
        return json.loads(Path(browser.record["spec"]).read_text())

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait_dead(self, pid):
        deadline = time.time() + 10
        while time.time() < deadline and self.alive(pid):
            for proc in self.host.procs:
                proc.poll()
            time.sleep(0.02)
        return not self.alive(pid)

    # ---- a tab in the front Ghostty window -----------------------------------------------
    def test_native_open_adds_one_tab_to_the_front_window_running_only_the_fixed_launcher(self):
        # Break: the browser opens a separate OS window instead of a tab of the window in
        # front, the page address (here with spaces, quotes and $(...)) reaches the command
        # Ghostty runs through /bin/sh, the browser starts in the caller's directory, or one
        # open adds more than one tab.
        browser, _, _ = self.native()
        self.page_at(browser, self.URL)
        browser.close()
        self.out.unlink()
        status = browser.open(self.alpha(browser), "ghostty")
        self.wait_run()
        tabs = self.host.new_tabs()
        self.assertEqual(len(tabs), 2)
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1", "HT-3"]})
        self.assertEqual((browser.record["window"], browser.record["tab"], browser.record["terminal"]),
                         ("HW-1", "HT-3", "GT-2"))
        command, directory, target = tabs[-1][3:]
        self.assertEqual(target, "")  # Firstpane itself always asks for the front window
        spec = browser.record["spec"]
        # Ghostty prepends `exec -l` itself (tests.support.ghostty_argv); a second `exec` is a
        # command named "exec" that does not exist, and the tab shows a launch error.
        self.assertEqual(shlex.split(command), [sys.executable, "-I", "-B", str(LAUNCHER), spec])
        self.assertNotIn("pwned", command)
        self.assertNotIn("fixture", command)
        self.assertEqual(os.path.realpath(directory), os.path.realpath(self.profile / "contexts" / self.alpha(browser)["id"]))
        self.assertEqual(stat.S_IMODE(os.stat(spec).st_mode), 0o600)
        self.assertEqual(self.spec(browser)["url"], self.URL)
        self.assertIn("Ghostty tab", status)

    def test_native_browser_runs_in_its_private_profile_without_ghostty_or_herdr_variables(self):
        # Break: the browser inherits Ghostty's or Herdr's environment (its adapters then type
        # into agents or edit terminal config), the personal HOME, an embed proxy, or loses the
        # select tool's bridge back to this Firstpane.
        browser, _, run = self.native()
        self.assertEqual(run["argv"], ["open"])
        env = run["env"]
        for name in env:
            self.assertFalse(name.startswith(("HERDR_", "PIXEL_", "KITTY_", "GHOSTTY_")), name)
        self.assertNotIn("TERM_PROGRAM", env)
        profile = self.profile / "contexts" / self.alpha(browser)["id"]
        self.assertTrue(env["HOME"].startswith(str(profile)))
        self.assertEqual(env["LANG"], "tr_TR.UTF-8")
        self.assertTrue(env["TERMINAL_BROWSER_AGENT_BRIDGE"].startswith("http://127.0.0.1:"))
        self.assertTrue(env["TERMINAL_BROWSER_AGENT_TOKEN"])
        self.assertEqual(os.path.realpath(run["cwd"]), os.path.realpath(profile))
        self.assertEqual(browser.state, "open")
        self.assertEqual(browser.record["pid"], run["pid"])

    def test_reopening_the_shown_context_reuses_its_tab_and_focus_brings_that_tab_forward(self):
        # Break: every Browser-tab visit adds another Ghostty tab (and Chromium) on the same
        # profile, or `f` focuses some other Ghostty terminal.
        browser, _, run = self.native()
        status = browser.open(self.alpha(browser), "ghostty")
        self.assertIn("Ghostty tab", status)
        self.assertEqual(len(self.host.new_tabs()), 1)
        self.assertEqual(browser.record["pid"], run["pid"])
        self.assertIn("brought", browser.focus(self.alpha(browser)))
        self.assertEqual(self.host.focused, ["GT-1"])

    def test_a_failed_script_that_still_opened_a_tab_is_never_retried_into_a_duplicate(self):
        # Break (the operational run's duplicate tab): osascript reports an error although
        # Ghostty already opened the tab, and the next open asks for a second one.
        self.host.ghostty_answer = "error-after-window"
        browser = self.browser()
        status = browser.open(self.alpha(browser), "ghostty")
        self.assertIn("not confirmed", status)
        again = browser.open(self.alpha(browser), "ghostty")
        self.assertIn("starting", again)
        refused = browser.open(self.beta(browser), "ghostty")
        self.assertIn("busy", refused)
        self.assertEqual(len(self.host.new_tabs()), 1)
        run = self.wait_run()
        self.wait_open(browser)
        self.assertEqual(browser.record["pid"], run["pid"])
        browser.open(self.alpha(browser), "ghostty")
        self.assertEqual(len(self.host.new_tabs()), 1)
        self.assertIn("cannot bring", browser.focus(self.alpha(browser)))
        status = browser.close()
        self.assertIn("closed (profile kept)", status)
        self.assertIn("close its Ghostty tab there", status)
        self.assertEqual(self.host.closed, [])
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1", "HT-2"]})

    def test_without_ghostty_in_front_or_with_no_window_nothing_opens_and_it_says_why(self):
        # Break: Firstpane guesses a window (another app is in front, so the window Ghostty
        # calls front may not be the one Firstpane runs in) or falls back to a new OS window,
        # or a refusal leaves a record that blocks the next open.
        browser = self.browser()
        self.host.frontmost = False
        status = browser.open(self.alpha(browser), "ghostty")
        self.assertIn("not the front application", status)
        self.assertIn("Beside Firstpane", status)
        self.host.frontmost, self.host.front = True, None
        self.assertIn("no window", browser.open(self.alpha(browser), "ghostty"))
        self.assertIsNone(browser.record)
        self.assertEqual(self.host.tabs, [])
        self.host.front = "HW-1"
        browser.open(self.alpha(browser), "ghostty")
        self.wait_run()
        self.wait_open(browser)
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1", "HT-2"]})

    def test_a_given_window_id_gets_the_tab_even_with_another_window_in_front_and_a_gone_one_opens_nothing(self):
        # Break: the window id is dropped and the tab lands in whatever window is in front (the
        # wrong-window path), or a window that is gone falls back to the front or a new window.
        browser = self.browser()
        self.host.host_tabs["HW-2"] = ["HT-9"]
        browser.ghostty_window = "HW-2"
        browser.open(self.alpha(browser), "ghostty")
        self.wait_run()
        self.wait_open(browser)
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1"], "HW-2": ["HT-9", "HT-2"]})
        self.assertEqual(browser.record["window"], "HW-2")
        browser.close()
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1"], "HW-2": ["HT-9"]})
        browser.ghostty_window = "HW-3"
        status = browser.open(self.alpha(browser), "ghostty")
        self.assertIn("no window HW-3", status)
        self.assertIsNone(browser.record)
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1"], "HW-2": ["HT-9"]})

    def test_a_tab_that_never_started_is_given_up_after_the_start_timeout_then_reopened_once(self):
        # Break: a script error that opened nothing blocks the browser forever, or is reopened
        # at once (racing a tab that may still come up).
        self.host.ghostty_answer = "error"
        browser = self.browser()
        browser.open(self.alpha(browser), "ghostty")
        self.assertIn("busy", browser.open(self.beta(browser), "ghostty"))
        self.clock.t += START_TIMEOUT + 1
        browser.refresh()
        self.assertEqual(browser.state, "failed")
        self.assertIn("did not start", browser.describe())
        self.host.ghostty_answer = "ok"
        self.out.unlink(missing_ok=True)
        browser.open(self.alpha(browser), "ghostty")
        self.wait_run()
        self.wait_open(browser)
        self.assertEqual(len(self.host.new_tabs()), 2)

    def test_a_native_tab_needs_macos_and_osascript_and_says_what_to_choose_instead(self):
        # Break: on Linux Firstpane tries AppleScript anyway, or a missing osascript leaves a
        # record that blocks every later open.
        browser = self.browser(platform="linux")
        status = browser.open(self.alpha(browser), "ghostty")
        self.assertIn("macOS", status)
        self.assertIn("Beside Firstpane", status)
        self.assertEqual(self.host.runs, [])
        self.assertIsNone(browser.record)
        mac = self.browser()
        self.host.run = lambda argv, **kwargs: CmdResult(127, "", "[Errno 2] No such file or directory: 'osascript'")
        status = mac.open(self.alpha(mac), "ghostty")
        self.assertIn("osascript", status)
        self.assertIsNone(mac.record)
        self.assertEqual(mac.state, "failed")

    def test_close_ends_only_the_launched_browser_and_its_own_tab_and_keeps_address_and_profile(self):
        # Break: `x` leaves the native browser running, ends some other process, closes the
        # host window or the user's sentinel tab, deletes the private profile, or forgets the
        # page address so the next open starts blank.
        browser, _, run = self.native()
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(bystander.kill)
        self.page_at(browser, "http://127.0.0.1:8001/app")
        status = browser.close()
        self.assertIn("closed", status)
        self.assertTrue(self.wait_dead(run["pid"]))
        self.assertTrue(self.wait_dead(run["daemon"]))
        self.assertIsNone(bystander.poll())
        # The tab is Firstpane's own (its terminal id); it is closed once its browser ended, so
        # Ghostty never asks to confirm closing a running process. The host window and the
        # user's tab stay.
        self.assertEqual(self.host.closed, [("GT-1", False)])
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1"]})
        self.assertEqual(self.alpha(browser)["url"], "http://127.0.0.1:8001/app")
        self.assertTrue((self.profile / "contexts" / self.alpha(browser)["id"] / "tbconfig" / "settings.json").exists())
        self.assertIsNone(browser.record)
        self.out.unlink()
        browser.open(self.alpha(browser), "ghostty")
        self.assertEqual(self.spec(browser)["url"], "http://127.0.0.1:8001/app")

    def test_a_tab_the_user_closed_is_forgotten_and_a_reused_pid_is_never_adopted(self):
        # Break: after the user closes the Ghostty tab, its old pid (now some other program)
        # is captured from or signalled as if it were the browser.
        browser, _, run = self.native()
        os.kill(run["pid"], signal.SIGTERM)
        self.assertTrue(self.wait_dead(run["pid"]))
        browser.refresh()
        self.assertEqual(browser.state, "closed")
        self.assertIsNone(browser.record)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(other.kill)
        second, _, _ = self.native(browser=self.browser())
        launched = Path(second.record["spec"]).parent / "launched.json"
        launched.write_text(json.dumps({"pid": other.pid, "nonce": second.record["nonce"]}))
        second.refresh()
        self.assertNotEqual(second.state, "open")
        with self.assertRaises(CaptureError):
            second.capture(self.alpha(second))
        self.assertIn("no browser", second.close())
        self.assertIsNone(other.poll())

    def test_another_context_closes_the_previous_native_browser_before_opening_its_own(self):
        # Break: two browsers stack up, the new one runs in the previous context's profile, or
        # the previous context's page address is forgotten.
        browser, _, first = self.native()
        self.page_at(browser, "http://127.0.0.1:8001/app")
        self.out.unlink()
        browser.open(self.beta(browser), "ghostty")
        second = self.wait_run()
        self.assertTrue(self.wait_dead(first["pid"]))
        self.assertTrue(second["env"]["HOME"].startswith(str(self.profile / "contexts" / self.beta(browser)["id"])))
        self.assertEqual(self.alpha(browser)["url"], "http://127.0.0.1:8001/app")
        self.assertEqual(browser.owner()["label"], "beta-task")

    def test_changing_the_placement_moves_the_context_without_two_browsers_on_one_profile(self):
        # Break: the beside pane opens while the native window of the same profile still runs
        # (two Chromium instances on one profile), or the native one is left behind.
        browser, _, run = self.native()
        browser.open(self.alpha(browser), "beside")
        self.assertTrue(self.wait_dead(run["pid"]))
        self.assertEqual(browser.record["placement"], "beside")
        self.assertEqual(self.host.methods().count("pane.split"), 1)

    def test_a_restarted_firstpane_reuses_the_proven_native_tab_and_says_its_select_tool_is_unlinked(self):
        # Break: restarting Firstpane opens a duplicate window, or claims the select tool still
        # reaches Firstpane when its bridge ended with the previous process.
        browser, _, run = self.native()
        again = self.browser()
        again.refresh()
        self.assertEqual(again.state, "open")
        self.assertIn("select tool", again.describe())
        again.open(self.alpha(again), "ghostty")
        self.assertEqual(len(self.host.new_tabs()), 1)
        self.assertEqual(again.record["pid"], run["pid"])

    def test_the_select_tool_bridge_hands_over_picks_with_its_token_only(self):
        # Break: a pick from the launched browser never reaches Firstpane, or any local process
        # that finds the loopback port can open a FirstMate message without the token.
        browser, _, run = self.native()
        bridge, token = run["env"]["TERMINAL_BROWSER_AGENT_BRIDGE"], run["env"]["TERMINAL_BROWSER_AGENT_TOKEN"]
        import threading
        import urllib.error
        import urllib.request
        results = []

        def post(auth):
            request = urllib.request.Request(bridge + "/agent-text", method="POST", data=b'{"text": "[<p>x</p>]"}',
                                             headers={"authorization": f"Bearer {auth}"})
            try:
                results.append(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10).status)
            except urllib.error.HTTPError as error:
                results.append(error.code)

        for auth in ("guess", token):
            thread = threading.Thread(target=post, args=(auth,))
            thread.start()
            picks = []
            deadline = time.time() + 10
            while thread.is_alive() and time.time() < deadline:
                import select
                ready, _, _ = select.select(browser.fds(), [], [], 0.05)
                browser.pump(ready)
                picks.extend(browser.take_picks())
                for conn, *_ in picks:
                    browser.answer_pick(conn, True)
            thread.join(5)
            if auth == "guess":
                self.assertEqual(picks, [])
            else:
                self.assertEqual([pick[1] for pick in picks], ["[<p>x</p>]"])
        self.assertEqual(results, [401, 200])

    def test_capture_is_bound_to_the_launched_native_browser_process(self):
        # Break: capture reads a registry entry or CDP port of another browser than the one this
        # Firstpane launched, or another context's page.
        browser, _, run = self.native()
        seen = {}

        def fake_capture(tb_bin, tb_root, env, timeout=20, owner_pid=None):
            seen.update(root=tb_root, env=env, owner=owner_pid)
            return {"url": self.URL, "title": "T", "selection": "s"}

        browser.capture_fn = fake_capture
        with self.assertRaises(CaptureError) as caught:
            browser.capture(self.beta(browser))
        self.assertIn("alpha-task", str(caught.exception))
        browser.capture(self.alpha(browser))
        self.assertEqual(seen["owner"], run["pid"])
        self.assertEqual(seen["root"], os.path.realpath(self.tb))
        self.assertEqual(seen["env"]["HOME"], run["env"]["HOME"])

    # ---- beside Firstpane (the pre-embedding Herdr pane) -------------------------------
    def test_beside_splits_this_pane_right_without_focus_and_types_only_the_fixed_launcher(self):
        # Break: the browser opens in another tab or steals focus, or a remembered address is
        # typed into a shell where it can run code.
        browser, _, first, _ = self.beside()
        self.page_at(browser, self.URL)
        browser.close()
        status = browser.open(self.alpha(browser), "beside")
        split = [params for method, params in self.host.calls if method == "pane.split"]
        self.assertEqual(len(split), 2)
        self.assertEqual((split[1]["target_pane_id"], split[1]["direction"], split[1]["focus"]), ("w1:p1", "right", False))
        pane_id = browser.record["pane_id"]
        self.assertIn(pane_id, status)
        typed = self.host.typed[pane_id]
        self.assertEqual(shlex.split(typed), ["exec", sys.executable, "-I", "-B", str(LAUNCHER), browser.record["spec"]])
        self.assertNotIn("pwned", typed)
        self.assertEqual(self.spec(browser)["url"], self.URL)
        send = [params for method, params in self.host.calls if method == "pane.send_input"]
        self.assertEqual(send[-1]["keys"], ["enter"])

    def test_beside_runs_the_browser_in_its_private_profile_without_pane_variables(self):
        # Break: the beside browser inherits HERDR_*/kitty/Ghostty variables or the personal HOME.
        browser, _, _, pid = self.beside()
        run = json.loads(self.out.read_text())
        self.assertEqual(run["pid"], pid)
        for name in run["env"]:
            self.assertFalse(name.startswith(("HERDR_", "PIXEL_", "KITTY_", "GHOSTTY_")), name)
        self.assertTrue(run["env"]["HOME"].startswith(str(self.profile / "contexts" / self.alpha(browser)["id"])))
        self.assertEqual(browser.state, "open")

    def test_beside_reuses_its_pane_and_replaces_it_only_for_another_context(self):
        # Break: every Browser-tab visit adds a pane, or another context's browser reuses the
        # previous profile or forgets its address.
        browser, _, pane_id, _ = self.beside()
        self.assertIn(pane_id, browser.open(self.alpha(browser), "beside"))
        self.assertEqual(self.host.methods().count("pane.split"), 1)
        self.page_at(browser, "http://127.0.0.1:8001/app")
        browser.open(self.beta(browser), "beside")
        self.assertEqual([p for m, p in self.host.calls if m == "pane.close"], [{"pane_id": pane_id}])
        self.assertTrue(self.spec(browser)["env"]["HOME"].startswith(str(self.profile / "contexts" / self.beta(browser)["id"])))
        self.assertEqual(self.alpha(browser)["url"], "http://127.0.0.1:8001/app")

    def test_beside_never_adopts_a_replaced_pane_or_a_pane_running_something_else(self):
        # Break: a pane that later got the same id (another agent's shell) is focused, captured
        # from or closed as if it were the browser.
        browser, _, pane_id, _ = self.beside()
        self.host.shell_pids[pane_id] = os.getpid()
        browser.refresh()
        self.assertNotEqual(browser.state, "open")
        with self.assertRaises(CaptureError):
            browser.capture(self.alpha(browser))
        self.host.panes[pane_id]["terminal_id"] = "term_someone_else"
        browser.refresh()
        self.assertEqual(browser.state, "closed")
        self.assertIn("no browser", browser.focus(self.alpha(browser)))
        self.assertIn("no browser", browser.close())
        self.assertNotIn("pane.close", self.host.methods())
        self.assertNotIn("pane.focus", self.host.methods())

    def test_beside_with_herdr_unreachable_keeps_the_record_and_refuses_every_action(self):
        # Break: one socket timeout is read as "pane closed" and the next open splits a second
        # browser pane next to the live one.
        browser, _, pane_id, _ = self.beside()
        saved = (self.profile / "companion.json").read_text()
        original = self.host.socket_call

        def unreachable(method, params, timeout):
            raise TimeoutError("slow")

        self.host.socket_call = unreachable
        browser.refresh()
        self.assertEqual(browser.state, "unreachable")
        for refused in (browser.focus(self.alpha(browser)), browser.close(),
                        browser.open(self.alpha(browser), "beside"), browser.open(self.beta(browser), "ghostty")):
            self.assertIn("unverified", refused)
        self.assertEqual((self.profile / "companion.json").read_text(), saved)
        self.assertEqual(self.host.new_tabs(), [])
        self.host.socket_call = original
        browser.refresh()
        self.assertEqual(browser.state, "open")
        self.assertIn(pane_id, browser.focus(self.alpha(browser)))

    def test_beside_refuses_without_its_own_pane_and_a_split_outside_this_tab(self):
        # Break: with no HERDR_PANE_ID the browser lands beside whatever has focus, or Firstpane
        # types into a pane Herdr placed in another tab.
        browser = self.browser(own=None)
        self.assertIn("HERDR_PANE_ID", browser.open(self.alpha(browser), "beside"))
        self.assertEqual(self.host.calls, [])
        self.host.split_tab = "w1:t9"
        other = self.browser()
        status = other.open(self.alpha(other), "beside")
        self.assertIn("refused", status)
        self.assertNotIn("pane.send_input", self.host.methods())
        self.assertNotIn("pane.close", self.host.methods())

    def test_an_unconfigured_install_starts_nothing_in_either_placement(self):
        # Break: a missing install is looked up on PATH, or a pane or window is opened anyway.
        for placement in ("ghostty", "beside"):
            browser = self.browser(root="")
            self.assertIn("terminal_browser_root", browser.open(self.alpha(browser), placement))
        self.assertEqual((self.host.calls, self.host.runs), ([], []))

    def test_a_pre_embedding_beside_record_is_still_read_and_reproven(self):
        # Break: a companion.json written by the earlier beside-only Firstpane (no placement
        # field) is ignored, so the live pane is duplicated, or it is read as a native window.
        browser, _, pane_id, _ = self.beside()
        record = json.loads((self.profile / "companion.json").read_text())
        record.pop("placement")
        (self.profile / "companion.json").write_text(json.dumps(record))
        again = self.browser()
        again.refresh()
        self.assertEqual(again.state, "open")
        self.assertIn(pane_id, again.open(self.alpha(again), "beside"))
        self.assertEqual(self.host.methods().count("pane.split"), 1)


if __name__ == "__main__":
    unittest.main()
