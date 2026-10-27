"""The Pull Requests tab, and the Agents tab without its Projects/Pull Requests chrome.

Each test names the production break it exists to catch. GitHub is a fake `gh`/`git` handler.
"""

import json
import re
from concurrent.futures import Future

from firstpane.config import Config
from tests.support import CmdResult, agent_for, pane, selected_line, snapshot, workspace, write_meta
from tests.test_behavior import AppFixture, strip_sgr

def active_tab(frame):
    match = re.search(r"\x1b\[1;4(?:;[0-9;]+)?m(.*?)\x1b\[0m", frame.splitlines()[0])
    return match.group(1) if match else None


def pr(number, title):
    return {"number": number, "title": title, "isDraft": False, "reviewDecision": "APPROVED", "statusCheckRollup": []}


class InventoryFixture(AppFixture):
    """Five projects under a project root and no agents."""

    NAMES = ("alpha", "beta", "delta", "epsilon", "gamma")

    def setUp(self):
        super().setUp()
        self.projects = self.root / "projects"
        for name in self.NAMES:
            (self.projects / name / ".git").mkdir(parents=True)
        self.config = Config(
            firstmate_home=str(self.home),
            firstmate_root=str(self.tool),
            socket_path="/Users/x/.config/herdr/sessions/lab/herdr.sock",
            project_root=str(self.projects),
        )
        self.remotes = {name: f"origin\tgit@github.com:acme/{name}.git (fetch)\n" for name in self.NAMES}
        self.remotes["epsilon"] = "origin\tgit@gitlab.example:acme/epsilon.git (fetch)\n"
        self.listed = {
            "alpha": [pr(11, "Alpha one"), pr(12, "Alpha two")],
            "beta": [],
            "delta": None,
            "gamma": [pr(31, "Gamma one")],
        }
        self.transport.handlers.append((lambda argv: True, self.handle))

    def handle(self, call):
        argv = call["argv"]
        if argv[:2] == ["git", "-C"] and argv[3:] == ["remote", "-v"]:
            return CmdResult(0, self.remotes[argv[2].rsplit("/", 1)[1]])
        if argv[:1] == ["gh"] and "list" in argv:
            name = argv[argv.index("-R") + 1].rsplit("/", 1)[1]
            if self.listed[name] is None:
                return CmdResult(1, "", "HTTP 401: Bad credentials")
            return CmdResult(0, json.dumps(self.listed[name]))
        return CmdResult(127, "", "unexpected")

    def list_calls(self):
        return [call for call in self.gh_calls() if "list" in call["argv"]]

    def body(self, app):
        return "\n".join(strip_sgr(app.render()).splitlines()[1:])

    def open_list(self, app):
        """The Pull Requests tab with its project-wide PR list (the PR selector) open."""
        app.handle_key("4")
        app.handle_key("p")

    def listed_prs(self, app):
        """PR numbers shown as rows of the project-wide list."""
        app.render()
        return [row["number"] for row in app.hit_rows if row and row["kind"] == "pr"]


