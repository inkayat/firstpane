"""Settings chooses one FirstMate transport: the durable queue, or an immediate unsent draft.

Absent or old settings stay on the queue. Draft mode pastes into the lock owner's input box
even while FirstMate is working, and does not queue, submit, or press Enter.
"""

import json
import unittest

from tests.support import FakePi
from tests.test_board_layout import LayoutFixture
from tests.test_panel import PanelFixture, plain


QUEUE = "Queue for next check"
DRAFT = "Unsent draft in input box"


class DeliverySettingsTests(PanelFixture):
    def choice_line(self, app, label):
        return next(line for line in self.lines(app) if label in line)

    def saved(self):
        return json.loads((self.state / "ui-state.json").read_text()).get("message_delivery")

    def test_settings_offers_both_transports_and_an_old_file_stays_on_the_queue(self):
        # Break: the choice is missing, both look selected, or a ui-state from before this
        # setting queues nothing or pastes by default.
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "ui-state.json").write_text(json.dumps({"tab": "agents", "browser_placement": "ghostty"}))
        app = self.app(env={**self.fx.env, "NO_COLOR": "1"})
        app.handle_key(",")
        frame = plain(app.render())
        self.assertIn("Messages to FirstMate", frame)
        self.assertIn("(\u2022) " + QUEUE, self.choice_line(app, QUEUE))
        self.assertIn("( ) " + DRAFT, self.choice_line(app, DRAFT))
        self.assertEqual(app.message_delivery, "queue")
        app.handle_key("esc")
        self.changed_readme()
        app.refresh(manual=True)
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "keep the queue")
        self.keys(app, "enter", "y")
        self.assertEqual(self.main.received, [])
        self.assertEqual(len(self.transport.inbox), 1)
        self.assertIn("keep the queue", self.transport.inbox[0]["stdin"])

    def test_choosing_the_draft_survives_restart_and_keeps_a_composed_message(self):
        # Break: the choice is not written, a restart returns to paste or drops the draft text.
        self.changed_readme()
        app = self.app()
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "yarım kaldı")
        app.handle_key("esc")
        app.handle_key(",")
        self.click_text(app, DRAFT)
        self.assertEqual(self.saved(), "input-draft")
        self.assertIn("(\u2022) " + DRAFT, self.choice_line(app, DRAFT))
        self.assertIn("( ) " + QUEUE, self.choice_line(app, QUEUE))
        app.handle_key("esc")
        self.assertIn("yarım kaldı", plain(app.render()))
        again = self.app()
        again.handle_key(",")
        self.assertIn("(\u2022) " + DRAFT, self.choice_line(again, DRAFT))
        again.handle_key("esc")
        again.handle_key("2")
        self.assertIn("yarım kaldı", plain(again.render()))


