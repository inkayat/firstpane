"""Workspace identity and read-only Git state against real fixture repositories."""

import os
import unittest

from firstpane.transport import LiveTransport
from firstpane.workspace import (
    content_id,
    parse_diff,
    read_diff,
    read_status,
    resolve_workspace,
)
from tests.gitfixture import GitFixture


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self.fx = GitFixture()
        self.run_git = LiveTransport(None).run
        self.project_key = str(self.fx.primary)

    def tearDown(self):
        self.fx.cleanup()


class StatusTests(WorkspaceFixture):
    def test_groups_and_count_follow_index_and_worktree(self):
        # Break: a file both staged and modified again is counted twice, a rename loses its
        # old path, or untracked files are missing from the changed count.
        fx = self.fx
        fx.write("README.md", "one\ntwo\nthree\nfour\n")
        fx.git(fx.primary, "add", "README.md")
        fx.write("README.md", "one\ntwo\nthree\nfour\nfive\n")
        fx.write("notes.txt", "n\n")
        fx.git(fx.primary, "add", "notes.txt")
        fx.git(fx.primary, "mv", "notes.txt", "renamed.txt")
        fx.write("scratch.py", "print(1)\n")
        status, error = read_status(self.run_git, str(fx.primary), fx.env)
        self.assertEqual(error, "")
        staged = [(e.code, e.path, e.orig) for e in status.entries if e.group == "staged"]
        unstaged = [(e.code, e.path) for e in status.entries if e.group == "unstaged"]
        untracked = [e.path for e in status.entries if e.group == "untracked"]
        self.assertEqual(sorted(staged), [("A", "renamed.txt", None), ("M", "README.md", None)])
        self.assertEqual(unstaged, [("M", "README.md")])
        self.assertEqual(untracked, ["scratch.py"])
        self.assertEqual(status.changed, 3)
        self.assertEqual(status.head, "main")
        self.assertEqual(status.upstream, "origin/main")
        self.assertEqual((status.ahead, status.behind), (0, 0))

    def test_detached_unborn_and_ahead_are_reported_as_words(self):
        # Break: a detached HEAD is shown as a branch named "(detached)", or ahead/behind is
        # lost so a push preview cannot say what will be sent.
        fx = self.fx
        fx.write("a.txt", "a\n")
        fx.git(fx.primary, "add", "a.txt")
        fx.git(fx.primary, "commit", "-q", "-m", "ahead")
        status, _ = read_status(self.run_git, str(fx.primary), fx.env)
        self.assertEqual((status.ahead, status.behind), (1, 0))
        fx.git(fx.primary, "checkout", "-q", "--detach")
        status, _ = read_status(self.run_git, str(fx.primary), fx.env)
        self.assertTrue(status.detached)
        self.assertIsNone(status.head)
        self.assertEqual(status.oid, fx.head())
        self.assertEqual(status.branch_label(), "detached at " + fx.head()[:7])
        empty = fx.base / "empty"
        fx.git(fx.base, "init", "-q", str(empty))
        status, _ = read_status(self.run_git, str(empty), fx.env)
        self.assertIsNone(status.oid)
        self.assertEqual(status.head, "main")
        self.assertIsNone(status.upstream)
        self.assertEqual(status.branch_label(), "main (no commits, no upstream)")

    def test_polling_does_not_rewrite_the_agents_index(self):
        # Break: the read poll refreshes and rewrites .git/index, racing the agent's own git
        # commands for index.lock.
        fx = self.fx
        index = fx.primary / ".git" / "index"
        before = index.stat().st_mtime_ns
        readme = fx.primary / "README.md"
        os.utime(readme, ns=(readme.stat().st_atime_ns, readme.stat().st_mtime_ns + 5_000_000_000))
        status, _ = read_status(self.run_git, str(fx.primary), fx.env)
        self.assertEqual(status.changed, 0)
        self.assertEqual(index.stat().st_mtime_ns, before)

    def test_not_a_repository_is_an_error_word(self):
        # Break: a plain directory is shown as a clean repository with 0 files.
        plain = self.fx.base / "plain"
        plain.mkdir()
        status, error = read_status(self.run_git, str(plain), self.fx.env)
        self.assertIsNone(status)
        self.assertEqual(error, "not a git repo")


