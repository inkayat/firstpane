"""Shared diff viewport. Motions, line selection and jump-back, with no I/O."""

import unittest

from firstpane.diffview import NO_SYMBOL, apply_key, push_jump
from firstpane.workspace import DiffLine


def view(text="alpha beta  gamma", cursor=0):
    return {
        "lines": [DiffLine(" ", 1, 1, text), DiffLine("+", None, 2, "next line here")],
        "cursor": cursor,
        "xoff": 0,
        "mark": None,
        "col": 0,
    }


class DiffViewTests(unittest.TestCase):
    def test_horizontal_and_word_motions_stay_on_the_line(self):
        # Break: h/l ignore the line, or w/b jump to another line.
        item = view()
        apply_key(item, "l", rows=24)
        self.assertEqual(item["xoff"], 8)
        apply_key(item, "0", rows=24)
        self.assertEqual((item["col"], item["xoff"]), (0, 0))
        apply_key(item, "w", rows=24)
        self.assertEqual(item["col"], 6)  # "beta"
        apply_key(item, "w", rows=24)
        self.assertEqual(item["col"], 12)  # "gamma"
        apply_key(item, "b", rows=24)
        self.assertEqual(item["col"], 6)
        apply_key(item, "$", rows=24)
        self.assertEqual(item["col"], len("alpha beta  gamma"))
        self.assertEqual(item["cursor"], 0)

    def test_vertical_page_and_edges(self):
        # Break: j/k or Ctrl-d walk off the diff, or G/gg miss the ends.
        item = view()
        apply_key(item, "j", rows=24)
        self.assertEqual(item["cursor"], 1)
        apply_key(item, "j", rows=24)
        self.assertEqual(item["cursor"], 1)
        apply_key(item, "ctrl-u", rows=24)
        self.assertEqual(item["cursor"], 0)
        apply_key(item, "G", rows=24)
        self.assertEqual(item["cursor"], 1)
        apply_key(item, "g", rows=24)
        apply_key(item, "g", rows=24)
        self.assertEqual(item["cursor"], 0)

    def test_v_and_V_select_lines_and_esc_clears(self):
        # Break: v/V select characters, or Esc leaves a range that a message would send.
        item = view()
        apply_key(item, "v", rows=24)
        self.assertEqual(item["mark"], 0)
        apply_key(item, "j", rows=24)
        self.assertEqual(item["mark"], 0)
        self.assertEqual(item["cursor"], 1)
        apply_key(item, "V", rows=24)
        self.assertEqual(item["mark"], 0)
        apply_key(item, "esc", rows=24)
        self.assertIsNone(item["mark"])
        apply_key(item, "V", rows=24)
        self.assertEqual(item["mark"], 1)

    def test_gd_and_gI_report_no_symbol_support_without_moving(self):
        # Break: gd/gI move the cursor or pretend a definition was opened.
        item = view()
        item["cursor"] = 1
        item["xoff"] = 4
        for key in ("d", "I"):
            again = view()
            again["cursor"] = 1
            again["xoff"] = 4
            apply_key(again, "g", rows=24)
            result = apply_key(again, key, rows=24)
            self.assertEqual(result["status"], NO_SYMBOL)
            self.assertEqual((again["cursor"], again["xoff"]), (1, 4))

    def test_ctrl_o_restores_the_pushed_position(self):
        # Break: Ctrl-o drops the jump or returns a different file position.
        item = view()
        push_jump(item, ("workspace", "uncommitted", "src/a.py", 3, 9))
        item["cursor"] = 1
        result = apply_key(item, "ctrl-o", rows=24)
        self.assertEqual(result["jumped"], ("workspace", "uncommitted", "src/a.py", 3, 9))
        self.assertEqual(apply_key(item, "ctrl-o", rows=24)["status"], "no earlier position")
