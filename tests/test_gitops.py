"""Guarded Git actions on fixture repositories and a local bare remote only."""

import os
import stat
import sys
import unittest

from firstpane.gitops import execute, prepare
from firstpane.transport import CmdResult, LiveTransport
from tests.gitfixture import GitFixture


class DetachTests(unittest.TestCase):
    def test_detached_child_has_its_own_session_and_no_pane_stdin(self):
        # Break: a write child shares the pane's session/stdin, so ssh or a hook can prompt
        # on the pane tty while Firstpane holds it in cbreak mode.
        probe = "import os,sys; print(os.getsid(0)); print(sys.stdin.read() == '')"
        result = LiveTransport(None).run([sys.executable, "-c", probe], timeout=10, detach=True)
        sid, empty = result.stdout.split()
        self.assertNotEqual(int(sid), os.getsid(0))
        self.assertEqual(empty, "True")
from tests.gitfixture import GitFixture


class GitOpsFixture(unittest.TestCase):
    def setUp(self):
        self.fx = GitFixture()
        self.calls = []
        live = LiveTransport(None)

        def run(argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return live.run(argv, **kwargs)

        self.run_git = run
        self.ws = self.fx.worktree("wt", "fm/task")
        self.msg = self.fx.base / "message.txt"

    def tearDown(self):
        self.fx.cleanup()

    def prepare(self, action, **kwargs):
        kwargs.setdefault("preferred_remote", "origin")
        return prepare(action, self.run_git, self.fx.env, str(self.ws), **kwargs)

    def execute(self, plan, **kwargs):
        return execute(plan, self.run_git, self.fx.env, str(self.ws), **kwargs)

    def git(self, *args, check=True):
        return self.fx.git(self.ws, *args, check=check)

    def writes(self):
        return [argv for argv, _ in self.calls if any(w in argv for w in ("add", "commit", "push", "pull", "fetch", "restore"))]


class StageTests(GitOpsFixture):
    def test_stage_preview_names_target_and_execute_stages_exactly_that_path(self):
        # Break: staging runs in another checkout, stages more than the selected path, or the
        # preview hides the branch/HEAD it will act on.
        self.fx.write("a.txt", "a\n", root=self.ws)
        self.fx.write("b.txt", "b\n", root=self.ws)
        plan = self.prepare("stage", paths=["a.txt"])
        self.assertEqual(plan.refusal, "")
        preview = "\n".join(plan.preview)
        self.assertIn(str(self.ws), preview)
        self.assertIn("fm/task", preview)
        self.assertIn(self.fx.head(self.ws)[:12], preview)
        self.assertIn("git add -- a.txt", preview)
        self.assertEqual(self.writes(), [])
        outcome = self.execute(plan)
        self.assertEqual(outcome.kind, "done", outcome.detail)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "a.txt")

    def test_stage_refuses_when_the_path_is_gone_after_the_preview(self):
        # Break: a confirm built on an old status stages whatever is there now.
        self.fx.write("a.txt", "a\n", root=self.ws)
        plan = self.prepare("stage", paths=["a.txt"])
        os.remove(self.ws / "a.txt")
        outcome = self.execute(plan)
        self.assertEqual(outcome.kind, "refused")
        self.assertIn("a.txt", outcome.detail)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_glob_characters_in_a_path_stage_and_unstage_only_that_literal_file(self):
        # Break: the status path is a pathspec, so app/[slug]/page.tsx also matches
        # app/s/page.tsx and a sibling file is staged or unstaged without being previewed.
        for rel in ("app/[slug]/page.tsx", "app/s/page.tsx"):
            self.fx.write(rel, "old\n", root=self.ws)
            self.git("add", rel)
        self.git("commit", "-q", "-m", "pages")
        self.fx.write("app/[slug]/page.tsx", "literal\n", root=self.ws)
        self.fx.write("app/s/page.tsx", "sibling\n", root=self.ws)
        plan = self.prepare("stage", paths=["app/[slug]/page.tsx"])
        self.assertEqual(self.execute(plan).kind, "done")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "app/[slug]/page.tsx")
        self.git("add", "app/s/page.tsx")
        plan = self.prepare("unstage", paths=["app/[slug]/page.tsx"])
        self.assertEqual(self.execute(plan).kind, "done")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "app/s/page.tsx")

    def test_unstage_restores_only_the_index(self):
        # Break: unstage discards the worktree change (checkout/reset) instead of only the index.
        self.fx.write("README.md", "changed\n", root=self.ws)
        self.git("add", "README.md")
        plan = self.prepare("unstage", paths=["README.md"])
        self.assertEqual(self.execute(plan).kind, "done")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertEqual((self.ws / "README.md").read_text(), "changed\n")


