"""Agents: a FirstMate direct report whose project is a task-private source clone.

FirstMate can give a task its own clone of a project under the FirstMate home's `data/` (with the
agent's worktree checked out from that clone) instead of `projects/<name>`. Such a report is listed
when its binding is live and unique and its worktree is a checkout of that clone; everything else
under `data/`, another home's clones, stale or doubly claimed bindings, and explicit
`project_root` / `project_allowlist` scopes stay as they were. A clone Git proves was made from a
listed project (its origin, or with no origin its recorded clone source) is listed under that
project; every other clone stays its own group. Herdr is faked at the socket boundary; Git runs
for real.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from firstpane.agents_view import humanize
from firstpane.app import App
from firstpane.config import Config
from firstpane.transport import LiveTransport
from tests.gitfixture import git_env
from tests.support import Clock, FakePi, FakeTerminal, FakeTransport, agent_for, pane, selected_line, snapshot, strip_sgr, workspace, write_meta

HEADER = re.compile(r"[\u25be\u25b8] (\S+)\s+(\d+) agents?\s*$")


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

    # ---- clones of a listed project -----------------------------------------------------
    def clone(self, source, pool, origin=None, name=None):
        """`git clone` of `source` to `<home>/data/<pool>/source/<name>` with its origin removed
        (or set to `origin`), and a worktree of it, as FirstMate makes a task's own copy."""
        path = self.home / "data" / pool / "source" / (name or Path(source).name)
        path.parent.mkdir(parents=True)
        self.git(self.root, "clone", "-q", str(source), str(path))
        if origin:
            self.git(path, "remote", "set-url", "origin", origin)
        else:
            self.git(path, "remote", "remove", "origin")
        return path, self.worktree(path, self.home / "data" / pool / "pool" / "wt", f"fm/{pool}")

    def headers(self, text):
        """(label, agent count) of each project heading on the Agents tab, top to bottom."""
        return [(match.group(1), int(match.group(2))) for line in text.splitlines() if (match := HEADER.search(line))]

    def listed_under(self, app, task):
        """(heading key, the row's own project_key) of the task's agent row."""
        return next((project["key"], row["project_key"]) for project in app.projects
                    for row in project["agents"] if row.get("task_id") == task)

    def test_reports_in_clones_of_a_listed_project_are_listed_under_it_and_keep_their_own_source(self):
        # Break (the user's case): each task-private clone of one project is its own heading
        # (`alpha/source/<hash>`), a clone of a clone is not traced back, a clone made from the
        # project's GitHub origin is not matched by that origin, or grouping rewrites the rows'
        # own source (their workspace and Git actions are checked against it).
        self.report("alpha-task", self.alpha, self.worktree(self.alpha, self.root / "alpha-wt", "fm/alpha-task"), number=1)
        one, one_wt = self.clone(self.alpha, "one-pool")
        two, two_wt = self.clone(one, "two-pool")
        self.report("alpha-one", one, one_wt, number=2)
        self.report("alpha-two", two, two_wt, number=3)
        beta = self.repo(self.home / "projects" / "beta")
        self.git(beta, "remote", "add", "origin", "https://github.com/acme/beta.git")
        hosted = self.root / "hosted" / "beta.git"
        self.git(self.root, "clone", "-q", "--bare", str(beta), str(hosted))
        three, three_wt = self.clone(hosted, "three-pool", origin="https://github.com/acme/beta.git", name="beta")
        self.report("beta-one", three, three_wt, number=4)
        app, text = self.listed()
        self.assertEqual(self.headers(text), [("alpha", 3), ("beta", 1)], text)
        self.assertNotIn("/source", text)
        self.assertEqual(self.listed_under(app, "alpha-one"), (str(self.alpha), str(one)))
        self.assertEqual(self.listed_under(app, "alpha-two"), (str(self.alpha), str(two)))
        self.assertEqual(self.listed_under(app, "beta-one"), (str(beta), str(three)))

    def test_a_clone_git_does_not_prove_was_made_from_the_listed_project_stays_its_own_project(self):
        # Break: a same-named clone is grouped by its name, by shared history alone (a clone whose
        # origin is now another repository), or by a guess when its origin is not a repository URL,
        # Git keeps no clone record, or the clone it was made from is gone.
        self.git(self.alpha, "remote", "add", "origin", "https://github.com/acme/alpha.git")
        self.report("alpha-task", self.alpha, self.worktree(self.alpha, self.root / "alpha-wt", "fm/alpha-task"), number=1)
        other, other_wt = self.private(self.home, "other-pool", "fm/other", name="alpha")
        self.git(other, "remote", "add", "origin", "https://github.com/other/alpha.git")
        fork, fork_wt = self.clone(self.alpha, "fork-pool", origin="https://github.com/fork/alpha.git")
        odd, odd_wt = self.clone(self.alpha, "odd-pool", origin="not a repository")
        bare, bare_wt = self.clone(self.alpha, "bare-pool")
        shutil.rmtree(bare / ".git" / "logs")
        gone, _ = self.clone(self.alpha, "gone-pool")
        orphan, orphan_wt = self.clone(gone, "orphan-pool")
        shutil.rmtree(gone)
        cases = {"other-task": (other, other_wt), "fork-task": (fork, fork_wt), "odd-task": (odd, odd_wt),
                 "bare-task": (bare, bare_wt), "orphan-task": (orphan, orphan_wt)}
        for number, (task, (source, wt)) in enumerate(cases.items(), start=2):
            self.report(task, source, wt, number=number)
        app, text = self.listed()
        self.assertEqual(self.headers(text)[0], ("alpha", 1), text)
        self.assertEqual(len(self.headers(text)), 6, text)
        for task, (source, _) in cases.items():
            self.assertEqual(self.listed_under(app, task), (str(source), str(source)), task)

    def test_an_origin_two_listed_projects_share_does_not_choose_one_of_them(self):
        # Break: a clone whose origin matches more than one listed project joins the first.
        for path in (self.alpha, self.repo(self.home / "projects" / "alpha-mirror")):
            self.git(path, "remote", "add", "origin", "https://github.com/acme/alpha.git")
        self.report("alpha-task", self.alpha, self.worktree(self.alpha, self.root / "alpha-wt", "fm/alpha-task"), number=1)
        one, one_wt = self.clone(self.alpha, "one-pool", origin="https://github.com/acme/alpha.git")
        self.report("alpha-one", one, one_wt, number=2)
        app, _ = self.listed()
        self.assertEqual(self.listed_under(app, "alpha-one"), (str(one), str(one)))

    def test_selection_fold_search_and_message_keep_the_exact_clone_agent_after_grouping(self):
        # Break: grouping sends the message with the listed project's path or another agent's
        # pane/workspace, unfolding the project restores an agent other than the one last
        # selected, or a selected clone agent hidden by a search falls to the first project
        # instead of its own heading.
        self.report("alpha-zed", self.alpha, self.worktree(self.alpha, self.root / "alpha-wt", "fm/alpha-zed"), number=1)
        one, one_wt = self.clone(self.alpha, "one-pool")
        two, two_wt = self.clone(one, "two-pool")
        self.report("alpha-one", one, one_wt, number=2)
        self.report("alpha-two", two, two_wt, number=3)
        beta = self.repo(self.home / "projects" / "beta")
        self.report("beta-zed", beta, self.worktree(beta, self.root / "beta-wt", "fm/beta-zed"), number=4)
        three, three_wt = self.clone(beta, "three-pool")
        self.report("beta-one", three, three_wt, number=6)
        main = FakePi(self.transport, self.home)
        self.rows.append(main.pane)
        app, _ = self.listed()

        def go(task):
            for _ in range(20):
                app.handle_key("k")
            for _ in range(20):
                if humanize(task) in selected_line(app.render()):
                    return
                app.handle_key("j")
            raise AssertionError(strip_sgr(app.render()))

        go("alpha-two")
        app.handle_key("enter")
        app.handle_key("m")
        app.handle_bytes("kontrol et".encode())
        app.handle_key("enter")
        app.handle_key("y")
        body = main.received[-1]
        self.assertIn(f"project: alpha  {two}\n", body)
        self.assertIn("agent: alpha-two  alpha-two-agent  pane w3:p1  terminal term_alpha-two\n", body)
        self.assertIn(f"workspace: {two_wt}  (fm)\n", body)
        app.handle_key("esc")
        go("alpha-one")
        app.handle_key("k")  # to the heading: alpha-one, a clone's agent, was selected last
        self.assertIn("alpha", selected_line(app.render()))
        app.handle_key("enter")
        self.assertNotIn(humanize("alpha-one"), strip_sgr(app.render()))
        app.handle_key("enter")
        self.assertIn(humanize("alpha-one"), selected_line(app.render()))
        go("beta-one")
        app.handle_key("/")
        app.handle_bytes("zed".encode())
        self.assertIn("beta", selected_line(app.render()))
        self.assertNotIn(humanize("beta-one"), strip_sgr(app.render()))


if __name__ == "__main__":
    unittest.main()
