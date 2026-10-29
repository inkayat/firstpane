"""New empty-shell control session. Fake popen only; never the user's Herdr."""

import json
import unittest

from firstpane.terminal_view import TerminalControl


class _Pipe:
    def __init__(self):
        self.chunks = []
        self.closed = False

    def write(self, data):
        self.chunks.append(data)
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _Child:
    def __init__(self):
        self.pid = 90
        self.stdin = _Pipe()
        self._gone = False
        self.killed = []

    def poll(self):
        return 0 if self._gone else None

    def wait(self, timeout=None):
        self._gone = True
        return 0


class TerminalControlTests(unittest.TestCase):
    def test_start_does_not_take_over_and_resize_is_one_command(self):
        # Break: the control session uses --takeover, or a resize is sent as raw keystrokes.
        seen = {}

        def popen(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _Child()

        control = TerminalControl(popen)
        control.start("herdr", "term-1", 80, 24, {"PATH": "/bin"})
        self.assertEqual(seen["argv"], ["herdr", "terminal", "session", "control", "term-1", "--cols", "80", "--rows", "24"])
        self.assertNotIn("--takeover", seen["argv"])
        self.assertFalse(seen["kwargs"]["shell"])
        self.assertTrue(seen["kwargs"]["start_new_session"])
        control.resize(90, 20)
        sent = json.loads(control.child.stdin.chunks[-1].decode())
        self.assertEqual(sent, {"type": "terminal.resize", "cols": 90, "rows": 20})

    def test_paste_is_one_input_and_ctrl_o_is_not_sent(self):
        # Break: a paste is split, or Ctrl-O is delivered to the shell.
        control = TerminalControl(lambda *args, **kwargs: _Child())
        control.start("herdr", "term-1", 40, 12, {})
        kept = control.feed_keys(b"\x1b[200~hi\x0f\x1b[201~")
        self.assertIsNone(kept)
        sent = json.loads(control.child.stdin.chunks[-1].decode())
        self.assertEqual(sent["type"], "terminal.input")
        self.assertEqual(sent["text"], "hi\x0f")
        returned = control.feed_keys(b"\x0f")
        self.assertEqual(returned, "firstpane")
        self.assertEqual(len(control.child.stdin.chunks), 1)

    def test_release_does_not_send_cancel_keys_and_close_is_separate(self):
        # Break: leaving the tab sends Ctrl-C, or close happens without an explicit y.
        killed = []
        control = TerminalControl(lambda *args, **kwargs: _Child(), kill=lambda pid, sig: killed.append((pid, sig)))
        control.start("herdr", "term-1", 40, 12, {})
        control.release()
        sent = [json.loads(chunk.decode()) for chunk in control.stdin.chunks]
        self.assertEqual(sent, [{"type": "terminal.release"}])
        self.assertTrue(control.stdin.closed)
        self.assertFalse(control.closed_shell)
        control.confirm_close()
        self.assertTrue(control.closed_shell)