class CommitTests(GitOpsFixture):
    def test_commit_refuses_when_the_index_changed_after_the_preview(self):
        # Break: the user confirms a commit of one file and another file staged meanwhile
        # (by the agent) is committed too.
        self.fx.write("a.txt", "a\n", root=self.ws)
        self.git("add", "a.txt")
        self.msg.write_text("Add a\n")
        plan = self.prepare("commit", message_path=str(self.msg))
        self.assertIn("a.txt", "\n".join(plan.preview))
        self.assertIn("Add a", "\n".join(plan.preview))
        head = self.fx.head(self.ws)
        self.fx.write("b.txt", "b\n", root=self.ws)
        self.git("add", "b.txt")
        outcome = self.execute(plan)
        self.assertEqual(outcome.kind, "refused")
        self.assertIn("index changed", outcome.detail)
        self.assertEqual(self.fx.head(self.ws), head)

    def test_commit_keeps_hooks_and_a_rejecting_hook_leaves_no_commit(self):
        # Break: Firstpane bypasses hooks (--no-verify), or reports success after a hook refused.
        hooks = self.fx.base / "hooks"
        hooks.mkdir()
        marker = self.fx.base / "hook-ran"
        hook = hooks / "commit-msg"
        hook.write_text(f"#!/bin/sh\necho ran > '{marker}'\ngrep -q ALLOW \"$1\"\n")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
        self.fx.git(self.fx.primary, "config", "core.hooksPath", str(hooks))
        self.fx.write("a.txt", "a\n", root=self.ws)
        self.git("add", "a.txt")
        head = self.fx.head(self.ws)
        self.msg.write_text("refused message\n")
        outcome = self.execute(self.prepare("commit", message_path=str(self.msg)))
        self.assertEqual(outcome.kind, "failed")
        self.assertTrue(marker.exists())
        self.assertEqual(self.fx.head(self.ws), head)
        self.msg.write_text("ALLOW message\n")
        outcome = self.execute(self.prepare("commit", message_path=str(self.msg)))
        self.assertEqual(outcome.kind, "done", outcome.detail)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "ALLOW message")

    def test_commit_with_nothing_staged_or_detached_is_refused_at_preview(self):
        # Break: an empty commit is attempted, or a commit lands on a detached HEAD.
        self.msg.write_text("m\n")
        plan = self.prepare("commit", message_path=str(self.msg))
        self.assertIn("nothing staged", plan.refusal)
        self.fx.write("a.txt", "a\n", root=self.ws)
        self.git("add", "a.txt")
        self.git("checkout", "-q", "--detach")
        plan = self.prepare("commit", message_path=str(self.msg))
        self.assertIn("detached", plan.refusal)


