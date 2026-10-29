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
    """The underlined tab in the tab row."""
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
        (app.handle_key("2"), None if app.review_source == "pr" else app.handle_key("\\"))
        app.handle_key("p")

    def listed_prs(self, app):
        """PR numbers shown as rows of the project-wide list."""
        app.render()
        return [row["number"] for row in app.hit_rows if row and row["kind"] == "pr"]


class PullRequestTabTests(InventoryFixture):
    def test_review_does_not_list_every_projects_pull_requests(self):
        # Break: Review falls back to a project-wide pull request inventory when no agent is selected.
        app = self.app()
        app.refresh(manual=True)
        self.assertEqual(self.gh_calls(), [])
        (app.handle_key("2"), None if app.review_source == "pr" else app.handle_key("\\"))
        app.handle_key("p")
        text = self.body(app)
        self.assertIn("no agent selected", text)
        self.assertNotIn("#11", text)
        self.assertNotIn("#31", text)
        self.assertEqual(self.list_calls(), [])

    def test_the_tab_opens_by_key_and_by_click_and_one_returns_to_agents(self):
        # Break: 4 is not bound, the label is not clickable, or key 3 no longer reaches Browser.
        app = self.app()
        app.refresh(manual=True)
        self.assertEqual(strip_sgr(app.render()).splitlines()[0].split(),
                         ["Agents", "?", "Review", "Board", "Terminal", "Browser", "Settings"])
        app.handle_key("5")
        self.assertEqual(active_tab(app.render()), "Browser")
        (app.handle_key("2"), None if app.review_source == "pr" else app.handle_key("\\"))
        self.assertEqual(active_tab(app.render()), "Review")
        self.assertEqual(app.review_source, "pr")
        app.handle_key("1")
        self.assertEqual(active_tab(app.render()), "Agents")
        column = strip_sgr(app.render()).splitlines()[0].index("Review") + 3
        app.handle_bytes(f"\x1b[<0;{column};1M")
        self.assertEqual(active_tab(app.render()), "Review")
        self.assertEqual(app.review_source, "pr")

    def test_narrow_panes_keep_the_pull_requests_tab_reachable(self):
        # Break: the fourth tab overflows a narrow row or loses its short label.
        app = self.app()
        app.cols = 50
        app.refresh(manual=True)
        first = strip_sgr(app.render()).splitlines()[0]
        self.assertEqual(first.split(), ["Agents", "?", "Rv", "Bd", "Tm", "Br", "Set"])
        (app.handle_key("2"), None if app.review_source == "pr" else app.handle_key("\\"))
        frame = app.render()
        first = strip_sgr(frame).splitlines()[0]
        self.assertEqual(first.split(), ["Ag", "?", "Review", "Bd", "Tm", "Br", "Set"])
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
        self.assertIn("Fm helper", selected_line(app.render()))
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
        (app.handle_key("2"), None if app.review_source == "pr" else app.handle_key("\\"))
        self.assertEqual(active_tab(app.render()), "Review")
        self.assertEqual(app.review_source, "pr")
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
        (app.handle_key("2"), None if app.review_source == "pr" else app.handle_key("\\"))
        app.handle_key("\\")
        self.assertEqual(active_tab(app.render()).split()[0], "Review")
        self.assertEqual(app.review_source, "workspace")
        self.assertIn("alpha / fm-helper", strip_sgr(app.render()).splitlines()[1])
