"""Herdr terminal frame decoder. Only the documented implementation vocabulary."""

import unittest

from firstpane.termframe import TermGrid

# From Herdr v0.9.5 render_ansi write_all_cells: text, an OSC 8 link, and reset-form SGR.
HERDR_ROW = (
    "\x1b[1;1H\x1b[0;39;49ma\x1b]8;;https://example.com\x1b\\b\x1b]8;;\x1b\\c"
    "\x1b[0;31;49md\x1b[0;39;49me\x1b[0m"
)


class TermFrameTests(unittest.TestCase):
    def test_known_frame_keeps_text_and_drops_the_link(self):
        # Break: the link URL is drawn, or SGR from Herdr's encoder is lost.
        grid = TermGrid()
        self.assertIsNone(grid.apply(HERDR_ROW.encode(), full=True, width=5, height=1))
        rows = grid.rows(show_cursor=False)
        self.assertEqual(len(rows), 1)
        self.assertIn("a", rows[0])
        self.assertIn("b", rows[0])
        self.assertNotIn("example.com", rows[0])
        self.assertIn("0;31;49", rows[0])

    def test_zero_byte_and_full_blank_are_valid(self):
        # Break: an empty frame is treated as a broken stream and the shell is dropped.
        grid = TermGrid()
        grid.apply(b"\x1b[1;1Hhello", full=True, width=5, height=1)
        self.assertIsNone(grid.apply(b"", full=False, width=5, height=1))
        self.assertIn("hello", grid.rows(False)[0])
        self.assertIsNone(grid.apply(b"", full=True, width=5, height=1))
        self.assertNotIn("hello", grid.rows(False)[0])

    def test_unknown_escape_is_an_error(self):
        # Break: an undocumented control is guessed and drawn as text.
        grid = TermGrid()
        reason = grid.apply(b"\x1b[K", full=True, width=4, height=1)
        self.assertEqual(reason, "unsupported")

    def test_wide_character_uses_two_cells(self):
        # Break: a wide glyph overwrites the next cell.
        grid = TermGrid()
        self.assertIsNone(grid.apply("\x1b[1;1H名x".encode(), full=True, width=3, height=1))
        text = grid.rows(False)[0]
        self.assertIn("名", text)
        self.assertIn("x", text)
