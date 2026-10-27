"""`python3 -m firstpane` on a real pty: cbreak input, UTF-8, paste, mouse, resize, send, restart, Settings, browser placement.

Herdr is a fake unix-socket server (the FirstMate primary's pi message box is FakePi behind
it) and Git is a fixture worktree. Ghostty is a fake `osascript` first on PATH (every test,
so nothing can reach the real Ghostty). Nothing reaches a real Herdr session, Ghostty window,
repository or FirstMate.
"""

import codecs
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import warnings
from pathlib import Path

from tests.gitfixture import GitFixture
from tests.support import FakePi, FakeTransport, agent_for, pane, remove_launch_runtimes, snapshot, workspace, write_meta
from tests.vtscreen import Screen

ROOT = Path(__file__).resolve().parents[1]
PLACED_TB = r"""#!{python}
import json, os, signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid()}}, open({out!r}, "w"))
while True:
    time.sleep(0.05)
"""
# Ghostty's AppleScript as Firstpane reaches it: `new tab in` the front window runs its command the
# way Ghostty on macOS does (bash -c "exec -l <command>", tests.support.ghostty_argv) in its own
# session and answers the window, tab and terminal ids; every call is logged.
FAKE_OSASCRIPT = r"""#!{python}
import json, os, subprocess, sys
log = {log!r}
with open(log, "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
script, args = sys.argv[2], sys.argv[3:]
if "new tab in" in script:
    subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", "exec -l " + args[0]], cwd=args[1], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("HW-1\nHT-2\nGT-1")
"""


class FakeHerdr:
    """Answers session.snapshot / pane.* on a unix socket, one request per connection."""

    def __init__(self, path, state):
        self.state = state
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(16)
        self.calls = []
        self.requests = []
        self.handlers = {}
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.split(b"\n", 1)[0])
                self.calls.append(request["method"])
                self.requests.append(request)
                if request["method"] == "session.snapshot":
                    reply = {"id": request["id"], "result": {"type": "session_snapshot", "snapshot": self.state["snapshot"]}}
                elif request["method"] == "pane.get":
                    found = self.state["panes"].get(request["params"]["pane_id"])
                    reply = {"id": request["id"], "result": {"type": "pane_info", "pane": found}} if found else {"id": request["id"], "error": {"code": "pane_not_found"}}
                elif request["method"] in self.handlers:
                    reply = {"id": request["id"], "result": self.handlers[request["method"]](request["params"])}
                else:
                    reply = {"id": request["id"], "error": {"code": "unsupported"}}
                conn.sendall((json.dumps(reply) + "\n").encode())

    def close(self):
        self.server.close()


