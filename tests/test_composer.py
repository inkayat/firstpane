"""Composer editing, per-target drafts and the exact outbound message."""

import tempfile
import unittest
from pathlib import Path

from firstpane.drafts import DraftStore
from firstpane.lineedit import LineEdit
from firstpane.message import build_message


class LineEditTests(unittest.TestCase):
    def test_cursor_editing_with_turkish_text(self):
        # Break: Backspace/Delete remove a byte or the wrong character, or Home/End jump
        # to the wrong place, corrupting a Turkish message.
        edit = LineEdit()
        edit.insert("şimdi gör")
        edit.key("left")
        edit.key("left")
        edit.key("left")
        edit.insert("ü")
        self.assertEqual(edit.text, "şimdi ügör")
        edit.key("backspace")
        edit.key("delete")
        self.assertEqual(edit.text, "şimdi ör")
        edit.key("home")
        edit.key("delete")
        edit.key("end")
        edit.insert("ğ")
        self.assertEqual(edit.text, "imdi örğ")

    def test_paste_keeps_newlines_and_drops_terminal_controls(self):
        # Break: a pasted escape sequence reaches the frame, or CRLF leaves stray CRs.
        edit = LineEdit()
        edit.insert("a\r\nb\x1b[31mred\x1b]0;x\x07\tc", paste=True)
        self.assertEqual(edit.text, "a\nbred\tc")

    def test_ctrl_u_clears_back_to_the_line_start(self):
        # Break: Ctrl-U wipes the whole multi-line draft instead of the current line.
        edit = LineEdit("keep\ndrop this")
        edit.key("ctrl-u")
        self.assertEqual(edit.text, "keep\n")


class DraftStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_draft_survives_restart_and_keeps_its_request_id(self):
        # Break: a restart loses the draft or mints a new request id, so a resend duplicates.
        store = DraftStore(self.dir)
        draft = store.load("fm:alpha:term_a", {"agent": "alpha"})
        draft["body"] = "merhaba"
        self.assertTrue(store.save(draft))
        again = DraftStore(self.dir).load("fm:alpha:term_a", {"agent": "alpha"})
        self.assertEqual(again["body"], "merhaba")
        self.assertEqual(again["request_id"], draft["request_id"])

    def test_targets_have_separate_drafts(self):
        # Break: switching agents carries the text (and attachments) to the other agent.
        store = DraftStore(self.dir)
        a = store.load("fm:alpha:term_a", {})
        a["body"] = "for alpha"
        store.save(a)
        b = store.load("fm:beta:term_b", {})
        self.assertEqual(b["body"], "")
        self.assertNotEqual(a["request_id"], b["request_id"])

    def test_a_second_pane_cannot_silently_overwrite(self):
        # Break: two Firstpane panes sharing the state dir overwrite each other's draft.
        one = DraftStore(self.dir)
        two = DraftStore(self.dir)
        a = one.load("general", {})
        b = two.load("general", {})
        a["body"] = "from pane one"
        self.assertTrue(one.save(a))
        b["body"] = "from pane two"
        self.assertFalse(two.save(b))
        self.assertEqual(DraftStore(self.dir).load("general", {})["body"], "from pane one")

    def test_a_sent_draft_is_not_reused(self):
        # Break: after a queued send the next message reuses the request id and is replayed
        # (dropped) by the inbox instead of delivered.
        store = DraftStore(self.dir)
        draft = store.load("general", {})
        draft["outcome"] = "queued"
        store.save(draft)
        fresh = store.load("general", {})
        self.assertNotEqual(fresh["request_id"], draft["request_id"])
        self.assertEqual(fresh["body"], "")


CONTEXT = {
    "project": "alpha",
    "project_key": "/repos/alpha",
    "agent": "alpha-task",
    "harness": "claude",
    "pane_id": "w1:p1",
    "terminal_id": "term_a",
    "workspace": "/wt/alpha",
    "provenance": "fm",
    "branch": "fm/alpha-task",
    "head": "0123456789abcdef0123",
}


def diff_attachment(lines, total=None):
    return {
        "kind": "diff",
        "path": "src/app.py",
        "group": "unstaged",
        "side": "new",
        "start": 12,
        "end": 12 + len(lines) - 1,
        "new_start": 12,
        "new_end": 12 + len(lines) - 1,
        "head": "0123456789abcdef0123",
        "blob": "9f3e2a1b7c0d9f3e2a1b",
        "source": "workspace",
        "revision": "uncommitted",
        "lines": lines,
        "lines_total": total if total is not None else len(lines),
    }


