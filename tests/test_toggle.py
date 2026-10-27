"""Firstpane toggle action: one key opens Firstpane beside the focused pane, the next closes it.

Each test names the production break it exists to catch. Herdr is a fake `herdr` CLI over an
in-memory layout whose JSON mirrors Herdr 0.9.5 responses (`pane list`, `plugin pane open`,
`plugin pane close` refusing panes that are not plugin panes). The manifest action test runs
the real `python3 -m firstpane.toggle` entrypoint against a fake `herdr` executable.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path

from firstpane.toggle import toggle
from tests.support import CmdResult

ROOT = Path(__file__).resolve().parents[1]


def _ok(result):
    return CmdResult(0, json.dumps({"id": "cli", "result": result}) + "\n", "")


def _err(code):
    return CmdResult(1, "", json.dumps({"id": "cli", "error": {"code": code, "message": code}}) + "\n")


class FakeHerdr:
    """The `herdr` CLI over one session: panes in tabs, some of them plugin panes."""

    def __init__(self):
        self.panes = {}
        self.calls = []
        self.next = 1
        self.fail = set()
        self.on_list = None
        self.ui_focused = "w1:p1"  # Herdr's default open target when none is given

    def add(self, tab, label=None, plugin=None):
        pane_id = f"w1:p{self.next}"
        self.next += 1
        self.panes[pane_id] = {
            "pane_id": pane_id, "terminal_id": f"term_{pane_id[3:]}_{self.next}", "tab_id": tab,
            "workspace_id": "w1", "label": label, "plugin": plugin,
            "focused": False, "agent_status": "unknown", "revision": 0,
        }
        return pane_id

    def info(self, pane_id):
        return {key: value for key, value in self.panes[pane_id].items() if key != "plugin"}

    def firstpanes(self, tab):
        return [pane_id for pane_id, pane in self.panes.items()
                if pane["tab_id"] == tab and pane["plugin"] == ("firstpane", "tree")]

    def mutating(self):
        return [argv for argv in self.calls if argv[:3] in (["plugin", "pane", "open"], ["plugin", "pane", "close"])]

    def run(self, argv, stdin=None, env=None, timeout=None, cwd=None, detach=False):
        assert argv[0] == "/fake/herdr", argv
        args = list(argv[1:])
        self.calls.append(args)
        if args[:2] == ["pane", "list"]:
            if self.on_list:
                self.on_list()
            if "list" in self.fail:
                return CmdResult(1, "", "connect: no such file\n")
            return _ok({"type": "pane_list", "panes": [self.info(pane_id) for pane_id in self.panes]})
        if args[:3] == ["plugin", "pane", "open"]:
            rest = [arg for arg in args[3:] if arg not in ("--no-focus", "--focus")]
            opts = dict(zip(rest[::2], rest[1::2]))
            target = opts.get("--target-pane", self.ui_focused)
            if target not in self.panes:
                return _err("pane_not_found")
            new = self.add(self.panes[target]["tab_id"], "Firstpane", (opts["--plugin"], opts["--entrypoint"]))
            return _ok({"type": "plugin_pane_opened", "plugin_pane": {
                "plugin_id": opts["--plugin"], "entrypoint": opts["--entrypoint"], "pane": self.info(new)}})
        if args[:3] == ["plugin", "pane", "close"]:
            pane_id = args[3]
            if pane_id not in self.panes or self.panes[pane_id]["plugin"] is None:
                return _err("plugin_pane_not_found")
            del self.panes[pane_id]
            return _ok({"type": "plugin_pane_closed", "pane_id": pane_id})
        if args[:2] == ["notification", "show"]:
            return _ok({"type": "ok"})
        raise AssertionError(f"unexpected herdr call {args}")


class ToggleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.herdr = FakeHerdr()
        self.shell = self.herdr.add("w1:t1")

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, focused=None, tab="w1:t1", socket="/run/herdr-a.sock"):
        return {"HERDR_SOCKET_PATH": socket, "HERDR_BIN_PATH": "/fake/herdr", "HERDR_PLUGIN_ID": "firstpane",
                "HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": tab, "HERDR_PANE_ID": focused or self.shell}

    def press(self, **kwargs):
        return toggle(self.env(**kwargs), self.herdr, self.state)

    def test_first_press_opens_one_firstpane_split_right_of_the_focused_pane(self):
        # Break: opening with the wrong plugin/entrypoint/target, taking focus, or opening nothing.
        code, _ = self.press()
        self.assertEqual(code, 0)
        self.assertEqual(self.herdr.mutating(), [[
            "plugin", "pane", "open", "--plugin", "firstpane", "--entrypoint", "tree",
            "--placement", "split", "--direction", "right", "--target-pane", "w1:p1", "--no-focus"]])
        self.assertEqual(len(self.herdr.firstpanes("w1:t1")), 1)

    def test_presses_alternate_open_close_open_and_never_leave_two_firstpanes(self):
        # Break (the reported bug): a second press opens another Firstpane instead of closing it.
        self.press()
        first = self.herdr.firstpanes("w1:t1")
        self.press()
        self.assertEqual(self.herdr.firstpanes("w1:t1"), [])
        self.assertEqual(self.herdr.mutating()[1], ["plugin", "pane", "close", first[0]])
        self.press()
        again = self.herdr.firstpanes("w1:t1")
        self.assertEqual(len(again), 1)
        self.assertNotEqual(again, first)
        for _ in range(5):
            self.press()
            self.assertLessEqual(len(self.herdr.firstpanes("w1:t1")), 1)
        self.assertEqual(self.herdr.firstpanes("w1:t1"), [])

    def test_close_press_from_inside_firstpane_closes_it(self):
        # Break: a press while Firstpane itself is focused opens a second Firstpane beside it.
        self.press()
        opened = self.herdr.firstpanes("w1:t1")[0]
        code, _ = self.press(focused=opened)
        self.assertEqual(code, 0)
        self.assertEqual(self.herdr.firstpanes("w1:t1"), [])

    def test_closing_leaves_every_other_pane_in_the_tab_alive(self):
        # Break: closing the focused pane, another plugin's pane, or Firstpane's browser pane.
        browser = self.herdr.add("w1:t1", "browser")
        other_plugin = self.herdr.add("w1:t1", "Board", ("example.board", "board"))
        self.press()
        self.press()
        self.assertEqual(sorted(self.herdr.panes), sorted([self.shell, browser, other_plugin]))

    def test_firstpane_in_another_tab_is_neither_closed_nor_counted(self):
        # Break: sweeping the session, or treating another tab's Firstpane as this tab's.
        other_shell = self.herdr.add("w1:t2")
        self.press(focused=other_shell, tab="w1:t2")
        elsewhere = self.herdr.firstpanes("w1:t2")
        code, _ = self.press()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.herdr.firstpanes("w1:t1")), 1)
        self.assertEqual(self.herdr.firstpanes("w1:t2"), elsewhere)

    def test_unmanaged_firstpane_titled_pane_refuses_without_opening_or_closing(self):
        # Break: identifying by title (closing a pane this toggle never opened) or opening a duplicate.
        self.herdr.add("w1:t1", "Firstpane", ("firstpane", "tree"))
        code, message = self.press()
        self.assertEqual(code, 1)
        self.assertEqual(self.herdr.mutating(), [])
        self.assertIn("w1:p2", message)
        self.assertIn(["notification", "show"], [argv[:2] for argv in self.herdr.calls])

    def test_recorded_pane_id_on_another_terminal_is_not_closed(self):
        # Break: trusting a recorded pane id alone after Herdr replaced the terminal behind it.
        self.press()
        opened = self.herdr.firstpanes("w1:t1")[0]
        self.herdr.panes[opened].update(terminal_id="term_replaced", label=None, plugin=None)
        code, _ = self.press()
        self.assertEqual(code, 0)
        self.assertIn(opened, self.herdr.panes)
        self.assertEqual(self.herdr.mutating()[-1][:3], ["plugin", "pane", "open"])

    def test_press_in_another_session_leaves_this_sessions_record_alone(self):
        # Break: pruning the shared state file against one session's pane list drops another
        # session's record, so that session's next press refuses its own Firstpane as unknown.
        self.press()
        opened = self.herdr.firstpanes("w1:t1")
        other = FakeHerdr()
        other_shell = other.add("w1:t1")
        code, _ = toggle(self.env(focused=other_shell, socket="/run/herdr-b.sock"), other, self.state)
        self.assertEqual(code, 0)
        self.assertEqual(len(other.firstpanes("w1:t1")), 1)
        code, _ = self.press()
        self.assertEqual(code, 0)
        self.assertEqual(self.herdr.firstpanes("w1:t1"), [])
        self.assertEqual(len(other.firstpanes("w1:t1")), 1)
        self.assertEqual(self.herdr.mutating()[-1], ["plugin", "pane", "close", opened[0]])
        code, _ = toggle(self.env(focused=other_shell, socket="/run/herdr-b.sock"), other, self.state)
        self.assertEqual(code, 0)
        self.assertEqual(other.firstpanes("w1:t1"), [])

    def test_two_recognised_firstpanes_in_the_tab_refuse(self):
        # Break: picking one of two candidates at random.
        self.press()
        other_shell = self.herdr.add("w1:t2")
        self.press(focused=other_shell, tab="w1:t2")
        moved = self.herdr.firstpanes("w1:t2")[0]
        self.herdr.panes[moved]["tab_id"] = "w1:t1"
        before = dict(self.herdr.panes)
        code, message = self.press()
        self.assertEqual(code, 1)
        self.assertEqual(self.herdr.panes, before)
        self.assertEqual(len(self.herdr.mutating()), 2)
        self.assertIn("2", message)

    def test_unreadable_layout_opens_and_closes_nothing(self):
        # Break: treating "Herdr did not answer" as "no Firstpane here" and opening a duplicate.
        self.press()
        self.herdr.fail.add("list")
        code, _ = self.press()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.herdr.mutating()), 1)
        self.assertEqual(len(self.herdr.firstpanes("w1:t1")), 1)

    def test_missing_focus_context_refuses(self):
        # Break: opening against an unknown pane/tab when Herdr gave no focused context.
        env = self.env()
        del env["HERDR_TAB_ID"]
        code, _ = toggle(env, self.herdr, self.state)
        self.assertEqual(code, 1)
        self.assertEqual(self.herdr.mutating(), [])

    def test_concurrent_presses_serialise_and_never_open_two(self):
        # Break: two rapid presses both read "no Firstpane" before either opens, leaving two.
        entered = threading.Event()
        release = threading.Event()
        listing = []

        def on_list():
            listing.append(threading.current_thread().name)
            if len(listing) == 1:
                entered.set()
                release.wait(5)

        self.herdr.on_list = on_list
        results = {}
        first = threading.Thread(target=lambda: results.update(a=self.press()), name="a")
        second = threading.Thread(target=lambda: results.update(b=self.press()), name="b")
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        second.join(0.3)
        self.assertEqual(listing, ["a"])
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual((results["a"][0], results["b"][0]), (0, 0))
        self.assertEqual(self.herdr.firstpanes("w1:t1"), [])
        self.assertEqual([argv[2] for argv in self.herdr.mutating()], ["open", "close"])


FAKE_HERDR_BIN = """#!{python}
import json, os, sys
log = os.environ["FAKE_HERDR_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["pane", "list"]:
    print(json.dumps({{"id": "cli", "result": {{"type": "pane_list", "panes": [
        {{"pane_id": "w1:p1", "terminal_id": "term_a", "tab_id": "w1:t1", "workspace_id": "w1",
         "focused": True, "agent_status": "unknown", "revision": 0}}]}}}}))
elif sys.argv[1:4] == ["plugin", "pane", "open"]:
    print(json.dumps({{"id": "cli", "result": {{"type": "plugin_pane_opened", "plugin_pane": {{
        "plugin_id": "firstpane", "entrypoint": "tree", "pane": {{"pane_id": "w1:p2", "terminal_id": "term_b",
        "tab_id": "w1:t1", "workspace_id": "w1", "label": "Firstpane", "focused": False,
        "agent_status": "unknown", "revision": 0}}}}}}}}))
else:
    sys.exit(3)
"""


class ManifestActionTest(unittest.TestCase):
    def test_manifest_toggle_action_runs_the_toggle_from_the_plugin_root(self):
        # Break: the manifest action points at a module or entrypoint that does not exist.
        manifest = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())
        actions = {action["id"]: action for action in manifest.get("actions", [])}
        self.assertIn("toggle", actions)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "herdr"
            fake.write_text(FAKE_HERDR_BIN.format(python=sys.executable))
            fake.chmod(0o755)
            log = Path(tmp) / "calls.log"
            env = {"PATH": os.environ.get("PATH", ""), "HERDR_BIN_PATH": str(fake), "FAKE_HERDR_LOG": str(log),
                   "HERDR_PLUGIN_ID": "firstpane", "HERDR_PLUGIN_ACTION_ID": "toggle",
                   "HERDR_PLUGIN_STATE_DIR": str(Path(tmp) / "state"), "HERDR_WORKSPACE_ID": "w1",
                   "HERDR_TAB_ID": "w1:t1", "HERDR_PANE_ID": "w1:p1"}
            command = [sys.executable if part == "python3" else part for part in actions["toggle"]["command"]]
            done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([call[:3] for call in calls], [["pane", "list"], ["plugin", "pane", "open"]])


if __name__ == "__main__":
    unittest.main()
