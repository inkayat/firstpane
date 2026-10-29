"""Commit revisions, PR facts, and the requirement that a review names a trusted skill."""

import unittest

from firstpane.pr_view import log_argv, parse_view
from firstpane.review import mark_findings, review_scope, selected_skills
from firstpane.workspace import commit_diff_argv, commit_list_argv, parse_commit_list
from tests.gitfixture import GitFixture


class CommitRevisionTests(unittest.TestCase):
    def test_list_excludes_the_remote_default_and_caps_the_log(self):
        # Break: the revision menu lists commits already on the remote default branch.
        argv = commit_list_argv("/repo", remote="origin", remote_head=True)
        self.assertIn("HEAD", argv)
        self.assertIn("--not", argv)
        self.assertIn("origin/HEAD", argv)
        self.assertIn("50", argv)
        self.assertNotIn("--remotes", argv)
        fallback = commit_list_argv("/repo", remote="origin", remote_head=False)
        self.assertIn("--remotes", fallback)
        self.assertNotIn("origin/HEAD", fallback)

    def test_a_worktree_only_commit_is_listed_and_its_diff_argv_is_safe(self):
        # Break: a local commit is omitted, or the diff argv can take a ref that is not an oid.
        fx = GitFixture()
        self.addCleanup(fx.cleanup)
        fx.write("src/app.py", "one\n", root=fx.primary)
        fx.git(fx.primary, "checkout", "-q", "-b", "topic")
        fx.git(fx.primary, "add", "src/app.py")
        fx.git(fx.primary, "commit", "-q", "-m", "topic only")
        oid = fx.head()
        self.assertTrue(oid)
        listed = parse_commit_list(f"{oid}\ttopic only\nnot-an-oid\tnope\n")
        self.assertEqual(listed, [{"oid": oid, "subject": "topic only"}])
        diff = commit_diff_argv(str(fx.primary), oid)
        self.assertIn("--no-ext-diff", diff)
        self.assertIn(oid, diff)
        with self.assertRaises(ValueError):
            commit_diff_argv(str(fx.primary), "HEAD")


class PullRequestFactTests(unittest.TestCase):
    def test_review_decision_commits_reviews_and_comments_are_kept(self):
        # Break: gh returned reviewDecision or commits and the page still says they are unknown.
        view = parse_view({
            "title": "Sidebar",
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": "a" * 40,
            "reviewDecision": "CHANGES_REQUESTED",
            "commits": [{"oid": "b" * 40, "messageHeadline": "fix badge"}],
            "reviews": [{"author": {"login": "ada"}, "state": "CHANGES_REQUESTED", "body": "rename it"}],
            "comments": [{"author": {"login": "bea"}, "body": "see the log"}],
            "statusCheckRollup": [{
                "name": "ci",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://github.com/acme/app/actions/runs/4242",
            }],
        })
        self.assertEqual(view["review_decision"], "changes")
        self.assertEqual(view["commits"], [{"oid": "b" * 40, "subject": "fix badge"}])
        self.assertEqual(view["reviews"][0]["author"], "ada")
        self.assertEqual(view["issue_comments"][0]["author"], "bea")
        self.assertEqual(log_argv(view["checks"][0]["details_url"]), ["gh", "run", "view", "4242", "--log-failed"])
        self.assertIsNone(log_argv("https://example.test/not-actions"))

    def test_a_missing_field_stays_unknown(self):
        # Break: a gh that omitted commits is treated as an empty commit list.
        view = parse_view({"title": "Sidebar", "state": "OPEN"})
        self.assertIsNone(view["commits"])
        self.assertIsNone(view["review_decision"])


class ScopeTests(unittest.TestCase):
    def test_workspace_commit_and_pr_scopes_stay_distinct(self):
        # Break: a commit review is sent as uncommitted, or a PR commit is sent as the whole PR.
        self.assertEqual(review_scope(source="workspace", mode="staged", head="a" * 40), ("uncommitted", {"mode": "staged", "head": "a" * 40}))
        self.assertEqual(review_scope(source="workspace", head="a" * 40, commit="b" * 40, parent="a" * 40)[0], "commit")
        pr = {"host": "github.com", "owner": "acme", "repo": "app", "number": 7}
        scope, revision = review_scope(source="pr", head="c" * 40, pr=pr)
        self.assertEqual(scope, "pr")
        self.assertEqual(revision["repo"], "acme/app")
        scope, revision = review_scope(source="pr", head="c" * 40, pr=pr, pr_commit="d" * 40)
        self.assertEqual((scope, revision["commit"]), ("pr_commit", "d" * 40))

    def test_stale_label_is_attached_and_the_finding_is_kept(self):
        # Break: a moved HEAD deletes the finding instead of marking it stale.
        marked = mark_findings([{"id": "one", "title": "rename"}], scope="uncommitted", head_moved=True)
        self.assertEqual(marked[0]["stale"], "stale: HEAD moved")
        self.assertEqual(marked[0]["title"], "rename")


class SkillSelectionTests(unittest.TestCase):
    def test_a_run_without_a_trusted_selected_skill_is_refused(self):
        # Break: Run review starts with no skill, or a skill inside the repository is accepted.
        self.assertEqual(selected_skills([], [{"path": "/skills/careful", "usable": True}]), [])
        picked = selected_skills(["/skills/careful"], [
            {"path": "/skills/careful", "usable": True},
            {"path": "/repo/planted", "usable": False},
        ])
        self.assertEqual(picked, ["/skills/careful"])
        self.assertEqual(selected_skills(["/repo/planted"], [{"path": "/repo/planted", "usable": False}]), [])
