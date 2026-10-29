"""The Pull Requests tab as a review page for one selected pull request.

Each test names the production break it exists to catch. GitHub is a fake `gh` handler with
synthetic pull requests, review threads and answers; nothing reaches GitHub or an inbox.
"""

import json
import time

from firstpane.config import Config
from tests.support import CmdResult, FakePi, agent_for, note_json, pane, snapshot, workspace, write_meta
from tests.test_behavior import AppFixture, strip_sgr

NOW = 1_793_000_000.0
HUNK = (
    "@@ -36,6 +36,8 @@ def summarize(agents):\n"
    "     if not agents:\n"
    "         return 'idle'\n"
    "     counts = {}\n"
    "+    active = any(agent.working for agent in agents)\n"
    "+    return 'working' if active else 'idle'\n"
)


def iso(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds_ago))


def comment(cid, author, ago, body, review="COMMENTED", hunk=HUNK):
    return {
        "id": cid, "author": {"login": author} if author else None, "body": body, "createdAt": iso(ago),
        "diffHunk": hunk, "pullRequestReview": {"state": review},
    }


def thread(tid, comments, resolved=False, path="app/sidebar.py", line=40, start=39, can=True, by=None):
    return {
        "id": tid, "isResolved": resolved, "isOutdated": False, "path": path, "line": line, "startLine": start,
        "originalLine": line, "originalStartLine": start, "diffSide": "RIGHT", "viewerCanResolve": can,
        "resolvedBy": {"login": by} if by else None, "comments": {"totalCount": len(comments), "nodes": comments},
    }


def listed(number, title, head):
    return {"number": number, "title": title, "isDraft": False, "reviewDecision": None, "statusCheckRollup": [], "headRefName": head}


def view(number, title, head, **fields):
    payload = {
        "number": number, "title": title, "state": "OPEN", "isDraft": False, "author": {"login": "alice"},
        "headRefName": head, "baseRefName": "main", "headRefOid": "0123456789abcdef0123456789abcdef01234567",
        "additions": 28, "deletions": 9, "changedFiles": 2,
        "files": [
            {"path": "app/sidebar.py", "additions": 20, "deletions": 4, "changeType": "MODIFIED"},
            {"path": "app/old.py", "additions": 0, "deletions": 5, "changeType": "DELETED"},
        ],
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "test", "workflowName": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "lint", "workflowName": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "e2e", "workflowName": "ci", "status": "IN_PROGRESS", "conclusion": ""},
        ],
    }
    payload.update(fields)
    return payload


DIFF = (
    "diff --git a/app/old.py b/app/old.py\ndeleted file mode 100644\n--- a/app/old.py\n+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-OLD_FILE_LINE\n"
    "diff --git a/app/sidebar.py b/app/sidebar.py\n--- a/app/sidebar.py\n+++ b/app/sidebar.py\n"
    "@@ -1 +1 @@\n-before\n+SIDEBAR_LINE\n"
)


