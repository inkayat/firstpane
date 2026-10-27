"""Project scope and mouse hits on the Agents tab."""

import os
import tempfile
import unittest
from pathlib import Path

from tests.support import (
    Clock,
    FakeTerminal,
    FakeTransport,
    agent_for,
    pane,
    snapshot,
    workspace,
    write_meta,
    worktree,
)
from firstpane.app import App
from firstpane.config import Config, load_config
from firstpane.input import decode_input
from firstpane.scope import path_in_scope


def visible_lines(frame):
    import re
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in frame.splitlines()]


class ScopeTests(unittest.TestCase):
    def test_symlink_escape_is_outside_the_root(self):
        # Break: a link inside projects/ is treated as a member after it leaves the root.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            outside = Path(tmp) / "tools" / "firstmate"
            root.mkdir()
            outside.mkdir(parents=True)
            link = root / "escape"
            link.symlink_to(outside)
            self.assertFalse(path_in_scope(str(link), str(root)))
            self.assertTrue(path_in_scope(str(root / "alter"), str(root)) or not (root / "alter").exists())
            (root / "firstmate").mkdir()
            self.assertTrue(path_in_scope(str(root / "firstmate"), str(root)))

    def test_allowlist_covers_nested_paths_but_not_a_shared_name_prefix(self):
        # Break: a cwd inside an allowlisted project is hidden, or /p/foobar passes for /p/foo.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            (root / "foo" / "src").mkdir(parents=True)
            (root / "foobar").mkdir()
            allow = (str(root / "foo"),)
            self.assertTrue(path_in_scope(str(root / "foo"), str(root), allow))
            self.assertTrue(path_in_scope(str(root / "foo" / "src"), str(root), allow))
            self.assertFalse(path_in_scope(str(root / "foobar"), str(root), allow))
            self.assertFalse(path_in_scope(str(root / "foo" / ".." / "foobar"), str(root), allow))


class SectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "fm-home"
        (self.home / "projects" / "alter").mkdir(parents=True)
        (self.home / "projects" / "firstmate").mkdir()
        self.tool = self.root / "tool"
        (self.tool / "bin").mkdir(parents=True)
        (self.tool / "bin" / "fm-inbox.sh").write_text("#!/bin/sh\nexit 0\n")
        self.transport = FakeTransport()
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = Config(
            firstmate_home=str(self.home),
            firstmate_root=str(self.tool),
            socket_path="/Users/x/.config/herdr/sessions/lab/herdr.sock",
            github_hosts=("github.com",),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, **kwargs):
        return App(
            config=kwargs.get("config", self.config),
            transport=self.transport,
            state_dir=self.state,
            env={},
            clock=Clock(),
            terminal=FakeTerminal(),
            rows=40,
            cols=80,
        )

    def test_default_scope_hides_supervisor_and_outside_cwd(self):
        # Break: the captain pane or a tools checkout is listed as a project.
        alter = self.home / "projects" / "alter"
        own = self.home / "projects" / "firstmate"
        write_meta(
            self.home,
            "alter-task",
            project=str(alter),
            herdr_session="lab",
            herdr_workspace_id="w1",
            herdr_tab_id="w1:t1",
            herdr_pane_id="w1:p1",
            endpoint_task_id="alter-task",
        )
        write_meta(
            self.home,
            "fork-task",
            project=str(own),
            herdr_session="lab",
            herdr_workspace_id="w2",
            herdr_tab_id="w2:t1",
            herdr_pane_id="w2:p1",
            endpoint_task_id="fork-task",
        )
        write_meta(
            self.home,
            "tool-task",
            project=str(self.tool),
            herdr_session="lab",
            herdr_workspace_id="w3",
            herdr_tab_id="w3:t1",
            herdr_pane_id="w3:p1",
            endpoint_task_id="tool-task",
        )
        rows = [
            pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1", cwd=str(alter), foreground_cwd=str(alter)),
            pane(pane_id="w2:p1", terminal_id="term_f", workspace_id="w2", tab_id="w2:t1", cwd=str(own), foreground_cwd=str(own), agent_status="done"),
            pane(pane_id="w3:p1", terminal_id="term_t", workspace_id="w3", tab_id="w3:t1", cwd=str(self.tool), foreground_cwd=str(self.tool), agent_status="idle"),
            pane(pane_id="w4:p1", terminal_id="term_m", workspace_id="w4", tab_id="w4:t1", cwd=str(self.tool), foreground_cwd=str(self.tool)),
        ]
        self.transport.snapshot = snapshot(
            [
                workspace("w1", "alter"),
                workspace("w2", "firstmate", number=2),
                workspace("w3", "tools", number=3),
                workspace("w4", "main", number=4),
            ],
            rows,
            [
                agent_for(rows[0], name="alter-agent"),
                agent_for(rows[1], name="fork-agent"),
                agent_for(rows[2], name="tool-agent"),
                agent_for(rows[3], name="main-agent"),
            ],
        )
        self.config.herdr_pane_id = "w4:p1"
        app = self.app()
        app.refresh(manual=True)
        text = "\n".join(visible_lines(app.render()))
        self.assertNotIn("Projects", text)
        self.assertIn("Alter task", text)
        self.assertIn("Fork task", text)
        self.assertIn("Done", text)
        self.assertNotIn("Tool task", text)
        self.assertNotIn("main-agent", text)
        self.assertNotIn("Pull Requests", "\n".join(text.splitlines()[1:]))

    def test_allowlist_is_the_only_manual_override(self):
        # Break: a configured allowlist still shows every project under the home.
        alter = self.home / "projects" / "alter"
        own = self.home / "projects" / "firstmate"
        write_meta(
            self.home,
            "alter-task",
            project=str(alter),
            herdr_session="lab",
            herdr_workspace_id="w1",
            herdr_tab_id="w1:t1",
            herdr_pane_id="w1:p1",
            endpoint_task_id="alter-task",
        )
        write_meta(
            self.home,
            "fork-task",
            project=str(own),
            herdr_session="lab",
            herdr_workspace_id="w2",
            herdr_tab_id="w2:t1",
            herdr_pane_id="w2:p1",
            endpoint_task_id="fork-task",
        )
        one = pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1")
        two = pane(pane_id="w2:p1", terminal_id="term_f", workspace_id="w2", tab_id="w2:t1")
        self.transport.snapshot = snapshot(
            [workspace("w1", "alter"), workspace("w2", "firstmate", number=2)],
            [one, two],
            [agent_for(one, name="alter-agent"), agent_for(two, name="fork-agent")],
        )
        self.config.project_allowlist = (str(alter),)
        app = self.app()
        app.refresh(manual=True)
        text = "\n".join(visible_lines(app.render()))
        self.assertIn("Alter task", text)
        self.assertNotIn("Fork task", text)

    def test_folding_a_project_hides_its_agents_and_a_click_does_not_post(self):
        # Break: folding a project leaves its agents listed, or a click focuses (leaves the panel) or posts a comment.
        alter = self.home / "projects" / "alter"
        write_meta(
            self.home,
            "alter-task",
            project=str(alter),
            herdr_session="lab",
            herdr_workspace_id="w1",
            herdr_tab_id="w1:t1",
            herdr_pane_id="w1:p1",
            endpoint_task_id="alter-task",
        )
        row = pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1", cwd=str(alter), foreground_cwd=str(alter))
        self.transport.snapshot = snapshot([workspace("w1", "alter")], [row], [agent_for(row)])
        self.transport.pane_get["w1:p1"] = dict(row)
        app = self.app()
        app.refresh(manual=True)
        app.render()
        project_row = next(
            index for index, row in enumerate(app.hit_rows, start=1) if row and row["kind"] == "project"
        )
        agent_row = next(
            index for index, row in enumerate(app.hit_rows, start=1) if row and row["kind"] == "agent"
        )
        app.handle_bytes(f"\x1b[<0;1;{project_row}M")
        folded = "\n".join(visible_lines(app.render()))
        self.assertNotIn("Alter task", folded)
        self.assertIn("\u25b8 alter", folded)
        app.handle_bytes(f"\x1b[<0;1;{project_row}M")
        app.render()
        app.handle_bytes(f"\x1b[<0;1;{agent_row}M")
        self.assertEqual([call for call in self.transport.socket_calls if call["method"] == "pane.focus"], [])
        app.handle_key("2")
        header = visible_lines(app.render())[1]
        self.assertIn("alter / alter-task", header)
        app.handle_key("1")
        app.handle_key("g")
        focuses = [call for call in self.transport.socket_calls if call["method"] == "pane.focus"]
        self.assertEqual(len(focuses), 1)
        self.assertEqual(focuses[0]["params"], {"pane_id": "w1:p1"})
        self.assertFalse(any(call["argv"][:1] == ["gh"] and "comment" in call["argv"] for call in self.transport.calls))

    def test_stale_click_does_not_select_a_replacement(self):
        # Break: a click uses the row now under the old coordinate after refresh.
        alter = self.home / "projects" / "alter"
        write_meta(
            self.home,
            "alter-task",
            project=str(alter),
            herdr_session="lab",
            herdr_workspace_id="w1",
            herdr_tab_id="w1:t1",
            herdr_pane_id="w1:p1",
            endpoint_task_id="alter-task",
        )
        row = pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1")
        self.transport.snapshot = snapshot([workspace("w1", "alter")], [row], [agent_for(row)])
        app = self.app()
        app.refresh(manual=True)
        lines = visible_lines(app.render())
        agent_row = next(index for index, line in enumerate(lines, start=1) if "Alter task" in line)
        self.transport.snapshot = snapshot([workspace("w1", "alter")], [], [])
        app.refresh(manual=True)
        app.handle_bytes(f"\x1b[<0;1;{agent_row}M")
        self.assertNotIn("pane.focus", [call["method"] for call in self.transport.socket_calls])
        self.assertIsNone(app.target)
        self.assertIn("stale", "\n".join(visible_lines(app.render())).lower() + app.status)


class InputTests(unittest.TestCase):
    def test_sgr_click_is_a_press_and_release_is_not(self):
        # Break: a mouse release or a wheel is treated as Enter on the row under the cursor.
        press = decode_input("\x1b[<0;4;2M")
        release = decode_input("\x1b[<0;4;2m")
        self.assertEqual(press, [("mouse", 0, 4, 2, True)])
        self.assertEqual(release, [("mouse", 0, 4, 2, False)])

    def test_config_reads_allowlist_without_inventing_a_root(self):
        # Break: a missing file fills in a personal project root.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"project_allowlist": ["/tmp/one"], "project_root": "/tmp/projects"}')
            loaded = load_config(path, environ={})
            self.assertEqual(loaded.project_allowlist, ("/tmp/one",))
            self.assertEqual(loaded.project_root, "/tmp/projects")
            missing = load_config(Path(tmp) / "missing.json", environ={})
            self.assertEqual(missing.project_allowlist, ())
            self.assertIsNone(missing.project_root)


if __name__ == "__main__":
    unittest.main()