class MessageTests(unittest.TestCase):
    def test_exact_body_carries_context_text_and_marked_lines(self):
        # Break: the preview and the sent text differ, the agent/workspace/branch context is
        # missing, or the attachment loses its path/old-new ranges/HEAD/blob provenance.
        attachment = dict(diff_attachment(["-x = 0", "+x = 1", " y = 2"]), old_start=12, old_end=13, new_end=13)
        body, warnings = build_message(CONTEXT, "Bunu kontrol et", [attachment])
        self.assertEqual(
            body,
            "Firstpane message\n"
            "project: alpha  /repos/alpha\n"
            "agent: alpha-task  claude  pane w1:p1  terminal term_a\n"
            "workspace: /wt/alpha  (fm)\n"
            "branch: fm/alpha-task @ 0123456789ab\n"
            "\n"
            "Bunu kontrol et\n"
            "\n"
            "[1] diff src/app.py  unstaged  old lines 12-13  new lines 12-13  HEAD 0123456789ab  new blob 9f3e2a1b7c0d\n"
            "source workspace  revision uncommitted\n"
            "```diff\n"
            "-x = 0\n"
            "+x = 1\n"
            " y = 2\n"
            "```\n",
        )
        self.assertEqual(warnings, [])

    def test_a_diff_attachment_saved_before_old_new_ranges_still_names_its_side_and_range(self):
        # Break: a draft written by the removed composer (side/start/end only) loses its
        # line range when it is finally sent.
        legacy = {key: value for key, value in diff_attachment(["+x = 1"]).items() if not key.startswith("new_")}
        body, _ = build_message(CONTEXT, "", [legacy])
        self.assertIn("[1] diff src/app.py  unstaged  new lines 12-12  HEAD 0123456789ab  new blob 9f3e2a1b7c0d\n", body)

    def test_the_internal_board_request_binding_is_not_sent_as_an_attachment(self):
        # Break: a Board reply draft carries a board-request binding next to its board task;
        # rendering it appends a stray "[2] unsupported attachment" block to the paste.
        board_task = {"kind": "board", "id": "fp-ask-0001", "project": "alter", "state": "waiting", "title": "Layout?"}
        request = {"kind": "board-request", "id": "fp-ask-0001"}
        body, _ = build_message(CONTEXT, "ok", [board_task, request])
        self.assertIn("[1] board task fp-ask-0001", body)
        self.assertNotIn("unsupported attachment", body)
        self.assertNotIn("[2]", body)

    def test_long_attachment_is_truncated_visibly(self):
        # Break: a huge selection is sent whole, or cut without saying so.
        lines = [f"+line {n}" for n in range(250)]
        body, _ = build_message(CONTEXT, "", [diff_attachment(lines)])
        self.assertIn("+line 199\n", body)
        self.assertNotIn("+line 200\n", body)
        self.assertIn("[truncated: shown 200 of 250 lines]", body)

    def test_secret_looking_content_warns_without_rewriting(self):
        # Break: a token in a marked line goes out with no warning on the preview.
        token = "ghp_" + "A" * 36
        body, warnings = build_message(CONTEXT, "", [diff_attachment([f"+TOKEN = '{token}'"])])
        self.assertIn(token, body)
        self.assertEqual(warnings, ["attachment 1 looks like it contains a secret (ghp_)"])

    def test_browser_attachment_is_sanitized_and_fenced_safely(self):
        # Break: page text escapes the code fence or carries terminal controls into the note.
        attachment = {
            "kind": "browser",
            "url": "https://example.test/a",
            "title": "Beta\x1b]0;x\x07",
            "selection": "see ```\nhere\x1b[2J",
            "browser_key": "5224-1",
            "tab_id": "1",
            "captured_at": "2026-10-24T10:00:00+0000",
        }
        body, _ = build_message(CONTEXT, "", [attachment])
        self.assertIn('[1] browser "Beta"  https://example.test/a  instance 5224-1 tab 1  captured 2026-10-24T10:00:00+0000\n', body)
        self.assertIn("````text\nsee ```\nhere\n````\n", body)
        self.assertNotIn("\x1b", body)

    def test_without_selected_agent_the_context_says_none(self):
        # Break: a general message is labelled with a stale agent.
        body, _ = build_message({}, "hi", [])
        self.assertEqual(body, "Firstpane message\nagent: none selected\n\nhi\n")


if __name__ == "__main__":
    unittest.main()
