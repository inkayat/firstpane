"""Live terminal input through a pty: escape sequences must arrive whole."""

import io
import os
import unittest

from firstpane.__main__ import TtyTerminal
from firstpane.input import InputDecoder, decode_input


class TtyInputTests(unittest.TestCase):
    def setUp(self):
        self.master, slave = os.openpty()
        self.stdin = os.fdopen(slave, "r")
        self.out = io.StringIO()
        self.terminal = TtyTerminal(stdin=self.stdin, stdout=self.out)
        self.terminal.enter_raw()
        self.out.truncate(0)
        self.out.seek(0)

    def tearDown(self):
        self.terminal.leave_raw()
        self.stdin.close()
        os.close(self.master)

    def read_events(self, payload):
        os.write(self.master, payload)
        return decode_input(self.terminal.read_input(1))

    def test_mouse_press_arrives_as_a_click_not_a_stray_escape(self):
        # Break: a buffered one-char read leaves the rest of the sequence behind and returns esc.
        self.assertEqual(self.read_events(b"\x1b[<0;10;5M"), [("mouse", 0, 10, 5, True)])
        self.assertEqual(self.terminal.read_input(0.05), b"")

    def test_arrow_keys_and_a_following_key_in_one_burst(self):
        # Break: an arrow decodes as esc and the tail is delivered as junk keys.
        self.assertEqual(
            self.read_events(b"\x1b[A\x1b[Bj"),
            [("key", "up"), ("key", "down"), ("key", "j")],
        )

    def test_mouse_then_key_in_one_burst_keeps_both(self):
        # Break: the key typed right after a click is swallowed or reordered.
        self.assertEqual(
            self.read_events(b"\x1b[<0;3;2Mj"),
            [("mouse", 0, 3, 2, True), ("key", "j")],
        )

    def test_left_and_right_arrows_are_named_keys_not_escape(self):
        # Break: left/right arrows decode as esc, [ and a letter, which cancels a screen.
        self.assertEqual(self.read_events(b"\x1b[C\x1b[D"), [("key", "right"), ("key", "left")])

    def test_split_utf8_read_is_not_replaced(self):
        # Break: a read boundary inside a Turkish letter is decoded early into U+FFFD.
        raw = "ş".encode()
        os.write(self.master, raw[:1])
        first = self.terminal.read_input(1)
        os.write(self.master, raw[1:])
        second = self.terminal.read_input(1)
        decoder = InputDecoder()
        self.assertEqual(decoder.feed(first), [])
        self.assertEqual(decoder.feed(second), [("key", "ş")])

    def test_lone_escape_is_escape_and_idle_is_empty(self):
        # Break: a plain Esc is dropped, or an idle poll is reported as end of input.
        self.assertEqual(self.read_events(b"\x1b"), [("key", "esc")])
        self.assertEqual(self.terminal.read_input(0.05), b"")

    def test_ctrl_o_ctrl_v_and_ctrl_s_reach_the_pane(self):
        # Break: the tty driver swallows ^O (discard), ^V (literal next) or ^S (flow
        # control), so those keys never reach Firstpane's composer and lists.
        self.assertEqual(
            self.read_events(b"\x0f\x16\x13x"),
            [("key", "ctrl-o"), ("key", "ctrl-v"), ("key", "ctrl-s"), ("key", "x")],
        )

    def test_closed_input_is_end_of_input(self):
        # Break: hangup spins forever instead of leaving the pane process.
        os.close(self.master)
        self.master = os.open(os.devnull, os.O_RDONLY)
        self.assertIsNone(self.terminal.read_input(1))

    def test_only_changed_lines_are_rewritten_and_resize_clears(self):
        # Break: the idle pane repaints and flickers, a one-line change clears the whole
        # screen, or a resize leaves stale cells behind.
        self.terminal.write("one\nimage\nthree", (10, 40))
        start = len(self.out.getvalue())
        self.terminal.write("one\nimage\nthree", (10, 40))
        self.assertEqual(len(self.out.getvalue()), start)
        self.terminal.write("one\nimage\nTHREE", (10, 40))
        changed = self.out.getvalue()[start:]
        self.assertEqual(changed, "\x1b[3;1H\x1b[0m\x1b[2KTHREE")
        self.terminal.write("one\nimage\nTHREE", (12, 40))
        self.assertEqual(self.out.getvalue().count("\x1b[2J"), 2)
        self.assertTrue(self.out.getvalue().endswith("THREE"))

    def test_the_frame_is_repainted_after_the_editor_round_trip(self):
        # Break: the unchanged frame stays hidden behind the editor's screen after it exits.
        self.terminal.write("same", (10, 40))
        self.terminal.leave_raw()
        self.terminal.enter_raw()
        self.terminal.write("same", (10, 40))
        self.assertEqual(self.out.getvalue().count("\x1b[2J"), 2)
