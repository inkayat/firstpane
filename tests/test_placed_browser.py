"""The Browser tab's browser outside Firstpane: a new tab in the front Ghostty window (the Ghostty
tab placement chosen in Settings).

Each test names the production break it exists to catch. Ghostty is a fake `osascript` at the
transport boundary; the launcher really runs (through /bin/sh, as Ghostty's command would) with
a fake terminal-browser that keeps running until it is told to stop. No real Ghostty window or
tab, Herdr pane or browser is touched.
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

# Like terminal-browser 0.13.4, the command started in the tab keeps a browser process that runs
# in a session of its own: it outlives the command when only the command is signalled.
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
    """Ghostty's AppleScript at `osascript`.

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
        self.runs = []
        self.ghostty_answer = "ok"
        self.frontmost = True
        self.front = "HW-1"
        self.host_tabs = {"HW-1": ["HT-1"]}
        self.tabs = []
        self.focused = []
        self.procs = []
        self.closed = []

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

    def browser(self, root=None, platform="darwin"):
        item = PlacedBrowser(str(self.tb) if root is None else root, self.profile, self.host,
                             env={"LANG": "tr_TR.UTF-8", "HOME": "/personal/home"}, clock=self.clock,
                             platform=platform)
        self.addCleanup(item.release)
        return item

    def alpha(self, browser):
        return browser.context(("fm", "alpha-task"), "alpha-task", "/src/wt-a")

    def beta(self, browser):
        return browser.context(("fm", "beta-task"), "beta-task", "/src/wt-b")

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

    def native(self, context=None, browser=None):
        browser = browser or self.browser()
        context = context or self.alpha(browser)
        self.out.unlink(missing_ok=True)
        status = browser.open(context)
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
        status = browser.open(self.alpha(browser))
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
        status = browser.open(self.alpha(browser))
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
        status = browser.open(self.alpha(browser))
        self.assertIn("not confirmed", status)
        again = browser.open(self.alpha(browser))
        self.assertIn("starting", again)
        refused = browser.open(self.beta(browser))
        self.assertIn("busy", refused)
        self.assertEqual(len(self.host.new_tabs()), 1)
        run = self.wait_run()
        self.wait_open(browser)
        self.assertEqual(browser.record["pid"], run["pid"])
        browser.open(self.alpha(browser))
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
        status = browser.open(self.alpha(browser))
        self.assertIn("not the front application", status)
        self.assertIn("Inside Firstpane", status)
        self.host.frontmost, self.host.front = True, None
        self.assertIn("no window", browser.open(self.alpha(browser)))
        self.assertIsNone(browser.record)
        self.assertEqual(self.host.tabs, [])
        self.host.front = "HW-1"
        browser.open(self.alpha(browser))
        self.wait_run()
        self.wait_open(browser)
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1", "HT-2"]})

    def test_a_given_window_id_gets_the_tab_even_with_another_window_in_front_and_a_gone_one_opens_nothing(self):
        # Break: the window id is dropped and the tab lands in whatever window is in front (the
        # wrong-window path), or a window that is gone falls back to the front or a new window.
        browser = self.browser()
        self.host.host_tabs["HW-2"] = ["HT-9"]
        browser.ghostty_window = "HW-2"
        browser.open(self.alpha(browser))
        self.wait_run()
        self.wait_open(browser)
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1"], "HW-2": ["HT-9", "HT-2"]})
        self.assertEqual(browser.record["window"], "HW-2")
        browser.close()
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1"], "HW-2": ["HT-9"]})
        browser.ghostty_window = "HW-3"
        status = browser.open(self.alpha(browser))
        self.assertIn("no window HW-3", status)
        self.assertIsNone(browser.record)
        self.assertEqual(self.host.host_tabs, {"HW-1": ["HT-1"], "HW-2": ["HT-9"]})

    def test_a_tab_that_never_started_is_given_up_after_the_start_timeout_then_reopened_once(self):
        # Break: a script error that opened nothing blocks the browser forever, or is reopened
        # at once (racing a tab that may still come up).
        self.host.ghostty_answer = "error"
        browser = self.browser()
        browser.open(self.alpha(browser))
        self.assertIn("busy", browser.open(self.beta(browser)))
        self.clock.t += START_TIMEOUT + 1
        browser.refresh()
        self.assertEqual(browser.state, "failed")
        self.assertIn("did not start", browser.describe())
        self.host.ghostty_answer = "ok"
        self.out.unlink(missing_ok=True)
        browser.open(self.alpha(browser))
        self.wait_run()
        self.wait_open(browser)
        self.assertEqual(len(self.host.new_tabs()), 2)

    def test_a_native_tab_needs_macos_and_osascript_and_says_what_to_choose_instead(self):
        # Break: on Linux Firstpane tries AppleScript anyway, or a missing osascript leaves a
        # record that blocks every later open.
        browser = self.browser(platform="linux")
        status = browser.open(self.alpha(browser))
        self.assertIn("macOS", status)
        self.assertIn("Inside Firstpane", status)
        self.assertEqual(self.host.runs, [])
        self.assertIsNone(browser.record)
        mac = self.browser()
        self.host.run = lambda argv, **kwargs: CmdResult(127, "", "[Errno 2] No such file or directory: 'osascript'")
        status = mac.open(self.alpha(mac))
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
        browser.open(self.alpha(browser))
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
        browser.open(self.beta(browser))
        second = self.wait_run()
        self.assertTrue(self.wait_dead(first["pid"]))
        self.assertTrue(second["env"]["HOME"].startswith(str(self.profile / "contexts" / self.beta(browser)["id"])))
        self.assertEqual(self.alpha(browser)["url"], "http://127.0.0.1:8001/app")
        self.assertEqual(browser.owner()["label"], "beta-task")

    def test_a_restarted_firstpane_reuses_the_proven_native_tab_and_says_its_select_tool_is_unlinked(self):
        # Break: restarting Firstpane opens a duplicate window, or claims the select tool still
        # reaches Firstpane when its bridge ended with the previous process.
        browser, _, run = self.native()
        again = self.browser()
        again.refresh()
        self.assertEqual(again.state, "open")
        self.assertIn("select tool", again.describe())
        again.open(self.alpha(again))
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

    def test_an_unconfigured_install_opens_no_tab(self):
        # Break: a missing install is looked up on PATH, or a Ghostty tab is opened anyway.
        browser = self.browser(root="")
        self.assertIn("terminal_browser_root", browser.open(self.alpha(browser)))
        self.assertEqual(self.host.runs, [])

    def test_a_record_of_a_browser_pane_beside_an_earlier_firstpane_is_never_taken_for_a_ghostty_tab(self):
        # Break: after the upgrade a companion.json written for a Herdr pane beside Firstpane
        # (by the beside placement, or by the first Firstpane, which wrote no placement) is
        # read as a Ghostty tab, so `x` signals the browser running in that pane, or the
        # upgrade fails to load.
        _, _, run = self.native()
        record = json.loads((self.profile / "companion.json").read_text())
        for legacy in (dict(record, placement="beside", pane_id="w1:p2"), {k: v for k, v in record.items() if k != "placement"}):
            (self.profile / "companion.json").write_text(json.dumps(legacy))
            again = self.browser()
            again.refresh()
            self.assertIsNone(again.owner())
            self.assertIn("no browser", again.close())
            self.assertTrue(self.alive(run["pid"]))
        self.assertEqual(self.host.closed, [])


if __name__ == "__main__":
    unittest.main()