class DraftTransportTests(PanelFixture):
    def choose_draft(self, app):
        app.handle_key(",")
        self.click_text(app, DRAFT)
        app.handle_key("esc")

    def test_review_confirm_pastes_a_working_primary_and_does_not_queue_or_submit(self):
        # Break: draft mode waits, queues, presses Enter, or replaces text already in the box.
        self.main.pane["agent_status"] = "working"
        self.main.text = "half a thought"
        self.changed_readme()
        app = self.app()
        self.choose_draft(app)
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        app.handle_bytes(b"\x1b[200~line one\nline two\x1b[201~")
        app.handle_key("enter")
        preview = plain(app.render())
        self.assertIn("unsent draft", preview)
        self.assertIn("Enter is not pressed", preview)
        self.assertNotIn("Queue this for FirstMate", preview)
        self.assertIn("line one", preview)
        self.assertIn("line two", preview)
        app.message_delivery = "queue"
        app.handle_key("y")
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(self.main.submitted, [])
        self.assertEqual(len(self.main.received), 1)
        sent = self.main.received[0]
        self.assertTrue(sent.startswith("\x1b[200~") and sent.endswith("\x1b[201~"))
        self.assertNotIn("\r", sent)
        self.assertTrue(self.main.text.startswith("half a thought"), self.main.text)
        self.assertIn("line one\nline two", sent)
        self.assertIn("not sent", app.status)
        self.assertNotIn("queued", app.status)

    def test_agents_confirm_uses_only_the_selected_draft_transport(self):
        # Break: the Agents composer still queues after the draft transport is selected.
        self.main.text = ""
        self.main.pane["agent_status"] = "working"
        app = self.app()
        self.choose_draft(app)
        app.handle_key("1")
        self.select(app, "alpha-task")
        app.handle_key("m")
        self.type(app, "from agents")
        self.keys(app, "enter", "y")
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(self.main.submitted, [])
        self.assertIn("from agents", self.main.text)
        self.assertIn("not sent", app.status)

    def test_a_pr_note_in_draft_mode_is_pasted_and_not_queued(self):
        # Break: a pull-request note ignores the setting and still calls fm-inbox.sh.
        self.main.text = ""
        self.main.pane["agent_status"] = "working"
        app = self.app()
        self.choose_draft(app)
        self.transport.editor_body = "ping the captain\n"
        app._arm_note({
            "kind": "pr",
            "number": 9,
            "host": "github.com",
            "owner": "acme",
            "repo": "widgets",
            "slug": "acme/widgets",
            "project_key": "missing",
            "key": ("pr", "acme/widgets", 9),
        })
        self.assertEqual(app.screen, "confirm-note")
        preview = plain(app.render())
        self.assertIn("unsent draft", preview)
        self.assertNotIn("Queue note", preview)
        app.handle_key("y")
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(self.main.submitted, [])
        self.assertIn("ping the captain", self.main.text)
        self.assertNotIn("\r", self.main.received[0])

    def test_a_dialog_or_wrong_recipient_is_named_and_the_draft_stays(self):
        # Break: a modal or the wrong home is called busy, or the text is queued instead.
        self.changed_readme()
        app = self.app()
        self.choose_draft(app)
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "keep me")
        self.main.dialog = "Trust project folder?  → Trust"
        self.main.pane["agent_status"] = "blocked"
        self.keys(app, "enter", "y")
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(self.main.received, [])
        self.assertIn("not showing", app.status)
        self.assertNotIn("busy", app.status.lower())
        self.assertNotIn("idle", app.status.lower())
        self.assertEqual(app.composer.text, "keep me")
        self.main.dialog = None
        (self.home / "state" / ".lock").write_text("999\n")
        self.keys(app, "enter", "y")
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(self.main.received, [])
        self.assertIn("not open in this Herdr session", app.status)
        self.assertEqual(app.composer.text, "keep me")

    def test_an_unconfirmed_paste_keeps_the_draft_and_does_not_queue(self):
        # Break: an unseen paste is reported as sent, or the failure falls back to the queue.
        self.main.render = False
        self.main.text = ""
        self.changed_readme()
        app = self.app()
        self.choose_draft(app)
        self.open_changes(app)
        self.double_click(app, self.row_of(app, "+ TWO"))
        self.type(app, "uncertain")
        self.keys(app, "enter", "y")
        self.assertEqual(self.transport.inbox, [])
        self.assertEqual(len(self.main.received), 1)
        self.assertIn("look", app.status.lower())
        self.assertEqual(app.composer.text, "uncertain")
        self.assertNotIn("queued", app.status)


class BoardDraftTests(LayoutFixture):
    def test_a_board_reply_in_draft_mode_is_pasted_and_does_not_resolve_the_task(self):
        # Break: the Board composer still queues, or copying the reply records an answer.
        primary = FakePi(self.transport, self.home, status="working", text="")
        app = self.board()
        app.handle_key(",")
        row, column = next(
            (index + 1, line.index(DRAFT) + 1)
            for index, line in enumerate(plain(app.render()).splitlines())
            if DRAFT in line
        )
        app.handle_bytes(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())
        app.handle_key("esc")
        app.handle_key("tab")
        self.type(app, "ready")
        self.click(app, "Send \u21b5")
        self.assertIn("unsent draft", plain(app.render()))
        app.handle_key("y")
        self.assertEqual(primary.submitted, [])
        self.assertEqual(len(primary.received), 1)
        self.assertIn("ready", primary.received[0])
        self.assertNotIn("\r", primary.received[0])
        notes = [call for call in self.transport.calls if call["argv"] and str(call["argv"][0]).endswith("fm-inbox.sh")]
        self.assertEqual(notes, [])
        self.assertEqual(self.writers(), [])
        self.assertIn("not sent", app.status)


if __name__ == "__main__":
    unittest.main()
