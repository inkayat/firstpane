"""Copy a confirmed message into the FirstMate primary's input box as an unsent draft.

The primary is the lock owner in the configured home, found in this Herdr session. FakePi is
the composer at the socket boundary. Nothing reaches a real Herdr session or FirstMate.
"""

import tempfile
import unittest
from pathlib import Path

from firstpane.inputdraft import InputDraft
from tests.support import FakePi, FakeTransport, pane

SHORT = "Firstpane message\nagent: alpha\n\nkısa not 🚀\n\tindented"
LONG = (
    "Firstpane message\n"
    "project: alpha\n"
    "agent: alpha-task\n"
    "\n"
    "line one\n"
    "line two\n"
    "line three\n"
    "line four\n"
    "line five\n"
    "line six\n"
    "line seven\n"
)


class InputDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "fm-home"
        self.transport = FakeTransport()
        self.transport.snapshot["panes"].append(pane(pane_id="w1:p1", terminal_id="term_a", agent="claude"))
        self.main = FakePi(self.transport, self.home, status="working", text="half a thought")
        self.sleeps = []
        self.draft = InputDraft(self.transport, str(self.home), sleep=self._sleep)

    def tearDown(self):
        self.tmp.cleanup()

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > 12:
            raise AssertionError("paste waited instead of returning")

    def transfer(self, body=SHORT):
        target, refusal = self.draft.locate()
        self.assertIsNone(refusal, refusal)
        return self.draft.transfer(target, body)

    def test_a_working_primary_gets_a_multiline_draft_immediately_without_enter(self):
        # Break: a working FirstMate is refused, the paste waits for idle, a CR submits,
        # or the text is not one bracketed paste.
        self.main.text = ""
        outcome, detail = self.transfer()
        self.assertEqual(outcome, "transferred", detail)
        self.assertEqual(self.main.pane["agent_status"], "working")
        self.assertEqual(self.main.submitted, [])
        self.assertEqual(self.main.received, ["\x1b[200~" + SHORT.rstrip("\n") + "\x1b[201~"])
        self.assertNotIn("\r", self.main.received[0])
        self.assertEqual(self.main.text, "Firstpane message\nagent: alpha\n\nkısa not 🚀\n    indented")
        self.assertNotIn("idle", detail.lower())
        self.assertLessEqual(len(self.sleeps), 10)

    def test_a_trailing_space_hidden_by_the_screen_still_confirms_the_append(self):
        # Break: pi's screen read strips the space at the end of the existing line, so an
        # append that kept that space is reported unconfirmed.
        self.main.text = "keep me "
        self.main.pane["agent_status"] = "working"
        outcome, detail = self.transfer("Firstpane")
        self.assertEqual(outcome, "transferred", detail)
        self.assertEqual(self.main.text, "keep me Firstpane")
        self.assertEqual(self.main.submitted, [])

    def test_existing_input_is_kept_and_the_draft_is_appended(self):
        # Break: text the captain was typing is erased, replaced, or left without the draft.
        outcome, detail = self.transfer("look here")
        self.assertEqual(outcome, "transferred", detail)
        self.assertTrue(self.main.text.startswith("half a thought"), self.main.text)
        self.assertTrue(self.main.text.endswith("look here"), self.main.text)
        self.assertEqual(self.main.submitted, [])
        self.assertEqual(len(self.main.received), 1)

    def test_a_dialog_is_refused_as_not_showing_and_nothing_is_pasted(self):
        # Break: a modal is labelled busy, or the paste lands in the dialog.
        self.main.dialog = "Trust project folder?  → Trust"
        self.main.pane["agent_status"] = "blocked"
        outcome, detail = self.transfer()
        self.assertEqual(outcome, "refused", detail)
        self.assertIn("not showing", detail)
        self.assertNotIn("busy", detail.lower())
        self.assertNotIn("idle", detail.lower())
        self.assertEqual(self.main.received, [])
        self.assertEqual(self.main.text, "half a thought")
        self.assertEqual(self.main.submitted, [])

    def test_a_scrolled_pane_is_refused_before_paste_without_calling_it_busy(self):
        # Break: scrollback is treated as the live input box, or a scrolled working pane is called busy.
        self.main.pane["scroll"] = {"offset_from_bottom": 4, "max_offset_from_bottom": 9, "viewport_rows": 30}
        outcome, detail = self.transfer()
        self.assertEqual(outcome, "refused", detail)
        self.assertIn("scrolled", detail)
        self.assertNotIn("busy", detail.lower())
        self.assertNotIn("working", detail.lower())
        self.assertEqual(self.main.received, [])

    def test_the_lock_owner_is_the_only_recipient(self):
        # Break: a missing lock, another pid, two panes, or a non-pi harness still receives the paste.
        target, refusal = self.draft.locate()
        self.assertEqual((target["pane_id"], target["terminal_id"]), ("w5:p1", "term_main"))
        (self.home / "state" / ".lock").write_text("999\n")
        target, refusal = self.draft.locate()
        self.assertIsNone(target)
        self.assertIn("not open in this Herdr session", refusal)
        (self.home / "state" / ".lock").unlink()
        target, refusal = self.draft.locate()
        self.assertIsNone(target)
        self.assertIn("not running", refusal)
        (self.home / "state" / ".lock").write_text("4242\n")
        self.main.pane["agent"] = "claude"
        target, refusal = self.draft.locate()
        self.assertIsNone(target)
        self.assertIn("claude", refusal)
        self.assertEqual(self.main.received, [])

    def test_controls_in_the_message_cannot_end_the_paste_or_submit(self):
        # Break: an ESC, CR or Ctrl-C in the message is delivered as a key.
        self.main.text = ""
        hostile = "look\x1b[201~\r/quit\x03 done\r\nnext"
        outcome, detail = self.transfer(hostile)
        self.assertEqual(outcome, "transferred", detail)
        inner = self.main.received[0][len("\x1b[200~"):-len("\x1b[201~")]
        self.assertNotIn("\x1b", inner)
        self.assertNotIn("\r", inner)
        self.assertNotIn("\x03", inner)
        self.assertEqual(self.main.submitted, [])
        self.assertEqual(self.main.text, "look\n/quit done\nnext")

    def test_a_paste_the_box_does_not_show_is_unconfirmed(self):
        # Break: an unseen paste is reported as transferred and a later retry is invited as success.
        self.main.text = ""
        self.main.render = False
        outcome, detail = self.transfer()
        self.assertEqual(outcome, "unconfirmed", detail)
        self.assertIn("look", detail.lower())
        self.assertEqual(len(self.main.received), 1)
        self.assertEqual(self.main.submitted, [])

    def test_a_message_already_in_the_box_is_not_pasted_again(self):
        # Break: confirming twice appends the same draft a second time.
        self.main.text = ""
        self.assertEqual(self.transfer("once")[0], "transferred")
        outcome, detail = self.transfer("once")
        self.assertEqual(outcome, "transferred", detail)
        self.assertIn("not pasted again", detail)
        self.assertEqual(len(self.main.received), 1)
        self.assertEqual(self.main.text, "once")

    def test_a_long_draft_in_an_empty_box_is_confirmed_from_pi_paste_marker(self):
        # Break: a message over ten lines is split, submitted, or reported missing because pi collapsed it.
        self.main.text = ""
        outcome, detail = self.transfer(LONG)
        self.assertEqual(outcome, "transferred", detail)
        self.assertEqual(self.main.received, ["\x1b[200~" + LONG.rstrip("\n") + "\x1b[201~"])
        self.assertEqual(self.main.text, "[paste #1 +11 lines]")
        self.assertEqual(self.main.submitted, [])

    def test_only_pane_send_text_is_used(self):
        # Break: the copy focuses the pane, sends keys, or submits through agent.prompt.
        self.main.text = ""
        self.transfer("literal")
        methods = [call["method"] for call in self.transport.socket_calls]
        self.assertIn("pane.send_text", methods)
        self.assertNotIn("pane.send_keys", methods)
        self.assertNotIn("pane.send_input", methods)
        self.assertNotIn("agent.prompt", methods)
        self.assertNotIn("pane.focus", methods)