class ReviewFixture(AppFixture):
    def setUp(self):
        super().setUp()
        self.projects = self.root / "projects"
        for name in ("alpha", "beta"):
            (self.projects / name / ".git").mkdir(parents=True)
        self.worktree = self.root / "wt-alpha"
        self.worktree.mkdir()
        self.config = Config(
            firstmate_home=str(self.home),
            firstmate_root=str(self.tool),
            socket_path="/Users/x/.config/herdr/sessions/lab/herdr.sock",
            project_root=str(self.projects),
        )
        write_meta(
            self.home, "alpha-task", project=str(self.projects / "alpha"), herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="w1:p1", endpoint_task_id="alpha-task",
            worktree=str(self.worktree),
        )
        row = pane(pane_id="w1:p1", terminal_id="term_a", workspace_id="w1", tab_id="w1:t1", cwd=str(self.worktree))
        self.transport.snapshot = snapshot([workspace("w1", "alpha")], [row], [agent_for(row)])
        self.main = FakePi(self.transport, self.home)
        self.transport.pane_get["w1:p1"] = dict(row)
        self.lists = {
            "alpha": [listed(7, "Sidebar status", "fm/other"), listed(9, "Agent branch work", "fm/alpha-task")],
            "beta": [listed(12, "Beta cleanup", "fm/beta")],
        }
        self.views = {
            7: view(7, "Sidebar status", "fm/other"),
            9: view(9, "Agent branch work", "fm/alpha-task"),
            12: view(12, "Beta cleanup", "fm/beta"),
        }
        self.threads = {
            7: [
                thread("PRRT_one", [
                    comment("C1", "reviewer", 720, "What should the status be when the agent list is empty?", "CHANGES_REQUESTED"),
                    comment("C2", "alice", 300, "Good point, will change."),
                ]),
                thread("PRRT_two", [comment("C3", None, 7200, "Is this import still used?", hunk="")], path="app/old.py", line=None, start=None, can=False),
                thread("PRRT_done", [comment("C4", "reviewer", 172800, "RESOLVED_TEXT typo")], resolved=True, by="alice"),
            ],
            9: [],
            12: [],
        }
        self.threads_fail = False
        self.view_fail = False
        self.resolve_answer = None
        self.branch_head = "fm/other"
        self.transport.handlers.append((lambda argv: True, self.handle))

    def handle(self, call):
        argv = call["argv"]
        if argv[:2] == ["git", "-C"] and argv[3:] == ["remote", "-v"]:
            return CmdResult(0, f"origin\tgit@github.com:acme/{argv[2].rsplit('/', 1)[1]}.git (fetch)\n")
        if argv[:1] == ["git"] and "--show-toplevel" in argv:
            return CmdResult(0, f"{self.worktree}\n{self.projects / 'alpha' / '.git'}\n")
        if argv[:1] == ["git"] and "status" in argv:
            return CmdResult(0, "# branch.oid 0123456789abcdef0123456789abcdef01234567\0# branch.head " + self.branch_head + "\0")
        if argv[:1] == ["gh"] and "list" in argv:
            name = argv[argv.index("-R") + 1].rsplit("/", 1)[1]
            listed = self.lists.get(name)
            if listed is None:
                return CmdResult(1, "", "HTTP 502")
            return CmdResult(0, json.dumps(listed))
        if argv[:1] == ["gh"] and "view" in argv:
            if self.view_fail:
                return CmdResult(1, "", "HTTP 502")
            return CmdResult(0, json.dumps(self.views[int(argv[argv.index("view") + 1])]))
        if argv[:1] == ["gh"] and "diff" in argv:
            return CmdResult(0, DIFF)
        if argv[:3] == ["gh", "api", "graphql"] and any(part.startswith("threadId=") for part in argv):
            if self.resolve_answer is not None:
                return self.resolve_answer
            tid = next(part for part in argv if part.startswith("threadId="))[len("threadId="):]
            for node in self.threads[7]:
                if node["id"] == tid:
                    node["isResolved"] = True
            return CmdResult(0, json.dumps({"data": {"resolveReviewThread": {"thread": {"id": tid, "isResolved": True}}}}))
        if argv[:3] == ["gh", "api", "graphql"]:
            if self.threads_fail:
                return CmdResult(1, "", "HTTP 502")
            number = int(next(part for part in argv if part.startswith("number="))[len("number="):])
            nodes = self.threads[number]
            return CmdResult(0, json.dumps({"data": {"repository": {"pullRequest": {
                "headRefOid": self.views[number].get("headRefOid"),
                "reviewThreads": {"totalCount": len(nodes), "pageInfo": {"hasNextPage": False}, "nodes": nodes},
            }}}}))
        if argv[:2] == ["gh", "api"] and "user" in argv:
            return CmdResult(0, "octocat\n")
        if argv[0].endswith("fm-inbox.sh") and argv[1] == "note":
            return CmdResult(0, note_json("created"))
        return CmdResult(127, "", "unexpected")

    def app(self, **kwargs):
        kwargs.setdefault("wall", lambda: NOW)
        app = super().app(**kwargs)
        app.refresh(manual=True)
        app.handle_key("1")
        try:
            self.select_line(app, "Alpha task")
            app.handle_key("enter")
        except AssertionError:
            pass
        (app.handle_key("2"), None if app.review_source == "pr" else app.handle_key("\\"))
        return app

    def text(self, app):
        return strip_sgr(app.render())

    def click(self, app, line_text, word):
        lines = self.text(app).splitlines()
        row = next(index for index, line in enumerate(lines, 1) if line_text in line)
        column = lines[row - 1].index(word) + 2
        app.handle_bytes(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m")

    def mutations(self):
        return [call for call in self.gh_calls() if any(part.startswith("threadId=") for part in call["argv"])]


class AssociatedPullRequestTests(ReviewFixture):
    def test_same_repository_unrelated_pr_is_not_shown(self):
        # Break: Review shows another open pull request in the same repository, or the first one.
        self.branch_head = "fm/alpha-task"
        app = self.app()
        text = self.text(app)
        self.assertIn("#9", text)
        self.assertIn("Agent branch work", text)
        self.assertNotIn("Sidebar status", text)
        self.assertNotIn("#7", text)
        self.assertNotIn("#12", text)
        self.assertNotIn("beta", text.lower())

    def test_a_recorded_task_url_associates_when_the_branch_does_not(self):
        # Break: a recorded pull URL is ignored, or a same-repository title match is used instead.
        self.branch_head = "fm/no-such-branch"
        write_meta(
            self.home, "alpha-task", project=str(self.projects / "alpha"), herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="w1:p1", endpoint_task_id="alpha-task",
            worktree=str(self.worktree), pr="https://github.com/acme/alpha/pull/7",
        )
        app = self.app()
        text = self.text(app)
        self.assertIn("#7", text)
        self.assertIn("Sidebar status", text)
        self.assertNotIn("#9", text)
        self.assertNotIn("#12", text)

    def test_a_known_branch_with_no_matching_pr_is_absence_not_another_pr(self):
        # Break: no matching pull request falls back to the first open one in the repository.
        self.branch_head = "fm/no-such-branch"
        app = self.app()
        text = self.text(app)
        self.assertIn("no pull request for this branch", text)
        self.assertNotIn("Sidebar status", text)
        self.assertNotIn("#12", text)
        self.assertNotIn("Beta cleanup", text)

    def test_a_failed_pull_request_read_stays_unknown(self):
        # Break: a failed gh list is reported as no pull request, or another project's PR is shown.
        self.lists["alpha"] = None
        app = self.app()
        text = self.text(app)
        self.assertIn("PRs unavailable: gh failed", text)
        self.assertNotIn("no pull request for this branch", text)
        self.assertNotIn("#12", text)

    def test_switching_agents_does_not_keep_the_previous_pull_request(self):
        # Break: the previous agent's pull request or diff stays up after the selection changes.
        self.branch_head = "fm/alpha-task"
        beta = self.projects / "beta"
        write_meta(
            self.home, "beta-task", project=str(beta), herdr_session="lab",
            herdr_workspace_id="w2", herdr_tab_id="w2:t1", herdr_pane_id="w2:p1", endpoint_task_id="beta-task",
            worktree=str(self.worktree),
        )
        row = pane(pane_id="w2:p1", terminal_id="term_b", workspace_id="w2", tab_id="w2:t1", cwd=str(self.worktree))
        self.transport.snapshot = snapshot(
            [workspace("w1", "alpha"), workspace("w2", "beta", number=2)],
            [self.transport.snapshot["panes"][0], row],
            [agent_for(self.transport.snapshot["panes"][0]), agent_for(row, name="beta")],
        )
        self.transport.pane_get["w2:p1"] = dict(row)
        app = self.app()
        self.assertIn("#9", self.text(app))
        app.handle_key("d")
        self.assertTrue(app.pr_diff_open)
        app.handle_key("1")
        self.select_line(app, "Beta task")
        app.handle_key("enter")
        app.handle_key("2")
        if app.review_source != "pr":
            app.handle_key("\\")
        text = self.text(app)
        self.assertNotIn("Agent branch work", text)
        self.assertNotIn("Sidebar status", text)
        self.assertFalse(app.pr_diff_open)
        self.assertNotIn("SIDEBAR_LINE", text)

    def test_a_truncated_open_list_is_not_reported_as_no_pull_request(self):
        # Break: the agent's own PR lies beyond the fetched window and the page says there is none.
        self.branch_head = "fm/alpha-task"
        self.lists["alpha"] = [listed(100 + index, f"Other {index}", f"fm/other-{index}") for index in range(21)]
        text = self.text(self.app())
        self.assertIn("more open pull requests than were listed", text)
        self.assertNotIn("no pull request for this branch", text)
        self.assertNotIn("Other 0", text)

    def test_a_fork_pull_request_with_the_same_branch_name_is_not_the_agents(self):
        # Break: an unrelated contributor's fork PR on a same-named branch is shown as the agent's work.
        self.branch_head = "fm/alpha-task"
        self.lists["alpha"] = [dict(listed(9, "Agent branch work", "fm/alpha-task"), isCrossRepository=True)]
        text = self.text(self.app())
        self.assertIn("no pull request for this branch", text)
        self.assertNotIn("Agent branch work", text)
        self.assertTrue(any("isCrossRepository" in part for call in self.gh_calls() if "list" in call["argv"] for part in call["argv"]))

    def test_a_failed_refresh_keeps_the_open_diff_of_the_same_pull_request(self):
        # Break: one gh failure at the next refresh closes the diff the reader is in.
        self.branch_head = "fm/alpha-task"
        app = self.app()
        app.handle_key("d")
        self.assertTrue(app.pr_diff_open)
        self.lists["alpha"] = None
        app.refresh(manual=True)
        app.poll()
        self.assertIn("PRs unavailable: gh failed", self.text(app))
        self.assertTrue(app.pr_diff_open)
        self.lists["alpha"] = [listed(9, "Agent branch work", "fm/alpha-task")]
        app.refresh(manual=True)
        app.poll()
        self.assertIn("#9", self.text(app))
        self.assertTrue(app.pr_diff_open)
        self.assertIsNotNone(app.diff_view)

    def test_the_recorded_task_file_is_not_read_on_every_frame(self):
        # Break: each render rescans the FirstMate meta files for the recorded pull request.
        self.branch_head = "fm/alpha-task"
        write_meta(
            self.home, "alpha-task", project=str(self.projects / "alpha"), herdr_session="lab",
            herdr_workspace_id="w1", herdr_tab_id="w1:t1", herdr_pane_id="w1:p1", endpoint_task_id="alpha-task",
            worktree=str(self.worktree), pr="https://github.com/acme/alpha/pull/9",
        )
        app = self.app()
        reads = []
        original = app._meta
        app._meta = lambda task_id: (reads.append(task_id), original(task_id))[1]
        for _ in range(5):
            self.text(app)
        self.assertEqual(reads, [])

    def test_a_saved_unrelated_choice_is_not_restored(self):
        # Break: a pull request chosen for another agent is still the page on the next render.
        self.branch_head = "fm/alpha-task"
        app = self.app()
        app.pr_choice = ("pr", "other-project", "github.com", "acme", "beta", 12)
        app.pr_diff_open = True
        app.diff_view = {"pr": app.pr_choice, "lines": []}
        text = self.text(app)
        self.assertIn("#9", text)
        self.assertNotIn("#12", text)
        self.assertNotIn("Beta cleanup", text)
        self.assertEqual(app.pr_choice[-1], 9)
        self.assertFalse(app.pr_diff_open)


class ReviewPageTests(ReviewFixture):
    def test_the_page_shows_the_selected_prs_facts_and_its_open_conversation(self):
        # Break: the page shows invented or another PR's facts, drops the check progress or
        # open-comment count, the excerpt is not the commented lines, or resolved comments are
        # not folded away.
        app = self.app()
        text = self.text(app)
        lines = text.splitlines()
        self.assertIn("[ #7 \u00b7 Sidebar status ]", text)
        self.assertNotIn("#12", text)
        self.assertNotIn("Beta cleanup", text)
        self.assertIn("[ GitHub \u2197 ]  [ PR actions \u25be ]", text)
        self.assertIn("[ \u25cb Open ]   fm/other \u2192 main   2 files \u00b7 +28 -9", text)
        self.assertIn("Checks 2/3 passing \u00b7 1 pending", text)
        self.assertIn("\U0001f4ac 2 open comments", text)
        self.assertIn("Files 2   Comments 3   Checks", text)
        heading = next(line for line in lines if "Open comments" in line)
        self.assertTrue(heading.rstrip().endswith("1 / 2"), heading)
        card = next(index for index, line in enumerate(lines) if "reviewer" in line and "[selected]" in line)
        self.assertIn("12 min ago", lines[card])
        self.assertIn("app/sidebar.py:39-40", lines[card + 1])
        self.assertRegex(lines[card + 2], r"39\s+\+\s+active = any\(agent\.working")
        self.assertRegex(lines[card + 3], r"40\s+\+\s+return 'working'")
        self.assertIn("What should the status be", lines[card + 4])
        self.assertIn("Changes requested", lines[card + 5])
        self.assertIn("1 reply \u00b7 latest alice 5 min ago", lines[card + 6])
        self.assertIn("[ Add to FirstMate ]   Resolve conversation", lines[card + 7])
        self.assertIn("\u25b8 Resolved comments (1)", text)
        self.assertNotIn("RESOLVED_TEXT", text)
        self.assertNotIn("counts = {}", text)

    def test_unknown_and_failed_facts_are_said_never_guessed(self):
        # Break: a missing count reads as 0, a failed thread read reads as "no open comments",
        # a missing timestamp or code context is invented.
        self.views[7] = {"number": 7, "title": "Sidebar status", "state": "OPEN", "headRefName": "fm/other"}
        self.threads_fail = True
        app = self.app()
        text = self.text(app)
        self.assertIn("? files", text)
        self.assertIn("fm/other \u2192 ?", text)
        self.assertIn("? checks unknown", text)
        self.assertIn("comments unavailable: gh failed", text)
        self.assertNotIn("no open comments", text)
        self.assertNotIn("0 open", text)
        self.assertIn("Files   Comments   Checks", text)
        self.threads_fail = False
        self.threads[7][1]["comments"]["nodes"][0]["createdAt"] = None
        app.handle_key("r")
        app.handle_key("j")
        lines = self.text(app).splitlines()
        card = next(index for index, line in enumerate(lines) if "[selected]" in line)
        self.assertEqual(lines[card].split(), ["\u2503", "unknown", "author", "[selected]"])
        self.assertIn("code context unavailable", lines[card + 2])
        self.assertIn("Resolve conversation (not permitted)", self.text(app))

    def test_a_failed_refresh_keeps_the_last_good_data_and_says_it_is_stale(self):
        # Break: one failed gh read blanks the page, or the old data is shown with no sign of failure.
        clock = [NOW]
        app = self.app(wall=lambda: clock[0])
        self.assertIn("Sidebar status", self.text(app))
        self.assertNotIn("refresh failed", self.text(app))
        self.view_fail = True
        self.threads_fail = True
        clock[0] = NOW + 300
        app.handle_key("r")
        after = self.text(app)
        self.assertIn("Sidebar status", after)
        self.assertIn("refresh failed \u00b7 showing data from 5 min ago", after)
        self.assertIn("Resolve conversation", after)
        self.assertNotIn("PR unavailable", after)
        self.assertNotIn("comments unavailable", after)
        self.view_fail = False
        self.threads_fail = False
        app.handle_key("r")
        self.assertNotIn("refresh failed", self.text(app))

    def test_hostile_pr_and_comment_text_is_inert(self):
        # Break: a review comment or title writes terminal escapes into the pane.
        hostile = "bad\x1b]0;PWN\x07\x1b[2J\u202e text"
        self.views[7]["title"] = hostile
        self.threads[7][0]["comments"]["nodes"][0]["body"] = hostile
        self.threads[7][0]["path"] = "app/\x1b[31mside.py"
        app = self.app()
        frame = app.render()
        self.assertNotIn("\x1b]", frame)
        self.assertNotIn("\x1b[2J", frame)
        self.assertNotIn("PWN", frame)
        self.assertIn("bad<bidi> text", strip_sgr(frame))

    def test_comments_sub_tab_is_navigable_by_keys_and_mouse(self):
        # Break: j/k, Enter or clicks do not move the selection, expand a conversation or fold
        # the resolved group, or a click lands on the wrong conversation.
        app = self.app()
        app.handle_key("enter")
        self.assertIn("Good point, will change.", self.text(app))
        app.handle_key("j")
        self.assertEqual(app.pr_thread_id, "PRRT_two")
        self.assertTrue(next(line for line in self.text(app).splitlines() if "Open comments" in line).rstrip().endswith("2 / 2"))
        app.handle_key("j")
        app.handle_key("enter")
        self.assertIn("RESOLVED_TEXT", self.text(app))
        self.assertIn("\u25be Resolved comments (1)", self.text(app))
        self.click(app, "Resolved comments", "Resolved")
        self.assertNotIn("RESOLVED_TEXT", self.text(app))
        self.click(app, "app/sidebar.py:39-40", "What")
        self.assertEqual(app.pr_thread_id, "PRRT_one")
        app.handle_bytes("\x1b[<64;5;20M")
        self.assertEqual(app.pr_thread_id, "PRRT_one")
        app.handle_bytes("\x1b[<65;5;20M")
        self.assertEqual(app.pr_thread_id, "PRRT_two")

    def test_files_and_checks_sub_tabs_show_the_prs_files_and_checks(self):
        # Break: sub-tabs are a static strip, the file diff shows another file, or a check
        # state is lost.
        app = self.app()
        self.click(app, "Files 2   Comments 3", "Files")
        text = self.text(app)
        self.assertIn("M  app/sidebar.py", text)
        self.assertIn("D  app/old.py", text)
        app.handle_key("j")
        app.handle_key("enter")
        self.assertTrue(app.pr_diff_open)
        view = app.diff_view
        self.assertEqual(app._line_path(view, view["cursor"]), "app/old.py")
        self.assertIn("OLD_FILE_LINE", self.text(app))
        app.handle_key("esc")
        app.handle_key("tab")
        app.handle_key("tab")
        self.assertEqual(app.pr_section, "checks")
        text = self.text(app)
        self.assertRegex(text, r"\u2713 passing\s+test\s+ci")
        self.assertRegex(text, r"\u25cb pending\s+e2e\s+ci")
        app.handle_key("tab")
        self.assertEqual(app.pr_section, "files")
        app.handle_key("backtab")
        app.handle_key("backtab")
        self.assertEqual(app.pr_section, "comments")

    def test_the_page_does_not_open_a_project_wide_pull_request_list(self):
        # Break: the selector opens every project's pull requests, or choosing one retargets the agent.
        app = self.app()
        target = dict(app.target)
        self.click(app, "[ #7", "#7")
        text = self.text(app)
        self.assertNotIn("#12", text)
        self.assertNotIn("Beta cleanup", text)
        self.assertNotIn("\u25be beta", text)
        self.assertEqual(app.target, target)
        self.assertIn("alpha / alpha-task", text.splitlines()[1])
        lists = [call for call in self.gh_calls() if "list" in call["argv"]]
        repos = {call["argv"][call["argv"].index("-R") + 1] for call in lists}
        self.assertEqual(repos, {"github.com/acme/alpha"})

    def test_the_actions_menu_reuses_the_existing_guarded_pr_actions(self):
        # Break: a menu item or the GitHub button acts without its existing confirm.
        app = self.app()
        self.click(app, "[ GitHub", "GitHub")
        self.assertEqual(app.screen, "confirm-open")
        self.assertIn("https://github.com/acme/alpha/pull/7", self.text(app))
        app.handle_key("n")
        self.assertEqual(self.transport.browser, [])
        self.click(app, "[ PR actions", "PR actions")
        self.click(app, "Comment on GitHub", "Comment")
        self.assertEqual(app.screen, "confirm-comment")
        app.handle_key("n")
        self.assertFalse(any("comment" in call["argv"] for call in self.gh_calls()))

    def test_a_narrow_pane_keeps_the_page_inside_its_width(self):
        # Break: the review page overflows a narrow pane or drops its controls.
        app = self.app()
        app.cols, app.rows = 50, 24
        frame = self.text(app)
        self.assertTrue(all(len(line) <= 50 for line in frame.splitlines()), frame)
        self.assertIn("[ \u2197 ]  [ \u22ef ]", frame)
        self.assertIn("[ Add to FirstMate ]", frame)


class AddToFirstMateTests(ReviewFixture):
    def test_the_message_carries_the_exact_conversation_and_reaches_firstmates_box_only_after_the_preview(self):
        # Break: Add to FirstMate fills the box without a preview, loses the PR, thread, lines
        # or comments it was opened on, submits or goes to the inbox, or the preview and what
        # lands in FirstMate's message box differ.
        app = self.app()
        self.click(app, "[ Add to FirstMate ]", "Add")
        self.assertEqual(app.focus, "composer")
        for char in "decide this":
            app.handle_key(char)
        self.assertIn("#7 app/sidebar.py:39-40", self.text(app))
        self.assertIn("1 comment added", self.text(app))
        self.assertIn("decide this", self.text(app))
        app.handle_key("enter")
        self.assertEqual(app.screen, "confirm-send")
        preview = self.text(app)
        self.assertEqual(self.inbox_calls(), [])
        body = app.pending["body"]
        self.assertIn("decide this", body)
        self.assertIn("[1] pr conversation github.com/acme/alpha#7  app/sidebar.py:39-40  unresolved  thread PRRT_one  HEAD 0123456789ab", body)
        self.assertIn("https://github.com/acme/alpha/pull/7", body)
        self.assertIn("```diff\n+    active = any(agent.working for agent in agents)\n+    return 'working' if active else 'idle'\n```", body)
        self.assertIn(f"@reviewer {iso(720)} (changes requested):\nWhat should the status be", body)
        self.assertIn(f"@alice {iso(300)} (commented):\nGood point, will change.", body)
        for line in body.splitlines():
            self.assertIn(line[:60], preview)
        app.handle_key("y")
        self.assertEqual(self.main.received, [])
        self.assertEqual(self.main.submitted, [])
        self.assertEqual(len(self.inbox_calls()), 1)
        self.assertIn(body.rstrip("\n"), self.inbox_calls()[0]["stdin"])
        self.assertIn("queued for FirstMate; not processed yet", self.text(app))

    def test_a_conversation_that_changed_is_flagged_on_the_preview(self):
        # Break: the preview hides that the conversation got a new reply or was resolved
        # after it was added.
        app = self.app()
        app.handle_key("m")
        app.handle_key("x")
        app.handle_key("esc")
        self.threads[7][0]["comments"]["nodes"].append(comment("C9", "bob", 10, "one more thing"))
        self.threads[7][0]["comments"]["totalCount"] = 3
        self.threads[7][0]["isResolved"] = True
        app.handle_key("m")
        app.handle_key("enter")
        warnings = app.pending["warnings"]
        self.assertIn("attachment 1: the conversation is now resolved", warnings)
        self.assertTrue(any("changed since it was added (now 3 comments)" in warning for warning in warnings), warnings)
        self.assertNotIn("one more thing", app.pending["body"])

    def test_without_firstmate_the_button_opens_nothing(self):
        # Break: Add to FirstMate opens an editor that can never be sent.
        self.config = Config(socket_path=self.config.socket_path, project_root=str(self.projects))
        app = self.app()
        app.handle_key("m")
        self.assertNotEqual(app.focus, "composer")
        self.assertNotEqual(app.screen, "confirm-send")

    def test_the_agents_field_and_the_conversation_message_stay_on_their_own_tabs(self):
        # Break: the Agents message field stays focused after switching tabs and swallows the
        # PR page's keys, Tab or m on the PR page open the Agents field, or the agent's unsent
        # field text leaks into the conversation message (or is lost by it).
        self.threads[9] = [thread("PRRT_nine", [comment("C9", "reviewer", 60, "Branch question?")])]
        app = self.app()
        app.handle_key("1")
        self.select_line(app, "Alpha task")
        app.handle_key("tab")
        self.assertEqual(app.focus, "composer")
        for char in "agent field text":
            app.handle_key(char)
        self.click(app, "Review", "Review")
        self.assertEqual((app.tab, app.review_source), ("review", "pr"))
        self.assertNotEqual(app.focus, "composer")
        self.assertNotIn("agent field text", self.text(app))
        app.handle_key("tab")
        self.assertEqual(app.pr_section, "checks")
        app.handle_key("backtab")
        app.handle_key("m")
        text = self.text(app)
        self.assertIn("#7 app/sidebar.py:39-40", text)
        self.assertNotIn("agent field text", text)
        app.handle_key("esc")
        app.handle_key("1")
        self.assertIn("agent field text", self.text(app))


class ResolveTests(ReviewFixture):
    def test_resolve_confirms_the_exact_thread_and_reports_githubs_answer(self):
        # Break: resolve runs without a confirm, on a key other than y, on another thread, or
        # claims resolved without GitHub saying so.
        app = self.app()
        self.click(app, "[ Add to FirstMate ]", "Resolve conversation")
        self.assertEqual(app.screen, "confirm-resolve")
        text = self.text(app)
        self.assertIn("Resolve conversation on github.com/acme/alpha#7 as @octocat", text)
        self.assertIn("app/sidebar.py:39-40", text)
        self.assertIn("started by @reviewer 12 min ago \u00b7 2 comments \u00b7 latest @alice 5 min ago", text)
        self.assertIn("thread PRRT_one", text)
        app.handle_key("n")
        self.assertEqual(self.mutations(), [])
        app.handle_key("R")
        app.handle_key("y")
        mutations = self.mutations()
        self.assertEqual(len(mutations), 1)
        self.assertIn("threadId=PRRT_one", mutations[0]["argv"])
        self.assertIn("--hostname", mutations[0]["argv"])
        self.assertIn("resolve outcome: resolved", self.text(app))
        self.assertIn("\u25b8 Resolved comments (2)", self.text(app))

    def test_an_unconfirmed_or_failed_resolve_is_unknown_and_never_retried(self):
        # Break: a timeout or an answer without isResolved is shown as resolved, or retried.
        self.resolve_answer = CmdResult(0, json.dumps({"data": {"resolveReviewThread": {"thread": {"id": "PRRT_one", "isResolved": False}}}}))
        app = self.app()
        app.handle_key("R")
        app.handle_key("y")
        self.assertIn("resolve outcome: unknown", self.text(app))
        self.resolve_answer = CmdResult(0, "", "", timed_out=True)
        app.handle_key("R")
        app.handle_key("y")
        self.assertIn("resolve outcome: unknown", self.text(app))
        app.handle_key("r")
        self.assertEqual(len(self.mutations()), 2)
        self.assertIn("\u25b8 Resolved comments (1)", self.text(app))

    def test_a_thread_that_changed_or_cannot_be_resolved_is_not_resolved(self):
        # Break: y resolves a conversation that got a reply after the preview, or the
        # button is armed for a thread GitHub says this account cannot resolve.
        app = self.app()
        app.handle_key("R")
        self.threads[7][0]["comments"]["nodes"].append(comment("C9", "bob", 10, "wait"))
        self.threads[7][0]["comments"]["totalCount"] = 3
        app.handle_key("y")
        self.assertEqual(self.mutations(), [])
        self.assertEqual(app.screen, "confirm-resolve")
        text = self.text(app)
        self.assertIn("the conversation changed since the preview", text)
        self.assertIn("3 comments \u00b7 latest @bob just now", text)
        app.handle_key("n")
        app.handle_key("j")
        app.handle_key("R")
        self.assertNotEqual(app.screen, "confirm-resolve")
        self.assertIn("cannot resolve it", self.text(app))
        self.threads[7][0]["isResolved"] = True
        app.handle_key("k")
        app.handle_key("R")
        self.assertNotEqual(app.screen, "confirm-resolve")
        self.assertIn("already shows it resolved", self.text(app))
        self.assertEqual(self.mutations(), [])