class RemoteTests(GitOpsFixture):
    def other_commit(self, name="remote.txt"):
        other = self.fx.clone("other-" + name)
        exists = self.fx.git(other, "rev-parse", "--verify", "-q", "origin/fm/task", check=False)
        self.fx.git(other, "checkout", "-q", "-b", "fm/task", *(["origin/fm/task"] if exists else []))
        self.fx.write(name, "r\n", root=other)
        self.fx.git(other, "add", name)
        self.fx.git(other, "commit", "-q", "-m", "remote " + name)
        self.fx.git(other, "push", "-q", "origin", "fm/task")
        return other

    def test_push_without_upstream_is_a_separate_set_upstream_push(self):
        # Break: a branch without upstream is pushed to a guessed ref, or silently gets one.
        plan = self.prepare("push")
        self.assertEqual(plan.refusal, "")
        preview = "\n".join(plan.preview)
        self.assertIn("sets upstream origin/fm/task", preview)
        self.assertIn(str(self.fx.remote), preview)
        outcome = self.execute(plan)
        self.assertEqual(outcome.kind, "done", outcome.detail)
        self.assertEqual(self.fx.git(self.fx.remote, "rev-parse", "fm/task"), self.fx.head(self.ws))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{u}"), "origin/fm/task")

    def test_push_refuses_head_drift_and_never_forces_a_diverged_branch(self):
        # Break: a push confirmed for one HEAD sends a newer one, or a rejected push is forced.
        self.execute(self.prepare("push"))
        self.fx.write("mine.txt", "m\n", root=self.ws)
        self.git("add", "mine.txt")
        self.git("commit", "-q", "-m", "mine")
        plan = self.prepare("push")
        self.assertIn("1 ahead", "\n".join(plan.preview))
        self.git("commit", "-q", "--allow-empty", "-m", "agent moved head")
        self.assertEqual(self.execute(plan).kind, "refused")
        self.other_commit()
        remote_head = self.fx.git(self.fx.remote, "rev-parse", "fm/task")
        outcome = self.execute(self.prepare("push"))
        self.assertEqual(outcome.kind, "failed")
        self.assertEqual(self.fx.git(self.fx.remote, "rev-parse", "fm/task"), remote_head)

    def test_credentials_in_the_remote_url_are_not_shown(self):
        # Break: the confirm screen prints a token embedded in the remote URL.
        self.fx.git(self.fx.primary, "remote", "set-url", "origin", "https://bob:s3cr3t@example.invalid/acme/x.git")
        preview = "\n".join(self.prepare("fetch").preview)
        self.assertIn("https://example.invalid/acme/x.git", preview)
        self.assertNotIn("s3cr3t", preview)
        self.assertNotIn("bob", preview)

    def test_fetch_refuses_when_the_remote_url_changed_after_the_preview(self):
        # Break: a fetch confirmed against one URL contacts another.
        plan = self.prepare("fetch")
        self.assertIn("git fetch origin", "\n".join(plan.preview))
        self.fx.git(self.fx.primary, "remote", "set-url", "origin", str(self.fx.base / "elsewhere.git"))
        outcome = self.execute(plan)
        self.assertEqual(outcome.kind, "refused")
        self.assertIn("remote", outcome.detail)

    def test_fetch_updates_tracking_refs_only(self):
        # Break: fetch also merges or moves the local branch.
        self.execute(self.prepare("push"))
        head = self.fx.head(self.ws)
        self.other_commit()
        outcome = self.execute(self.prepare("fetch"))
        self.assertEqual(outcome.kind, "done", outcome.detail)
        self.assertEqual(self.fx.head(self.ws), head)
        self.assertEqual(self.git("rev-list", "--count", "HEAD..@{u}"), "1")

    def test_pull_is_fast_forward_only_and_refuses_dirty_or_busy_worktrees(self):
        # Break: pull runs over uncommitted work or under a working agent, or creates a merge.
        self.execute(self.prepare("push"))
        self.other_commit()
        self.fx.write("README.md", "dirty\n", root=self.ws)
        self.assertIn("not clean", self.prepare("pull").refusal)
        self.git("checkout", "--", "README.md")
        busy = self.prepare("pull", busy_agents=["other-task working"])
        self.assertIn("other-task working", busy.refusal)
        plan = self.prepare("pull")
        self.assertEqual(plan.refusal, "")
        self.assertIn("--ff-only", "\n".join(plan.preview))
        self.assertEqual(self.execute(plan).kind, "done")
        self.assertEqual(self.git("rev-list", "--count", "@{u}..HEAD"), "0")
        self.assertTrue((self.ws / "remote.txt").exists())
        self.git("commit", "-q", "--allow-empty", "-m", "local")
        self.other_commit("second.txt")
        head = self.fx.head(self.ws)
        outcome = self.execute(self.prepare("pull"))
        self.assertEqual(outcome.kind, "failed")
        self.assertEqual(self.fx.head(self.ws), head)
        self.assertEqual(self.git("rev-list", "--merges", "--count", "HEAD"), "0")

    def test_pull_refuses_a_busy_agent_that_appeared_after_the_preview(self):
        # Break: the busy-agent check runs only at preview time.
        self.execute(self.prepare("push"))
        plan = self.prepare("pull")
        outcome = self.execute(plan, busy_agents=["agent-x working"])
        self.assertEqual(outcome.kind, "refused")


class OutcomeTests(GitOpsFixture):
    def test_timeout_is_unknown_and_not_retried(self):
        # Break: a push that timed out is reported as failed and retried, possibly pushing twice.
        plan = self.prepare("push")
        real = self.run_git
        pushes = []

        def hanging(argv, **kwargs):
            if "push" in argv:
                pushes.append(argv)
                return CmdResult(124, "", "", timed_out=True)
            return real(argv, **kwargs)

        outcome = execute(plan, hanging, self.fx.env, str(self.ws))
        self.assertEqual(outcome.kind, "unknown")
        self.assertEqual(len(pushes), 1)

    def test_writes_run_detached_from_the_pane_terminal(self):
        # Break: a credential or hook prompt reads from the pane tty and corrupts raw mode.
        self.fx.write("a.txt", "a\n", root=self.ws)
        self.execute(self.prepare("stage", paths=["a.txt"]))
        adds = [kwargs for argv, kwargs in self.calls if "add" in argv]
        self.assertEqual(len(adds), 1)
        self.assertTrue(adds[0].get("detach"))
        self.assertEqual(adds[0]["env"].get("GIT_TERMINAL_PROMPT"), "0")

    def test_a_workspace_that_moved_is_refused(self):
        # Break: a plan prepared for one worktree executes in whatever the agent maps to now.
        self.fx.write("a.txt", "a\n", root=self.ws)
        plan = self.prepare("stage", paths=["a.txt"])
        outcome = execute(plan, self.run_git, self.fx.env, str(self.fx.primary))
        self.assertEqual(outcome.kind, "refused")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")


if __name__ == "__main__":
    unittest.main()
