"""Agents: a FirstMate direct report whose project is a task-private source clone.

FirstMate can give a task its own clone of a project under the FirstMate home's `data/` (with the
agent's worktree checked out from that clone) instead of `projects/<name>`. Such a report is listed
when its binding is live and unique and its worktree is a checkout of that clone; everything else
under `data/`, another home's clones, stale or doubly claimed bindings, and explicit
`project_root` / `project_allowlist` scopes stay as they were. Herdr is faked at the socket
boundary; Git runs for real.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from firstpane.agents_view import humanize
from firstpane.app import App
from firstpane.config import Config
from firstpane.transport import LiveTransport
from tests.gitfixture import git_env
from tests.support import Clock, FakeTerminal, FakeTransport, agent_for, pane, snapshot, strip_sgr, workspace, write_meta


class GitTransport(FakeTransport):
    """Fake Herdr socket; real git, nothing else."""

    def __init__(self):
        super().__init__()
        self.live = LiveTransport(None)

    def run(self, argv, stdin=None, env=None, timeout=None, cwd=None, detach=False):
        if argv and argv[0] == "git":
            self.calls.append({"argv": list(argv), "cwd": cwd})
            return self.live.run(argv, stdin=stdin, env=env, timeout=timeout, cwd=cwd, detach=detach)
        return super().run(argv, stdin=stdin, env=env, timeout=timeout, cwd=cwd, detach=detach)


class PrivateSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = git_env(self.root / "git-home")
        (self.root / "git-home").mkdir()
        (self.root / "git-home" / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
        self.home = self.root / "fm-home"
        self.alpha = self.repo(self.home / "projects" / "alpha")
        self.transport = GitTransport()
        self.rows = []
        self.agents = []
        self.workspaces = []
        self.config = Config(firstmate_home=str(self.home), socket_path="/Users/x/.config/herdr/sessions/lab/herdr.sock",
                             herdr_pane_id="w0:p1")

    def git(self, cwd, *args):
        subprocess.run(["git", *args], cwd=str(cwd), env=self.env, check=True, capture_output=True, stdin=subprocess.DEVNULL)

    def repo(self, path):
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        (path / "README.md").write_text("one\n")
        self.git(path, "add", "README.md")
        self.git(path, "commit", "-q", "-m", "initial")
        return path

    def worktree(self, source, path, branch):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git(source, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def private(self, home, pool, branch, name="repo"):
        """A task-private source clone `<home>/data/<pool>/source/<name>` and a worktree of it."""
        source = self.repo(home / "data" / pool / "source" / name)
        return source, self.worktree(source, home / "data" / pool / "pool" / "wt", branch)

    def report(self, task, project, worktree=None, number=1, live=True, pane_id=None):
        workspace_id = f"w{number}"
        pane_id = pane_id or f"{workspace_id}:p1"
        fields = dict(project=str(project), herdr_session="lab", herdr_workspace_id=workspace_id,
                      herdr_tab_id=f"{workspace_id}:t1", herdr_pane_id=pane_id, endpoint_task_id=task)
        if worktree:
            fields["worktree"] = str(worktree)
        write_meta(self.home, task, **fields)
        if live and not any(row["pane_id"] == pane_id for row in self.rows):
            cwd = str(worktree or project)
            row = pane(pane_id=pane_id, terminal_id=f"term_{task}", workspace_id=workspace_id,
                       tab_id=f"{workspace_id}:t1", cwd=cwd, foreground_cwd=cwd)
            self.rows.append(row)
            self.agents.append(agent_for(row, name=f"{task}-agent"))
            self.workspaces.append(workspace(workspace_id, task, number=number))

    def listed(self, config=None):
        self.transport.snapshot = snapshot(self.workspaces, self.rows, self.agents)
        app = App(config=config or self.config, transport=self.transport, state_dir=self.root / "state", env={},
                  clock=Clock(), terminal=FakeTerminal(), rows=60, cols=120)
        app.refresh(manual=True)
        return app, strip_sgr(app.render())

    def assertHidden(self, text, task):
        """Neither as a FirstMate report nor as an unbound agent row (its Herdr agent name)."""
        self.assertNotIn(humanize(task), text)
        self.assertNotIn(f"{task}-agent", text)

    def test_a_live_report_working_in_a_private_clone_of_this_home_is_listed_beside_canonical_ones(self):
        # Break (the user's case): only the report whose project is under projects/ is shown;
        # the two whose FirstMate-approved source is a private clone under data/ are hidden.
        self.report("alpha-task", self.alpha, number=1)
        source, wt = self.private(self.home, "beta-pool", "fm/beta-task")
        self.report("beta-task", source, wt, number=2)
        app, text = self.listed()
        self.assertIn(humanize("alpha-task"), text)
        self.assertIn(humanize("beta-task"), text)
        beta = next(row for project in app.projects for row in project["agents"] if row.get("task_id") == "beta-task")
        self.assertEqual(Path(beta["project_key"]), source)

    def test_a_data_path_whose_worktree_is_not_a_checkout_of_it_stays_hidden(self):
        # Break: anything under data/ is trusted, so a report naming a clone it does not work in
        # (or with no worktree at all) is listed.
        source, _ = self.private(self.home, "gamma-pool", "fm/gamma")
        elsewhere = self.worktree(self.alpha, self.root / "elsewhere" / "wt", "fm/gamma-task")
        self.report("gamma-task", source, elsewhere, number=3)
        self.report("delta-task", source, None, number=4)
        _, text = self.listed()
        self.assertHidden(text, "gamma-task")
        self.assertHidden(text, "delta-task")

    def test_another_homes_private_clone_stays_hidden(self):
        # Break: a clone under some other FirstMate home's data/ is admitted as this home's.
        source, wt = self.private(self.root / "other-home", "eps-pool", "fm/eps-task")
        self.report("eps-task", source, wt, number=5)
        _, text = self.listed()
        self.assertHidden(text, "eps-task")

    def test_a_stale_or_doubly_claimed_private_report_stays_hidden(self):
        # Break: a private-clone report whose pane is gone, or two reports claiming one pane,
        # are listed because their source checks out.
        source, wt = self.private(self.home, "zeta-pool", "fm/zeta-task")
        self.report("zeta-task", source, wt, number=6, live=False)
        source2, wt2 = self.private(self.home, "eta-pool", "fm/eta-task")
        self.report("eta-task", source2, wt2, number=7)
        self.report("theta-task", source2, wt2, number=7, pane_id="w7:p1")
        _, text = self.listed()
        for task in ("zeta-task", "eta-task", "theta-task"):
            self.assertHidden(text, task)

    def test_an_explicit_project_root_or_allowlist_keeps_private_clones_out(self):
        # Break: the private-clone rule overrides a scope the user configured on purpose.
        self.report("alpha-task", self.alpha, number=1)
        source, wt = self.private(self.home, "beta-pool", "fm/beta-task")
        self.report("beta-task", source, wt, number=2)
        for extra in ({"project_root": str(self.home / "projects")}, {"project_allowlist": (str(self.alpha),)}):
            with self.subTest(extra=extra):
                config = Config(firstmate_home=str(self.home), socket_path=self.config.socket_path,
                                herdr_pane_id="w0:p1", **extra)
                _, text = self.listed(config)
                self.assertIn(humanize("alpha-task"), text)
                self.assertHidden(text, "beta-task")

    def test_pull_requests_list_and_query_exactly_what_they_did_without_the_private_clone(self):
        # Break: admitting a private clone adds it to Pull Requests as its own project (a second
        # group for the same GitHub repository) or queries it, or drops the projects/ inventory
        # an allowlist workaround would lose.
        self.repo(self.home / "projects" / "other")
        self.report("alpha-task", self.alpha, number=1)
        _, without = self.pull_requests()
        # Named like the canonical project, as FirstMate's task-private clones are.
        source, wt = self.private(self.home, "beta-pool", "fm/beta-task", name="alpha")
        self.report("beta-task", source, wt, number=2)
        app, with_clone = self.pull_requests()
        self.assertEqual(with_clone[0], without[0])
        self.assertEqual(with_clone[1], without[1])
        self.assertTrue({str(self.alpha), str(self.home / "projects" / "other")} <= without[1])
        app.handle_key("1")
        self.assertIn(humanize("beta-task"), strip_sgr(app.render()))

    def pull_requests(self):
        """(app, (Pull Requests tab text, the project paths it queried with git))."""
        self.transport.calls = []
        app, _ = self.listed()
        app.handle_key("4")
        queried = {call["argv"][2] for call in self.transport.calls if call["argv"][1:2] == ["-C"] and "remote" in call["argv"]}
        return app, (strip_sgr(app.render()), queried)


if __name__ == "__main__":
    unittest.main()
