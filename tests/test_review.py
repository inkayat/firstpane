"""Manual review command: config, request pin, untrusted output, and one owned run."""

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from firstpane.review import (
    ReviewSupervisor,
    build_request,
    orphan_note,
    parse_review_config,
    revision_key,
    skill_usable,
    stale_label,
    validate_result,
)
PATCH = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,4 @@
 kept
-old
+new
"""


def request(**overrides):
    patch = overrides.pop("patch", PATCH)
    body = build_request(
        run_id="ab" * 16,
        created_at="2026-10-29T10:00:00Z",
        skills=[{"name": "ponytail", "path": "/skills/ponytail", "skill_md_sha256": "a" * 64}],
        repository={"path": "/repo", "remote": "owner/name"},
        scope="uncommitted",
        revision={"mode": "all", "head": "c" * 40},
        files=[{"path": "src/app.py", "old_path": None, "status": "modified", "old_blob": "1" * 40, "new_blob": "2" * 40}],
        patch_text=patch,
    )
    body.update(overrides)
    return body


class ConfigTests(unittest.TestCase):
    def test_missing_or_invalid_command_is_not_a_menu(self):
        # Break: a missing review command still offers Code review, or a string is executed as a shell line.
        self.assertIsNone(parse_review_config({}))
        self.assertIsNone(parse_review_config({"review": {"command": "git diff"}}))
        self.assertIsNone(parse_review_config({"review": {"command": ["", "x"]}}))
        parsed = parse_review_config({"review": {"command": ["/bin/echo", "fixed"], "skills": ["/skills"], "timeout_seconds": 10}})
        self.assertEqual(parsed.command, ("/bin/echo", "fixed"))
        self.assertEqual(parsed.timeout_seconds, 900)

    def test_skill_inside_the_repository_is_unusable(self):
        # Break: a reviewed worktree can supply the skill that reviews it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            outside = root / "skills" / "careful"
            inside = repo / "skills" / "planted"
            for path in (outside, inside):
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text("name: x\n")
            (repo / "SKILL.md").write_text("not a directory skill\n")
            self.assertTrue(skill_usable(outside, repo)[0])
            usable, reason = skill_usable(inside, repo)
            self.assertFalse(usable)
            self.assertIn("repository", reason)
            missing, why = skill_usable(root / "nope", repo)
            self.assertFalse(missing)
            self.assertIn("SKILL.md", why)


class RequestTests(unittest.TestCase):
    def test_revision_key_pins_scope_revision_and_patch_bytes(self):
        # Break: a later edit of the same commit still matches an old review, or key order changes the pin.
        body = request()
        again = revision_key("uncommitted", {"head": "c" * 40, "mode": "all"}, body["patch"]["sha256"])
        self.assertEqual(body["revision_key"], again)
        self.assertEqual(body["patch"]["sha256"], hashlib.sha256(PATCH.encode()).hexdigest())
        moved = revision_key("uncommitted", {"mode": "all", "head": "d" * 40}, body["patch"]["sha256"])
        self.assertNotEqual(body["revision_key"], moved)

    def test_oversize_or_empty_patch_is_refused_before_a_run(self):
        # Break: a truncated patch is sent as if it were the whole change.
        with self.assertRaises(ValueError) as empty:
            request(patch="")
        self.assertIn("nothing to review", str(empty.exception))
        with self.assertRaises(ValueError) as large:
            request(patch="x" * (2 * 1024 * 1024 + 1))
        self.assertIn("too large", str(large.exception))


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.request = request()

    def _raw(self, **finding):
        item = {
            "id": "one",
            "path": "src/app.py",
            "side": "new",
            "start_line": 12,
            "end_line": 12,
            "severity": "low",
            "title": "name the change",
            "body": "the new line has no reason",
            "skill": "ponytail",
        }
        item.update(finding)
        return json.dumps({
            "schema": "firstpane.review.result.v1",
            "run_id": self.request["run_id"],
            "revision_key": self.request["revision_key"],
            "status": "complete",
            "summary": "one finding",
            "findings": [item],
        }).encode()

    def test_wrong_identity_nan_bool_and_unknown_schema_fail_the_document(self):
        # Break: an answer for another run, or a v2 document, is shown as this review.
        wrong = json.loads(self._raw())
        wrong["run_id"] = "ff" * 16
        outcome, result, error = validate_result(json.dumps(wrong).encode(), self.request, PATCH)
        self.assertEqual(outcome, "failed")
        self.assertIsNone(result)
        self.assertIn("run_id", error)
        wrong["run_id"] = self.request["run_id"]
        wrong["schema"] = "firstpane.review.result.v2"
        outcome, _, error = validate_result(json.dumps(wrong).encode(), self.request, PATCH)
        self.assertIn("unsupported result schema", error)
        nan = self._raw().replace(b'"low"', b"NaN", 1)
        # NaN is not in an enum field this way; put it in a line number via a crafted document.
        crafted = json.loads(self._raw())
        crafted["findings"][0]["start_line"] = float("nan")
        outcome, _, error = validate_result(
            json.dumps(crafted, allow_nan=True).encode(), self.request, PATCH
        )
        self.assertEqual(outcome, "failed")
        self.assertIn("invalid output", error)
        crafted = json.loads(self._raw())
        crafted["findings"][0]["start_line"] = True
        outcome, result, _ = validate_result(json.dumps(crafted).encode(), self.request, PATCH)
        self.assertEqual(outcome, "partial")
        self.assertEqual(result["findings"], [])

    def test_line_outside_the_hunk_is_dropped_and_unknown_fields_are_kept_out(self):
        # Break: a finding on a line the pinned patch does not contain is drawn inline.
        outcome, result, _ = validate_result(self._raw(start_line=99, end_line=99), self.request, PATCH)
        self.assertEqual(outcome, "partial")
        self.assertEqual(result["dropped"], 1)
        outcome, result, _ = validate_result(self._raw(extra="ignore me"), self.request, PATCH)
        self.assertEqual(outcome, "complete")
        self.assertNotIn("extra", result["findings"][0])
        self.assertEqual(result["findings"][0]["start_line"], 12)


class StaleTests(unittest.TestCase):
    def test_labels_name_the_change_and_do_not_delete_the_finding(self):
        # Break: a moved HEAD is shown as current, or a stale finding disappears.
        self.assertEqual(stale_label("uncommitted", head_moved=True, blob_changed=False), "stale: HEAD moved")
        self.assertEqual(stale_label("uncommitted", head_moved=False, blob_changed=True), "stale: file changed since review")
        self.assertEqual(stale_label("pr", head_moved=True, old="aaa", new="bbb"), "stale: PR head moved aaa→bbb")
        self.assertIsNone(stale_label("commit", head_moved=True))
        self.assertEqual(stale_label("commit", listed=False), "commit no longer listed")


class _Child:
    def __init__(self, pid, code=None):
        self.pid = pid
        self._code = code
        self.killed = []

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        if self._code is None:
            raise TimeoutError
        return self._code


class SupervisorTests(unittest.TestCase):
    def test_start_uses_argv_files_and_strips_herdr_env(self):
        # Break: the command is a shell string, or the reviewer inherits the Herdr socket.
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}

            def popen(argv, **kwargs):
                seen["argv"] = argv
                seen["kwargs"] = kwargs
                return _Child(4242)

            clock = {"t": 100.0}
            run = ReviewSupervisor(tmp, popen, clock=lambda: clock["t"])
            body = request()
            run.start(("/bin/review", "fixed"), body, cwd="/repo", env={"HERDR_SOCKET_PATH": "/tmp/h", "PATH": "/bin"}, timeout=30)
            self.assertEqual(seen["argv"], ["/bin/review", "fixed"])
            self.assertFalse(seen["kwargs"]["shell"])
            self.assertTrue(seen["kwargs"]["start_new_session"])
            self.assertNotIn("HERDR_SOCKET_PATH", seen["kwargs"]["env"])
            self.assertEqual(seen["kwargs"]["env"]["PATH"], "/bin")
            self.assertTrue(str(seen["kwargs"]["stdin"].name).endswith("request.json"))
            with self.assertRaises(RuntimeError):
                run.start(("/bin/review",), body, cwd="/repo", env={}, timeout=30)
            run.cancel()

    def test_orphan_is_not_adopted(self):
        # Break: a restarted Firstpane signals a pid it did not start.
        note = orphan_note({"state": "running", "pid": 9, "started_at": "2026-10-29T10:00:00Z"})
        self.assertIn("may still be running", note)
        self.assertIn("not adopted", note)
        self.assertIsNone(orphan_note({"state": "complete", "pid": 9}))

    def test_cancel_kills_the_process_group_and_ignores_output(self):
        # Break: cancel waits for stdout and shows findings from a stopped run.
        with tempfile.TemporaryDirectory() as tmp:
            child = _Child(77)
            killed = []
            run = ReviewSupervisor(tmp, lambda *args, **kwargs: child, clock=lambda: 1, kill=lambda pid, sig: killed.append((pid, sig)))
            run.start(("/bin/review",), request(), cwd=tmp, env={}, timeout=30)
            (Path(tmp) / "reviews" / ("ab" * 16) / "stdout.json").write_text('{"schema":"no"}')
            self.assertEqual(run.cancel(), "cancelled")
            self.assertEqual(killed[0][0], 77)
            self.assertEqual(run.outcome, "cancelled")
            mode = os.stat(Path(tmp) / "reviews" / ("ab" * 16)).st_mode
            self.assertTrue(mode & stat.S_IRWXU)
            self.assertFalse(mode & (stat.S_IRWXG | stat.S_IRWXO))
