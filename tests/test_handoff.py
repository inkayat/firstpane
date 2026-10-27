"""Filling the FirstMate primary's own message box with a message, unsent.

The primary is found from the home's session lock (state/.lock pid) and the one Herdr pane
running that process. Its pi composer is FakePi at the socket boundary. Nothing reaches a
real Herdr session or FirstMate.
"""

import tempfile
import unittest
from pathlib import Path

from firstpane.handoff import Handoff
from tests.support import FakePi, FakeTransport, pane

BODY = (
    "Firstpane message\n"
    "project: alpha  /p/alpha\n"
    "agent: alpha-task  claude  pane w1:p1  terminal term_a\n"
    "\n"
    "Bu satırı kontrol et 🚀\n"
    "\n"
    "[1] diff README.md  unstaged  new lines 2-2\n"
    "```diff\n"
    "+TWO\n"
    "+THREE\n"
    "```\n"
)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "fm-home"
        self.transport = FakeTransport()
        self.transport.snapshot["panes"].append(pane(pane_id="w1:p1", terminal_id="term_a", agent="claude"))
        self.main = FakePi(self.transport, self.home)
        self.handoff = Handoff(self.transport, str(self.home), sleep=lambda seconds: None)

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, body=BODY):
        target, refusal = self.handoff.locate()
        self.assertIsNone(refusal)
        return self.handoff.transfer(target, body)

    def assert_untouched(self, before=""):
        self.assertEqual(self.main.received, [])
        self.assertEqual(self.main.text, before)
        self.assertEqual(self.main.submitted, [])

    def test_a_short_message_fills_the_lock_owners_empty_box_unsent(self):
        # Break: the paste goes to another pane, carries a CR (submits), arrives without
        # bracketed paste (no paste semantics) or reports success it did not observe.
        short = "Firstpane message\nagent: alpha\n\nkısa not 🚀\n\tindented"
        outcome, detail = self.send(short)
        self.assertEqual(outcome, "transferred", detail)
        self.assertEqual(self.main.received, ["\x1b[200~" + short + "\x1b[201~"])
        self.assertEqual(self.main.text, "Firstpane message\nagent: alpha\n\nkısa not 🚀\n    indented")
        self.assertEqual(self.main.submitted, [])

    def test_a_long_message_is_one_paste_that_pi_collapses_and_is_still_confirmed(self):
        # Break: a message over ten lines is sent in pieces, loses its trailing fence, or the
        # collapsed `[paste #1 +N lines]` view is reported as a failed transfer.
        outcome, detail = self.send(BODY)
        self.assertEqual(outcome, "transferred", detail)
        self.assertEqual(self.main.received, ["\x1b[200~" + BODY.rstrip("\n") + "\x1b[201~"])
        self.assertEqual(self.main.text, "[paste #1 +11 lines]")
        self.assertEqual(self.main.submitted, [])

    def test_selected_text_cannot_end_the_paste_submit_or_send_keys(self):
        # Break: an ESC from a diff or page ends the bracketed paste early so the rest runs
        # as keys, or a CR / Ctrl-C reaches the composer as a key.
        hostile = "look\x1b[201~\r/quit\x03\x1b]0;title\x07 done\r\nnext"
        outcome, detail = self.send(hostile)
        self.assertEqual(outcome, "transferred", detail)
        sent = self.main.received[0]
        self.assertTrue(sent.startswith("\x1b[200~") and sent.endswith("\x1b[201~"))
        inner = sent[len("\x1b[200~"):-len("\x1b[201~")]
        self.assertNotIn("\x1b", inner)
        self.assertNotIn("\r", inner)
        self.assertNotIn("\x03", inner)
        self.assertEqual(self.main.text, "look\n/quit done\nnext")
        self.assertEqual(self.main.submitted, [])

    def test_text_already_in_the_box_is_never_touched(self):
        # Break: the paste is appended to (or replaces) what the captain was typing there.
        self.main.text = "half-written thought"
        outcome, detail = self.send()
        self.assertEqual(outcome, "refused")
        self.assertIn("already has text", detail)
        self.assert_untouched("half-written thought")

    def test_a_second_transfer_of_the_same_message_does_not_duplicate_it(self):
        # Break: pressing send twice puts the message in the box twice.
        self.assertEqual(self.send()[0], "transferred")
        outcome, detail = self.send()
        self.assertEqual(outcome, "refused")
        self.assertIn("already has text", detail)
        self.assertEqual(len(self.main.received), 1)

    def test_busy_or_blocked_firstmate_is_refused(self):
        # Break: text is pasted while the primary is mid-turn or at an approval dialog.
        for status in ("working", "blocked", "unknown"):
            with self.subTest(status=status):
                self.main.pane["agent_status"] = status
                outcome, detail = self.send()
                self.assertEqual(outcome, "refused")
                self.assertIn(status, detail)
                self.assert_untouched()

    def test_a_dialog_in_place_of_the_box_is_refused(self):
        # Break: the paste lands in a trust prompt or menu that Herdr still reports as idle.
        self.main.dialog = "Trust project folder?  → Trust"
        outcome, detail = self.send()
        self.assertEqual(outcome, "refused")
        self.assertIn("not showing", detail)
        self.assertEqual(self.main.received, [])

    def test_a_scrolled_back_pane_is_refused(self):
        # Break: the screen read is scrollback, so an old empty box is taken for the live one.
        self.main.pane["scroll"] = {"offset_from_bottom": 5, "max_offset_from_bottom": 9, "viewport_rows": 30}
        outcome, detail = self.send()
        self.assertEqual(outcome, "refused")
        self.assertIn("scrolled", detail)
        self.assert_untouched()

    def test_the_primary_is_the_lock_owner_and_nothing_else(self):
        # Break: a missing lock, a lock pid no pane runs, or a pid two panes claim picks
        # some other agent (first pi, title match) instead of refusing.
        target, refusal = self.handoff.locate()
        self.assertEqual((target["pane_id"], target["terminal_id"]), ("w5:p1", "term_main"))
        (self.home / "state" / ".lock").write_text("999\n")
        target, refusal = self.handoff.locate()
        self.assertIsNone(target)
        self.assertIn("not open in this Herdr session", refusal)
        (self.home / "state" / ".lock").unlink()
        target, refusal = self.handoff.locate()
        self.assertIsNone(target)
        self.assertIn("not running", refusal)
        (self.home / "state" / ".lock").write_text("4242\n")
        twin = pane(pane_id="w6:p1", terminal_id="term_twin", agent="pi", agent_status="idle")
        self.transport.snapshot["panes"].append(twin)
        original = self.main.process_info
        self.transport.socket_handlers["pane.process_info"] = lambda params: original(dict(params, pane_id="w5:p1"))
        target, refusal = self.handoff.locate()
        self.assertIsNone(target)
        self.assertIn("more than one pane", refusal)

    def test_a_primary_on_an_unproven_harness_is_refused(self):
        # Break: the pi-only paste and box check are applied to another harness's screen.
        self.main.pane["agent"] = "claude"
        target, refusal = self.handoff.locate()
        self.assertIsNone(target)
        self.assertIn("claude", refusal)
        self.assertEqual(self.main.received, [])

    def test_a_pane_that_changed_since_the_preview_is_refused(self):
        # Break: the paste goes to whatever now occupies the previewed pane or pid.
        target, _ = self.handoff.locate()
        self.main.pane["terminal_id"] = "term_reused"
        outcome, detail = self.handoff.transfer(target, BODY)
        self.assertEqual(outcome, "refused")
        self.assertIn("changed", detail)
        self.assertEqual(self.main.received, [])

    def test_a_paste_the_box_does_not_show_is_unconfirmed_not_success(self):
        # Break: Firstpane reports the draft transferred without seeing it in the box.
        self.main.render = False
        outcome, detail = self.send()
        self.assertEqual(outcome, "unconfirmed")
        self.assertIn("look", detail)
        self.assertEqual(len(self.main.received), 1)

    def test_a_lost_reply_to_the_paste_is_unconfirmed(self):
        # Break: a socket error after the paste was written is reported as refused (nothing
        # sent), inviting a duplicate.
        target, _ = self.handoff.locate()
        deliver = self.main.send_text

        def lost(params):
            deliver(params)
            raise OSError("connection reset")

        self.transport.socket_handlers["pane.send_text"] = lost
        self.main.render = False
        outcome, detail = self.handoff.transfer(target, BODY)
        self.assertEqual(outcome, "unconfirmed")

    def test_an_unrelated_pane_closing_during_the_search_does_not_block_the_transfer(self):
        # Break: one pane answering pane_not_found (closed since the snapshot) aborts the
        # search for the primary, or an unreachable Herdr is mistaken for "not found".
        self.transport.snapshot["panes"].insert(0, pane(pane_id="w9:gone", terminal_id="term_gone", agent="codex"))
        info = self.main.process_info

        def gone(params):
            if params["pane_id"] == "w9:gone":
                return {"id": "t", "error": {"code": "pane_not_found", "message": "missing"}}
            return info(params)

        self.transport.socket_handlers["pane.process_info"] = gone
        self.assertEqual(self.send()[0], "transferred")

        def down(params):
            raise OSError("socket gone")

        self.transport.socket_handlers["pane.process_info"] = down
        target, refusal = self.handoff.locate()
        self.assertIsNone(target)
        self.assertEqual(refusal, "Herdr unreachable")

    def test_a_message_taller_than_the_box_is_confirmed_from_its_visible_tail(self):
        # Break: pi's scrolled border (`─── ↑ N more ───`) is not taken for the box, so a
        # message that did land is reported unconfirmed.
        self.main.rows = 3
        medium = "Firstpane message\nagent: alpha\n\nbir\niki\nüç 🚀"
        outcome, detail = self.send(medium)
        self.assertEqual(outcome, "transferred", detail)
        self.assertEqual(self.main.text, medium)

    def test_long_text_already_in_a_scrolled_box_is_never_touched(self):
        # Break: a box scrolled by its own long text is misread as missing or empty.
        self.main.rows = 2
        self.main.text = "satır 1\nsatır 2\nsatır 3\nsatır 4"
        outcome, detail = self.send()
        self.assertEqual(outcome, "refused")
        self.assertIn("already has text", detail)
        self.assert_untouched("satır 1\nsatır 2\nsatır 3\nsatır 4")


if __name__ == "__main__":
    unittest.main()