class PullRequestTabTests(InventoryFixture):
    def test_only_projects_with_open_prs_are_listed_under_their_own_header(self):
        # Break: every project is listed (empty ones too), PRs land under the wrong project,
        # or closed PRs are requested.
        app = self.app()
        app.refresh(manual=True)
        self.assertEqual(self.gh_calls(), [])
        self.open_list(app)
        text = self.body(app)
        order = [text.index(word) for word in ("alpha", "#11", "#12", "delta", "gamma", "#31")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("beta", text)
        self.assertNotIn("epsilon", text)
        argvs = [call["argv"] for call in self.list_calls()]
        self.assertEqual(len(argvs), 4)
        self.assertTrue(all(argv[argv.index("--state") + 1] == "open" for argv in argvs))

    def test_a_failed_project_is_listed_with_its_reason_never_as_empty(self):
        # Break: a gh auth/API failure drops the project, or the pane claims there are no PRs.
        for name in self.NAMES:
            self.listed[name] = []
        self.listed["delta"] = None
        app = self.app()
        app.refresh(manual=True)
        app.handle_key("4")
        self.assertIn("delta", self.body(app))
        self.assertIn("PRs unavailable: gh failed", self.body(app))
        self.assertNotIn("no open pull requests", self.body(app))

    def test_a_retry_that_succeeds_replaces_the_error_and_empty_projects_disappear(self):
        # Break: the error row survives a good refresh, or a recovered empty project stays listed.
        app = self.app()
        app.refresh(manual=True)
        self.open_list(app)
        self.assertIn("PRs unavailable: gh failed", self.body(app))
        self.listed["delta"] = []
        app.handle_key("r")
        self.assertNotIn("delta", self.body(app))
        self.assertNotIn("PRs unavailable", self.body(app))
        self.listed["delta"] = [pr(41, "Delta one")]
        app.handle_key("r")
        self.assertIn("#41", self.body(app))

    def test_a_git_remote_failure_is_retried_not_remembered(self):
        # Break: one failed `git remote -v` marks the project unavailable until the pane restarts.
        remote_ok = {"alpha": False}
        original = self.handle

        def flaky(call):
            argv = call["argv"]
            if argv[:2] == ["git", "-C"] and argv[2].endswith("/alpha") and not remote_ok["alpha"]:
                return CmdResult(128, "", "fatal: not a git repository")
            return original(call)

        self.transport.handlers[:] = [(lambda argv: True, flaky)]
        app = self.app()
        app.refresh(manual=True)
        self.open_list(app)
        self.assertIn("PRs unavailable: git remote failed", self.body(app))
        remote_ok["alpha"] = True
        app.handle_key("r")
        self.assertIn("#11", self.body(app))
        self.assertNotIn("git remote failed", self.body(app))

    def test_a_failed_refresh_does_not_keep_showing_the_old_pr_list(self):
        # Break: after gh starts failing, last minute's PRs are still shown as open.
        app = self.app()
        app.refresh(manual=True)
        self.open_list(app)
        self.assertIn("#11", self.body(app))
        self.listed["alpha"] = None
        app.handle_key("r")
        text = self.body(app)
        self.assertNotIn("#11", text)
        self.assertIn("PRs unavailable: gh failed", text)

    def test_when_every_project_was_checked_and_none_has_prs_it_says_so(self):
        # Break: an empty tab is blank, so nobody can tell it from a tab that has not loaded.
        for name in self.NAMES:
            self.listed[name] = []
        app = self.app()
        app.refresh(manual=True)
        app.handle_key("4")
        text = self.body(app)
        self.assertIn("no open pull requests", text)
        self.assertIn("1 not queried", text)

    def test_unqueryable_projects_are_counted_not_listed(self):
        # Break: a project with no GitHub remote is listed as an error row, or silently ignored.
        app = self.app()
        app.refresh(manual=True)
        self.open_list(app)
        text = self.body(app)
        self.assertIn("1 not queried", text)
        self.assertNotIn("epsilon", text)
        self.assertNotIn("gitlab", text)

    def test_loading_is_partial_state_and_never_blocks_or_claims_empty(self):
        # Break: the tab waits for every gh call before drawing, or shows "no PRs" while loading.
        pending = []

        def submit(fn):
            future = Future()
            pending.append((fn, future))
            return future

        app = self.app(submit=submit)
        app.refresh(manual=True)
        app.handle_key("4")
        self.assertEqual(len(pending), 5)
        text = self.body(app)
        self.assertIn("0 of 5 projects checked", text)
        self.assertNotIn("no open pull requests", text)
        self.assertNotIn("alpha", text)
        app.refresh(manual=True)
        self.assertEqual(len(pending), 5)
        fn, future = pending[0]
        future.set_result(fn())
        text = self.body(app)
        self.assertIn("1 of 5 projects checked", text)
        self.assertIn("#11", text)
        app.handle_key("1")
        self.assertEqual(active_tab(app.render()), "Agents")
        for fn, future in pending[1:]:
            future.set_result(fn())
        app.handle_key("4")
        self.assertNotIn("projects checked", self.body(app))

    def test_gh_runs_only_while_the_tab_is_open_and_on_the_existing_cadence(self):
        # Break: gh polls from other tabs, ignores the 120 s cadence, or never refreshes a stale list.
        app = self.app()
        app.refresh(manual=True)
        self.clock.t += 500
        app.refresh()
        self.assertEqual(self.gh_calls(), [])
        app.handle_key("4")
        self.assertEqual(len(self.list_calls()), 4)
        self.clock.t += 10
        app.refresh()
        self.assertEqual(len(self.list_calls()), 4)
        self.clock.t += 121
        app.refresh()
        self.assertEqual(len(self.list_calls()), 8)
        app.handle_key("1")
        self.clock.t += 500
        app.refresh()
        self.assertEqual(len(self.list_calls()), 8)
        app.handle_key("4")
        self.assertEqual(len(self.list_calls()), 12)

    def test_project_headers_are_colored_from_the_palette_and_plain_without_color(self):
        # Break: PR project headers are uncolored, or NO_COLOR still emits escapes or changes words.
        app = self.app()
        app.refresh(manual=True)
        self.open_list(app)
        colored = app.render()
        for name in ("alpha", "gamma"):
            self.assertRegex(colored, r"\x1b\[1;38;2;244;143;201m[\u25b8\u25be] %s" % name)
        plain = self.app(env={"NO_COLOR": "1"})
        plain.refresh(manual=True)
        self.open_list(plain)
        frame = plain.render()
        self.assertNotIn("\x1b", frame)
        self.assertEqual(strip_sgr(colored), frame)

    def test_a_project_header_folds_and_unfolds_its_prs(self):
        # Break: Enter on a PR project header does nothing, or folding hides another project's PRs.
        app = self.app()
        app.refresh(manual=True)
        self.open_list(app)
        app.handle_key("k")
        self.select_line(app, "alpha")
        app.handle_key("enter")
        self.assertEqual(self.listed_prs(app), [31])
        app.handle_key("enter")
        self.assertEqual(self.listed_prs(app), [11, 12, 31])

    def test_enter_on_an_unavailable_project_retries_only_that_project(self):
        # Break: a failed project can only be retried by refreshing everything, or Enter on it folds nothing and fetches nothing.
        app = self.app()
        app.refresh(manual=True)
        self.open_list(app)
        before = len(self.list_calls())
        self.listed["delta"] = [pr(41, "Delta one")]
        self.select_line(app, "delta")
        app.handle_key("enter")
        self.assertEqual(len(self.list_calls()), before + 1)
        self.assertIn("#41", self.body(app))
        self.assertNotIn("PRs unavailable", self.body(app))

    def test_a_long_pr_list_keeps_its_project_header_on_screen(self):
        # Break: scrolling down a long PR list drops the project header, so PRs lose their repository.
        self.listed["alpha"] = [pr(number, f"Alpha {number}") for number in range(1, 21)]
        app = self.app()
        app.rows = 14
        app.refresh(manual=True)
        self.open_list(app)
        self.assertEqual(app.pr_selection_key[-1], 1)
        for _ in range(14):
            app.handle_key("j")
        frame = app.render()
        header = next(index for index, row in enumerate(app.hit_rows) if row and row["kind"] == "pr-project")
        selected = next(index for index, line in enumerate(strip_sgr(frame).splitlines()) if line.startswith(">"))
        self.assertLess(header, selected)
        self.assertIn("alpha", strip_sgr(frame.splitlines()[header]))
        self.assertIn("#15", strip_sgr(frame.splitlines()[selected]))

    def test_the_tab_and_its_fold_state_survive_a_restart_and_load_on_start(self):
        # Break: a restart on the PR tab shows an empty tab that never fetches, or forgets what was folded.
        app = self.app()
        app.refresh(manual=True)
        self.open_list(app)
        app.handle_key("k")
        self.select_line(app, "alpha")
        app.handle_key("enter")
        before = len(self.list_calls())
        restarted = self.app()
        restarted.refresh(manual=True)
        self.assertEqual(active_tab(restarted.render()), "Pull Requests")
        restarted.handle_key("p")
        self.assertEqual(self.listed_prs(restarted), [31])
        self.assertGreater(len(self.list_calls()), before)

    def test_the_tab_opens_by_key_and_by_click_and_one_returns_to_agents(self):
        # Break: 4 is not bound, the label is not clickable, or key 3 no longer reaches Browser.
        app = self.app()
        app.refresh(manual=True)
        self.assertEqual(strip_sgr(app.render()).splitlines()[0].split(),
                         ["Agents", "Changes", "Browser", "Pull", "Requests", "Board", "Settings"])
        app.handle_key("3")
        self.assertEqual(active_tab(app.render()), "Browser")
        app.handle_key("4")
        self.assertEqual(active_tab(app.render()), "Pull Requests")
        app.handle_key("1")
        self.assertEqual(active_tab(app.render()), "Agents")
        column = strip_sgr(app.render()).splitlines()[0].index("Pull Requests") + 3
        app.handle_bytes(f"\x1b[<0;{column};1M")
        self.assertEqual(active_tab(app.render()), "Pull Requests")

    def test_narrow_panes_keep_the_pull_requests_tab_reachable(self):
        # Break: the fourth tab overflows a narrow row or loses its short label.
        app = self.app()
        app.cols = 50
        app.refresh(manual=True)
        first = strip_sgr(app.render()).splitlines()[0]
        self.assertEqual(first.split(), ["Agents", "Ch", "Br", "PR", "Bd", "Set"])
        app.handle_key("4")
        frame = app.render()
        first = strip_sgr(frame).splitlines()[0]
        self.assertEqual(first.split(), ["Ag", "Ch", "Br", "Pull", "Requests", "Bd", "Set"])
        self.assertTrue(all(len(strip_sgr(line)) <= 50 for line in frame.splitlines()))


class AgentContextFixture(AppFixture):
    def setUp(self):
        super().setUp()
        self.config = Config(
            firstmate_home=str(self.home),
            firstmate_root=str(self.tool),
            socket_path="/Users/x/.config/herdr/sessions/lab/herdr.sock",
            github_hosts=("github.com",),
            project_root="/repos",
        )
        for task, repo, number in (("fm-helper", "alpha", 1), ("beta-task", "beta", 2)):
            write_meta(
                self.home,
                task,
                project=f"/repos/{repo}",
                herdr_session="lab",
                herdr_workspace_id=f"w{number}",
                herdr_tab_id=f"w{number}:t1",
                herdr_pane_id=f"w{number}:p1",
                endpoint_task_id=task,
            )
        one = pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1", cwd="/repos/alpha", foreground_cwd="/repos/alpha")
        two = pane(pane_id="w2:p1", terminal_id="term_b", workspace_id="w2", tab_id="w2:t1", cwd="/repos/beta", foreground_cwd="/repos/beta", agent_status="idle")
        self.transport.snapshot = snapshot(
            [workspace("w1", "alpha"), workspace("w2", "beta", number=2)],
            [one, two],
            [agent_for(one), agent_for(two)],
        )
        self.transport.pane_get["w1:p1"] = dict(one)
        self.transport.pane_get["w2:p1"] = dict(two)
        self.transport.handlers.append(
            (
                lambda argv: True,
                lambda call: CmdResult(0, "origin\tgit@github.com:acme/x.git (fetch)\n")
                if call["argv"][-2:] == ["remote", "-v"]
                else CmdResult(0, json.dumps([pr(5, "Open work")]))
                if "list" in call["argv"]
                else CmdResult(127, "", "no"),
            )
        )


class AgentsTabTests(AgentContextFixture):
    def test_agents_are_grouped_under_colored_project_headers_with_no_section_chrome(self):
        # Break: the Projects/Pull Requests category rows come back, the header is not colored,
        # or agent rows are colored beyond their status word.
        app = self.app()
        app.refresh(manual=True)
        frame = app.render()
        lines = frame.splitlines()
        self.assertNotIn("Projects", strip_sgr(frame))
        self.assertNotIn("Pull Requests", "\n".join(strip_sgr(line) for line in lines[1:]))
        first = next(index for index, line in enumerate(lines) if "\u25be alpha" in strip_sgr(line))
        self.assertIn("1;38;2;244;143;201malpha\x1b[0m", lines[first])
        self.assertIn("1 agent", strip_sgr(lines[first]))
        self.assertIn("Fm helper", strip_sgr(lines[first + 1]))
        beta = next(index for index, line in enumerate(lines) if "\u25be beta" in strip_sgr(line))
        self.assertGreater(beta, first + 2)

    def test_an_agent_is_two_lines_with_status_on_the_right_and_details_below(self):
        # Break: the status word or chevron leaves the first line, or the files/status move to the
        # identity line.
        app = self.app()
        app.refresh(manual=True)
        lines = strip_sgr(app.render()).splitlines()
        first = next(index for index, line in enumerate(lines) if "Fm helper" in line)
        title, details = lines[first], lines[first + 1]
        self.assertRegex(title, r"\u25cf\s+Fm helper\s+Working \u00b7 workspace unknown\s+\u203a\s*$")
        self.assertNotIn("Working", details)
        self.assertNotIn("workspace unknown", details)

    def test_selecting_an_agent_binds_the_same_target(self):
        # Break: the display change alters the selected target or its pane binding, which
        # every inline message to FirstMate names.
        app = self.app()
        app.refresh(manual=True)
        self.select_line(app, "Fm helper")
        app.handle_key("enter")
        target = app.target
        self.assertEqual(target["key"], ("agent", "/repos/alpha", "term_a"))
        self.assertEqual(target["task_id"], "fm-helper")
        self.assertEqual(target["label"], "fm-helper")
        self.assertEqual(target["pane_id"], "w1:p1")
        self.assertEqual(target["harness"], "claude")
        self.assertEqual(target["status"], "working")
        context = strip_sgr(app.render()).splitlines()
        self.assertTrue(any(line.startswith(" alpha / fm-helper") for line in context))
        app.handle_key("2")
        self.assertIn("alpha / fm-helper", strip_sgr(app.render()).splitlines()[1])


class TabNavigationTests(AgentContextFixture):
    def test_pr_tab_keeps_the_selected_agent_context_and_the_agents_cursor(self):
        # Break: moving through PRs retargets the selected agent, or coming back to Agents
        # loses the row the cursor was on.
        app = self.app()
        app.refresh(manual=True)
        self.select_line(app, "Beta task")
        app.handle_key("enter")
        target = dict(app.target)
        app.handle_key("4")
        self.assertEqual(active_tab(app.render()), "Pull Requests")
        for key in ("j", "j", "k", "k", "enter", "enter"):
            app.handle_key(key)
        self.assertEqual(app.target, target)
        frame = strip_sgr(app.render())
        self.assertIn("beta / beta-task", frame)
        app.handle_key("1")
        frame = app.render()
        self.assertEqual(active_tab(frame), "Agents")
        self.assertIn("Beta task", selected_line(frame))
        self.assertEqual(app.target, target)

    def test_changes_still_follows_the_selected_agent_after_visiting_pull_requests(self):
        # Break: switching to the PR tab drops the workspace read for the selected agent.
        app = self.app()
        app.refresh(manual=True)
        self.select_line(app, "Fm helper")
        app.handle_key("enter")
        app.handle_key("4")
        app.handle_key("2")
        self.assertEqual(active_tab(app.render()).split()[0], "Changes")
        self.assertIn("alpha / fm-helper", strip_sgr(app.render()).splitlines()[1])