class Pane:
    def __init__(self, env, rows, cols):
        self.screen = Screen(rows, cols)
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.raw = ""
        with warnings.catch_warnings():
            # The child only sets its window size and execs; nothing in it touches a lock.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, fd = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.chdir(str(ROOT))
            os.execve(sys.executable, [sys.executable, "-m", "firstpane"], env)
        self.pid, self.fd = pid, fd

    def pump(self, timeout=0.1):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return False
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return False
        text = self.decoder.decode(data)
        self.raw += text
        self.screen.feed(text)
        return True

    def wait_for(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            if predicate(self.screen):
                return
        raise AssertionError("timed out; screen:\n" + self.screen.text())

    def send(self, data, settle=0.3):
        os.write(self.fd, data)
        end = time.time() + settle
        while time.time() < end:
            self.pump(0.05)

    def resize(self, rows, cols, xpixel=0, ypixel=0):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, xpixel, ypixel))
        self.screen.resize(rows, cols)
        os.kill(self.pid, signal.SIGWINCH)

    def close(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + 10
        while time.time() < deadline:
            self.pump(0.05)
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if pid:
                break
        else:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        os.close(self.fd)

    def exit_code(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump(0.05)
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                return os.waitstatus_to_exitcode(status)
        raise AssertionError("pane did not exit")


class PtyTests(unittest.TestCase):
    def setUp(self):
        self.fx = GitFixture()
        self.tmp = Path(tempfile.mkdtemp(prefix="fpty.", dir="/tmp"))
        self.home = self.tmp / "fm-home"
        self.wt = self.fx.worktree("wt-pty", "fm/pty-task")
        self.fx.write("README.md", "one\nTWO\nthree\n", root=self.wt)
        row = pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1", agent_status="idle")
        write_meta(
            self.home,
            "pty-task",
            project=str(self.fx.primary),
            herdr_session="lab",
            herdr_workspace_id="w1",
            herdr_tab_id="w1:t1",
            herdr_pane_id="w1:p1",
            endpoint_task_id="pty-task",
            worktree=str(self.wt),
        )
        sock_dir = self.tmp / "sessions" / "lab"
        sock_dir.mkdir(parents=True)
        self.herdr = FakeHerdr(
            str(sock_dir / "herdr.sock"),
            {"snapshot": snapshot([workspace("w1", "alpha")], [row], [agent_for(row)]), "panes": {"w1:p1": dict(row)}},
        )
        holder = FakeTransport()
        self.main = FakePi(holder, self.home)
        self.herdr.state["snapshot"]["panes"].append(self.main.pane)
        self.herdr.state["panes"][self.main.pane["pane_id"]] = self.main.pane
        for method, handler in holder.socket_handlers.items():
            self.herdr.handlers[method] = lambda params, handler=handler: handler(params)["result"]
        config_dir = self.tmp / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"firstmate_home": str(self.home), "project_allowlist": [str(self.fx.base)]})
        )
        self.osascript_log = self.tmp / "osascript.jsonl"
        fake_bin = self.tmp / "fake-bin"
        fake_bin.mkdir()
        osascript = fake_bin / "osascript"
        osascript.write_text(FAKE_OSASCRIPT.format(python=sys.executable, log=str(self.osascript_log)))
        osascript.chmod(0o755)
        self.env = dict(self.fx.env)
        self.env.update(
            PATH=f"{fake_bin}:{self.fx.env['PATH']}",
            TERM="xterm-256color",
            LANG="en_US.UTF-8",
            HERDR_SOCKET_PATH=str(sock_dir / "herdr.sock"),
            HERDR_PLUGIN_CONFIG_DIR=str(config_dir),
            HERDR_PLUGIN_STATE_DIR=str(self.tmp / "state"),
            HERDR_PANE_ID="w9:p9",
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.panes = []

    def tearDown(self):
        for item in self.panes:
            item.close()
        self.stop_browser()
        self.herdr.close()
        remove_launch_runtimes(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.fx.cleanup()

    def stop_browser(self):
        out = self.tmp / "tb-run.json"
        try:
            os.kill(json.loads(out.read_text())["pid"], signal.SIGKILL)
        except (OSError, ValueError, KeyError):
            pass

    def configure_browser(self):
        tb = self.tmp / "tb"
        (tb / "bin").mkdir(parents=True)
        out = self.tmp / "tb-run.json"
        script = tb / "bin" / "terminal-browser"
        script.write_text(PLACED_TB.format(python=sys.executable, out=str(out)))
        script.chmod(0o755)
        config = self.tmp / "config" / "config.json"
        config.write_text(json.dumps(dict(json.loads(config.read_text()), terminal_browser_root=str(tb))))
        return out

    def wait_file(self, path, term, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            term.pump(0.05)
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except ValueError:
                    pass
        raise AssertionError(f"{path.name} never appeared; screen:\n" + term.screen.text())

    def open(self, rows=30, cols=100):
        item = Pane(self.env, rows, cols)
        self.panes.append(item)
        return item

    def row_of(self, screen, needle):
        return next(index + 1 for index, line in enumerate(screen.lines()) if needle in line)

    def test_double_click_type_paste_resize_send_and_restart(self):
        # Break: the real entrypoint does not decode mouse clicks, cbreak input, Turkish text
        # or bracketed paste; there is still a bottom message footer; a resize hides the
        # inline editor; the preview and what lands in FirstMate's message box differ, or it
        # is submitted there; Ctrl-C leaves the tty in paste/mouse mode; or a restart loses
        # an unsent inline draft. Also: no Git write happens without an explicit confirm.
        head = self.fx.head(self.wt)
        index_before = (Path(self.fx.git(self.wt, "rev-parse", "--git-dir")) / "index").stat().st_mtime_ns
        term = self.open()
        term.wait_for(lambda s: "Pty task" in s.text())
        self.assertTrue(term.screen.modes.get("2004"))
        self.assertTrue(term.screen.modes.get("1002"))
        term.send(b"\r")
        before = len(term.raw)
        term.send(b"2")
        term.wait_for(lambda s: "/ pty-task" in s.lines()[1] and "1 file" in s.lines()[2])
        term.wait_for(lambda s: "README.md \u25be" in s.text() and "+ TWO" in s.text())
        self.assertIn("Write to FirstMate", term.screen.text())
        self.assertIn("48;2;16;22;35", term.raw[before:])
        row = self.row_of(term.screen, "+ TWO")
        term.send(f"\x1b[<0;20;{row}M\x1b[<0;20;{row}m\x1b[<0;20;{row}M\x1b[<0;20;{row}m".encode())
        term.wait_for(lambda s: "1 selection added" in s.text())
        term.send("Merhaba dünya ğüşiöç ".encode())
        term.send(b"\x1b[200~ek satir y\x1b[201~")
        term.wait_for(lambda s: "Merhaba dünya ğüşiöç ek satir y" in s.text())
        term.resize(41, 68)
        term.wait_for(lambda s: "Merhaba dünya ğüşiöç ek satir y" in s.text() and "+ TWO" in s.text())
        self.assertTrue(all(len(line) <= 68 for line in term.screen.lines()))
        term.send(b"\r")
        term.wait_for(lambda s: "Put in FirstMate's message box" in s.text())
        preview = term.screen.text()
        term.send(b"y")
        term.wait_for(lambda s: "press Enter there to send" in s.text())
        self.assertEqual(len(self.main.received), 1)
        self.assertEqual(self.main.submitted, [])
        self.assertRegex(self.main.text, r"^\[paste #1 \+\d+ lines\]$")
        body = self.main.received[0].removeprefix("\x1b[200~").removesuffix("\x1b[201~")
        self.assertIn("agent: pty-task", body)
        self.assertIn(f"workspace: {self.wt}  (fm)", body)
        self.assertIn("Merhaba dünya ğüşiöç ek satir y", body)
        self.assertIn("[1] diff README.md  unstaged  new lines 2-2", body)
        self.assertTrue(body.endswith("```diff\n+TWO\n```"))
        for line in body.splitlines():
            self.assertIn(line[:60], preview)
        row = self.row_of(term.screen, "- two")
        term.send(f"\x1b[<0;20;{row}M\x1b[<0;20;{row}m\x1b[<0;20;{row}M\x1b[<0;20;{row}m".encode())
        term.send("taslak ş".encode())
        term.wait_for(lambda s: "taslak ş" in s.text())
        term.send(b"\x03")
        self.assertEqual(term.exit_code(), 0)
        self.assertTrue(term.raw.endswith("\x1b[?1002l\x1b[?1006l\x1b[?2004l\x1b[?25h"))
        again = self.open(rows=20, cols=50)
        again.wait_for(lambda s: "taslak ş" in s.text() and "- two" in s.text())
        self.assertEqual(self.row_of(again.screen, "taslak ş"), self.row_of(again.screen, "- two") + 1)
        self.assertEqual(self.fx.head(self.wt), head)
        index_after = (Path(self.fx.git(self.wt, "rev-parse", "--git-dir")) / "index").stat().st_mtime_ns
        self.assertEqual(index_after, index_before)
        self.assertNotIn("pane.focus", self.herdr.calls)

    def osascript_calls(self):
        if not self.osascript_log.exists():
            return []
        return [json.loads(line) for line in self.osascript_log.read_text().splitlines()]

    def test_settings_keys_and_clicks_choose_inside_firstpane_which_the_browser_tab_and_a_restart_honour(self):
        # Break: on the real entrypoint `6`, j/Enter or a click do not reach the Settings tab,
        # the choice is not kept across a restart, the Browser tab still opens a Ghostty tab or a
        # Herdr pane with Inside Firstpane chosen, the browser is not Firstpane's own child
        # (it would outlive Firstpane), or it misses the embed host it draws through.
        out = self.configure_browser()
        term = self.open()
        term.wait_for(lambda s: "Pty task" in s.text())
        term.send(b"\r")
        term.send(b"6")
        term.wait_for(lambda s: "Browser placement" in s.text() and "(\u2022) Ghostty tab" in s.text())
        self.assertIn("Settings", term.screen.lines()[0])
        term.send(b"j")
        term.send(b"\r")
        term.wait_for(lambda s: "(\u2022) Inside Firstpane" in s.text() and "saved" in s.text())
        row = self.row_of(term.screen, "Ghostty tab")
        column = term.screen.lines()[row - 1].index("Ghostty") + 1
        term.send(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())
        term.wait_for(lambda s: "(\u2022) Ghostty tab" in s.text())
        row = self.row_of(term.screen, "Inside Firstpane")
        column = term.screen.lines()[row - 1].index("Inside") + 1
        term.send(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())
        term.wait_for(lambda s: "(\u2022) Inside Firstpane" in s.text())
        term.send(b"3")
        run = self.wait_file(out, term)
        parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(run["pid"])], capture_output=True, text=True).stdout.strip()
        self.assertEqual(parent, str(term.pid))
        self.assertTrue(run["env"]["PIXEL_EMBED"])
        self.assertEqual(self.osascript_calls(), [])
        self.assertNotIn("pane.split", self.herdr.calls)
        self.assertFalse([name for name in run["env"] if name.startswith("HERDR_")])
        self.assertNotEqual(run["env"]["HOME"], self.env.get("HOME"))
        term.send(b"\x03")
        self.assertEqual(term.exit_code(), 0)
        deadline = time.time() + 10
        while time.time() < deadline and subprocess.run(["ps", "-p", str(run["pid"])], capture_output=True).returncode == 0:
            time.sleep(0.05)
        self.assertNotEqual(subprocess.run(["ps", "-p", str(run["pid"])], capture_output=True).returncode, 0)
        out.unlink()
        again = self.open()
        again.wait_for(lambda s: "pty-task" in s.text())
        again.send(b"6")
        again.wait_for(lambda s: "(\u2022) Inside Firstpane" in s.text())
        again.send(b"3")
        self.assertTrue(self.wait_file(out, again)["env"]["PIXEL_EMBED"])
        self.assertEqual(self.osascript_calls(), [])

    def test_hiding_a_tab_in_settings_removes_it_from_the_row_and_survives_a_restart(self):
        # Break: on the real entrypoint a click on a tab row does not hide it, the hidden tab's
        # number key still opens it, or a restarted Firstpane shows it again.
        term = self.open()
        term.wait_for(lambda s: "Pty task" in s.text())
        term.send(b"6")
        term.wait_for(lambda s: "[x] Board" in s.text())
        row = self.row_of(term.screen, "[x] Board")
        column = term.screen.lines()[row - 1].index("[x]") + 1
        term.send(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())
        term.wait_for(lambda s: "[ ] Board" in s.text() and "Board" not in s.lines()[0])
        term.send(b"5")
        term.wait_for(lambda s: "Board is hidden" in s.text())
        self.assertIn("[ ] Board", term.screen.text())
        term.send(b"\x03")
        self.assertEqual(term.exit_code(), 0)
        again = self.open()
        again.wait_for(lambda s: "[ ] Board" in s.text())
        self.assertNotIn("Board", again.screen.lines()[0])
        self.assertIn("Settings", again.screen.lines()[0])

    def click_row(self, term, needle):
        row = self.row_of(term.screen, needle)
        column = term.screen.lines()[row - 1].index(needle) + 1
        term.send(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())

    def test_hiding_settings_and_every_other_tab_leaves_key_six_as_the_way_back_across_a_restart(self):
        # Break: on the real entrypoint a hidden Settings is unreachable (no key 6, or a restart
        # lands on a tab with no entry), hidden tabs still open from their number keys, or the
        # tabs cannot be shown again from there.
        term = self.open()
        term.wait_for(lambda s: "Pty task" in s.text())
        term.send(b"6")
        term.wait_for(lambda s: "[x] Settings" in s.text())
        for label in ("Agents", "Changes", "Browser", "Pull Requests", "Board", "Settings"):
            self.click_row(term, f"[x] {label}")
            term.wait_for(lambda s, label=label: f"[ ] {label}" in s.text())
        term.wait_for(lambda s: s.lines()[0].strip() == "")
        term.send(b"3")
        term.wait_for(lambda s: "Browser is hidden" in s.text())
        self.assertIn("Browser placement", term.screen.text())
        term.send(b"\x03")
        self.assertEqual(term.exit_code(), 0)
        again = self.open()
        again.wait_for(lambda s: "[ ] Settings" in s.text())
        self.assertEqual(again.screen.lines()[0].strip(), "")
        self.click_row(again, "[ ] Agents")
        again.wait_for(lambda s: "Agents" in s.lines()[0] and "Settings" not in s.lines()[0])
        again.send(b"1")
        again.wait_for(lambda s: "Pty task" in s.text())
        again.send(b"6")
        again.wait_for(lambda s: "Browser placement" in s.text())
        self.assertNotIn("Settings", again.screen.lines()[0])

    @unittest.skipUnless(sys.platform == "darwin", "the Ghostty tab placement is macOS-only; see the next test")
    def test_the_default_placement_opens_one_ghostty_tab_on_the_real_entrypoint(self):
        # Break: the real entrypoint draws the slow in-tab page by default, asks Ghostty for a
        # tab on every visit to the tab, splits a Herdr pane, or `f` focuses no tab.
        out = self.configure_browser()
        term = self.open()
        term.wait_for(lambda s: "Pty task" in s.text())
        term.send(b"\r")
        term.send(b"3")
        run = self.wait_file(out, term)
        term.wait_for(lambda s: "in a Ghostty tab" in s.text())
        self.assertNotIn("\U0010EEEE", term.screen.text())
        self.assertEqual(run["argv"], ["open"])
        self.assertFalse([name for name in run["env"] if name.startswith(("HERDR_", "GHOSTTY_"))])
        term.send(b"1")
        term.send(b"3")
        term.send(b"f")
        tabs = [call for call in self.osascript_calls() if "new tab in" in call[1]]
        focus = [call for call in self.osascript_calls() if "new tab in" not in call[1]]
        self.assertEqual(len(tabs), 1)
        self.assertEqual(focus[-1][2:], ["GT-1"])
        self.assertNotIn("pane.split", self.herdr.calls)
        term.send(b"\x03")
        self.assertEqual(term.exit_code(), 0)

    @unittest.skipIf(sys.platform == "darwin", "this host supports the Ghostty tab placement; see the previous test")
    def test_off_macos_the_default_placement_says_to_choose_inside_firstpane_and_opens_nothing(self):
        # Break: on Linux the real entrypoint calls osascript anyway, starts the in-tab browser
        # without being asked, or leaves the tab silent about why no browser opened.
        out = self.configure_browser()
        term = self.open()
        term.wait_for(lambda s: "Pty task" in s.text())
        term.send(b"\r")
        term.send(b"3")
        term.wait_for(lambda s: "needs macOS" in s.text() and "Inside Firstpane" in s.text())
        self.assertEqual(self.osascript_calls(), [])
        self.assertNotIn("pane.split", self.herdr.calls)
        self.assertFalse(out.exists())
        term.send(b"\x03")
        self.assertEqual(term.exit_code(), 0)

    def test_agents_search_filter_and_message_field_on_the_real_entrypoint(self):
        # Break: on the real entrypoint the Agents frame is wider than the pane, the search or
        # status filter ignore input bytes, Turkish text or a bracketed paste does not reach
        # the message field, or a message leaves without the preview, about another agent,
        # or is submitted in FirstMate's box.
        term = self.open(rows=30, cols=100)
        term.wait_for(lambda s: "Pty task" in s.text())
        self.assertTrue(all(len(line) <= 100 for line in term.screen.lines()))
        self.assertIn("1 project \u00b7 1 agent", term.screen.text())
        term.send(b"/")
        term.send(b"zzz")
        term.wait_for(lambda s: "No matching agents" in s.text())
        term.send(b"\x1b")
        term.wait_for(lambda s: "Pty task" in s.text())
        term.send(b"f")
        term.wait_for(lambda s: "Working \u25be" in s.text() and "No matching agents" in s.text())
        term.send(b"\x1b")
        term.wait_for(lambda s: "All \u25be" in s.text() and "Pty task" in s.text())
        term.send(b"\t")
        term.send("Merhaba dünya ğüşiöç ".encode())
        term.send(b"\x1b[200~ek satir y\x1b[201~")
        term.wait_for(lambda s: "Merhaba dünya ğüşiöç ek satir y" in s.text())
        self.assertEqual(self.main.received, [])
        term.send(b"\r")
        term.wait_for(lambda s: "Put in FirstMate's message box" in s.text() and "agent: pty-task" in s.text())
        self.assertEqual(self.main.received, [])
        term.send(b"y")
        term.wait_for(lambda s: "press Enter there to send" in s.text())
        self.assertEqual(len(self.main.received), 1)
        self.assertEqual(self.main.submitted, [])
        self.assertIn("agent: pty-task", self.main.received[0])
        self.assertIn("Merhaba dünya ğüşiöç ek satir y", self.main.received[0])
        self.assertNotIn("pane.focus", self.herdr.calls)

    def test_the_real_entrypoint_shows_the_harness_image_once_accepted_and_deletes_it_at_exit(self):
        # Break: the real loop never transmits the icon images, draws icon cells the terminal did
        # not accept, keeps the harness name beside a shown image, or leaves the images in the
        # terminal after Firstpane exits.
        term = self.open(rows=30, cols=100)
        term.wait_for(lambda s: "Pty task" in s.text())
        term.wait_for(lambda s: term.raw.count("\x1b_Ga=T,U=1,") >= 5)
        heads = [dict(part.split("=", 1) for part in head.split(",") if "=" in part) for head in re.findall(r"\x1b_G([^;\x1b]*)", term.raw)]
        ids = sorted({int(keys["i"]) for keys in heads if keys.get("a") == "T"})
        self.assertEqual(len(ids), 5)
        self.assertNotIn("\U0010EEEE", term.screen.text())
        self.assertIn("Claude \u00b7", term.screen.text())
        term.send("".join(f"\x1b_Gi={image_id};OK\x1b\\" for image_id in ids).encode())
        term.wait_for(lambda s: "\U0010EEEE" in s.text())
        line = next(line for line in term.screen.lines() if "\U0010EEEE" in line)
        self.assertEqual(line.count("\U0010EEEE"), 2)
        self.assertNotIn("Claude", term.screen.text())
        term.send(b"\x03")
        self.assertEqual(term.exit_code(), 0)
        for image_id in ids:
            self.assertIn(f"\x1b_Ga=d,d=I,i={image_id}", term.raw)
        self.assertTrue(term.raw.endswith("\x1b[?1002l\x1b[?1006l\x1b[?2004l\x1b[?25h"))


if __name__ == "__main__":
    unittest.main()