class ResolveTests(WorkspaceFixture):
    def test_recorded_linked_worktree_is_the_workspace(self):
        # Break: the primary checkout (project path) is used instead of the agent's worktree.
        wt = self.fx.worktree("wt-a", "fm/a")
        ws = resolve_workspace(self.run_git, self.fx.env, self.project_key, recorded=str(wt))
        self.assertEqual(ws.state, "ok")
        self.assertEqual(ws.path, str(wt))
        self.assertEqual(ws.provenance, "fm")
        self.assertTrue(ws.writable)

    def test_recorded_worktree_of_another_repository_is_refused_without_fallback(self):
        # Break: a recorded path in a different repository (reused slot) is accepted, or the
        # resolver silently falls back to the Herdr checkout or the primary path.
        other = self.fx.clone("other-clone")
        wt = self.fx.worktree("wt-b", "fm/b")
        ws = resolve_workspace(
            self.run_git, self.fx.env, self.project_key, recorded=str(other), herdr=str(wt)
        )
        self.assertEqual(ws.state, "unavailable")
        self.assertIsNone(ws.path)
        self.assertIn("another repository", ws.reason)

    def test_recorded_subdirectory_is_not_a_workspace_root(self):
        # Break: a path inside a checkout is treated as the checkout and writes run there.
        sub = self.fx.primary / "src"
        sub.mkdir()
        ws = resolve_workspace(self.run_git, self.fx.env, self.project_key, recorded=str(sub))
        self.assertEqual(ws.state, "unavailable")
        self.assertIn("not the worktree root", ws.reason)

    def test_disagreeing_sources_are_ambiguous_and_read_only(self):
        # Break: two different worktrees for one agent are resolved by picking one silently
        # and allowing Git writes there.
        a = self.fx.worktree("wt-c", "fm/c")
        b = self.fx.worktree("wt-d", "fm/d")
        ws = resolve_workspace(self.run_git, self.fx.env, self.project_key, recorded=str(a), herdr=str(b))
        self.assertEqual(ws.state, "ambiguous")
        self.assertEqual(ws.path, str(a))
        self.assertFalse(ws.writable)

    def test_without_recorded_or_herdr_worktree_the_workspace_is_unknown(self):
        # Break: a FirstMate row with no recorded worktree guesses the primary project path.
        ws = resolve_workspace(self.run_git, self.fx.env, self.project_key)
        self.assertEqual(ws.state, "unknown")
        self.assertIsNone(ws.path)

    def test_cwd_inference_is_read_only(self):
        # Break: a workspace inferred from a pane cwd (a guess) accepts Git writes.
        sub = self.fx.primary / "pkg"
        sub.mkdir()
        ws = resolve_workspace(self.run_git, self.fx.env, self.project_key, cwd=str(sub))
        self.assertEqual(ws.state, "ok")
        self.assertEqual(ws.path, str(self.fx.primary))
        self.assertEqual(ws.provenance, "cwd")
        self.assertFalse(ws.writable)


class DiffTests(WorkspaceFixture):
    def test_hunk_lines_carry_old_and_new_numbers(self):
        # Break: selected diff lines are attributed to the wrong line numbers or side.
        self.fx.write("README.md", "one\nTWO\nthree\nfour\n")
        status, _ = read_status(self.run_git, str(self.fx.primary), self.fx.env)
        entry = status.entries[0]
        text, error = read_diff(self.run_git, str(self.fx.primary), entry, self.fx.env)
        self.assertEqual(error, "")
        lines = [(l.kind, l.old, l.new, l.text) for l in parse_diff(text) if l.kind in "+- "]
        self.assertEqual(
            lines,
            [
                (" ", 1, 1, "one"),
                ("-", 2, None, "two"),
                ("+", None, 2, "TWO"),
                (" ", 3, 3, "three"),
                ("+", None, 4, "four"),
            ],
        )

    def test_a_tracked_path_with_glob_characters_diffs_only_that_file(self):
        # Break: the status path is a pathspec, so the diff of app/[slug]/page.tsx also shows
        # the changes of app/s/page.tsx and a range attached from it names the wrong file.
        for rel in ("app/[slug]/page.tsx", "app/s/page.tsx"):
            self.fx.write(rel, "old\n")
            self.fx.git(self.fx.primary, "add", rel)
        self.fx.git(self.fx.primary, "commit", "-q", "-m", "pages")
        self.fx.write("app/[slug]/page.tsx", "literal\n")
        self.fx.write("app/s/page.tsx", "sibling\n")
        status, _ = read_status(self.run_git, str(self.fx.primary), self.fx.env)
        entry = next(e for e in status.entries if e.path == "app/[slug]/page.tsx")
        text, error = read_diff(self.run_git, str(self.fx.primary), entry, self.fx.env)
        self.assertEqual(error, "")
        self.assertEqual([l.text for l in parse_diff(text) if l.kind == "+"], ["literal"])

    def test_untracked_file_diff_is_all_new_lines(self):
        # Break: an untracked file shows no diff (git diff ignores it), so it cannot be attached.
        self.fx.write("new.py", "a = 1\nb = 2\n")
        status, _ = read_status(self.run_git, str(self.fx.primary), self.fx.env)
        entry = next(e for e in status.entries if e.path == "new.py")
        text, error = read_diff(self.run_git, str(self.fx.primary), entry, self.fx.env)
        self.assertEqual(error, "")
        adds = [(l.new, l.text) for l in parse_diff(text) if l.kind == "+"]
        self.assertEqual(adds, [(1, "a = 1"), (2, "b = 2")])

    def test_content_identity_is_the_blob_of_the_shown_side(self):
        # Break: a staged attachment records the worktree blob (or the reverse), so a later
        # edit is not detected as drift.
        fx = self.fx
        fx.write("README.md", "staged\n")
        fx.git(fx.primary, "add", "README.md")
        fx.write("README.md", "worktree\n")
        status, _ = read_status(self.run_git, str(fx.primary), fx.env)
        staged = next(e for e in status.entries if e.group == "staged")
        unstaged = next(e for e in status.entries if e.group == "unstaged")
        index_blob = fx.git(fx.primary, "rev-parse", ":README.md")
        work_blob = fx.git(fx.primary, "hash-object", "README.md")
        self.assertNotEqual(index_blob, work_blob)
        self.assertEqual(content_id(self.run_git, str(fx.primary), staged, fx.env), index_blob)
        self.assertEqual(content_id(self.run_git, str(fx.primary), unstaged, fx.env), work_blob)
        head_blob = fx.git(fx.primary, "rev-parse", "HEAD:README.md")
        self.assertEqual(content_id(self.run_git, str(fx.primary), staged, fx.env, side="old"), head_blob)
        self.assertEqual(content_id(self.run_git, str(fx.primary), unstaged, fx.env, side="old"), index_blob)


if __name__ == "__main__":
    unittest.main()
