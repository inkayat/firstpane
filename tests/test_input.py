"""Input decoding for the composer, the Changes tab and the lists."""

import unittest

from firstpane.input import InputDecoder, decode_input


class EditingKeyTests(unittest.TestCase):
    def test_editing_and_navigation_keys_have_names(self):
        # Break: Left/Right/Home/End/Delete/Backspace arrive as stray letters or esc, so the
        # composer cursor cannot move and Backspace inserts a control character.
        cases = {
            "\x1b[C": "right",
            "\x1b[D": "left",
            "\x1b[H": "home",
            "\x1b[F": "end",
            "\x1b[1~": "home",
            "\x1b[4~": "end",
            "\x1b[3~": "delete",
            "\x1b[5~": "pageup",
            "\x1b[6~": "pagedown",
            "\x1bOH": "home",
            "\x1bOF": "end",
            "\x7f": "backspace",
            "\x08": "backspace",
            "\t": "tab",
            "\x1b[Z": "backtab",
            "\x15": "ctrl-u",
            "\x0f": "ctrl-o",
        }
        for raw, name in cases.items():
            self.assertEqual(decode_input(raw), [("key", name)], repr(raw))

    def test_turkish_text_is_one_key_per_character(self):
        # Break: multi-byte characters are split into replacement characters.
        self.assertEqual(
            decode_input("çğış".encode()),
            [("key", "ç"), ("key", "ğ"), ("key", "ı"), ("key", "ş")],
        )

    def test_utf8_split_across_reads_is_reassembled(self):
        # Break: a read boundary inside "ğ" turns it into two U+FFFD characters.
        decoder = InputDecoder()
        raw = "ağ".encode()
        self.assertEqual(decoder.feed(raw[:2]), [("key", "a")])
        self.assertEqual(decoder.feed(raw[2:]), [("key", "ğ")])


class PasteTests(unittest.TestCase):
    def test_bracketed_paste_is_one_event_with_newlines_and_escapes_kept_raw(self):
        # Break: pasted text is replayed as keys (an "y" in the paste confirms a send,
        # a newline opens the preview), or the paste markers leak into the text.
        events = decode_input("\x1b[200~line y\nçift\x1b[31m\x1b[201~j")
        self.assertEqual(events, [("paste", "line y\nçift\x1b[31m"), ("key", "j")])

    def test_paste_split_across_reads_waits_for_its_end_marker(self):
        # Break: the first half of a long paste is delivered as keys.
        decoder = InputDecoder()
        self.assertEqual(decoder.feed("\x1b[200~first "), [])
        self.assertEqual(decoder.feed("second\x1b[201~"), [("paste", "first second")])


class MouseWheelTests(unittest.TestCase):
    def test_wheel_is_a_wheel_event_not_a_click(self):
        # Break: scrolling over the list clicks (activates) the row under the pointer.
        self.assertEqual(decode_input("\x1b[<64;5;6M"), [("wheel", "up", 5, 6)])
        self.assertEqual(decode_input("\x1b[<65;5;6M"), [("wheel", "down", 5, 6)])


class RangeGestureTests(unittest.TestCase):
    def test_shift_arrows_have_their_own_names(self):
        # Break: Shift+Down arrives as plain Down, so the keyboard cannot extend a diff range.
        self.assertEqual(
            decode_input("\x1b[1;2B\x1b[1;2A\x1b[B"),
            [("key", "shift-down"), ("key", "shift-up"), ("key", "down")],
        )

    def test_drag_and_shift_click_are_not_plain_clicks(self):
        # Break: dragging is dropped (or read as clicks that reset the range), a Shift-click
        # replaces the range instead of extending it, or a pointer moving with no button held is
        # read as a drag or a click.
        self.assertEqual(decode_input("\x1b[<32;5;6M"), [("drag", 5, 6)])
        self.assertEqual(decode_input("\x1b[<4;5;6M\x1b[<4;5;6m"), [("shift-click", 5, 6)])
        self.assertEqual(decode_input("\x1b[<35;5;6M"), [("hover", 5, 6)])



class BrowserInputTests(unittest.TestCase):
    def test_modified_keys_arrive_named_with_their_modifiers(self):
        # Break: while the page has the keyboard (kitty keyboard reporting), Cmd/Ctrl/Alt keys
        # arrive as junk or bare letters, so Cmd+L never focuses the address field and Ctrl-O
        # or Ctrl-C (reported as CSI u) no longer return to Firstpane or exit it.
        cases = {
            "\x1b[108;9u": "super-l",
            "\x1b[97;5u": "ctrl-a",
            "\x1b[111;5u": "ctrl-o",
            "\x1b[99;5u": "ctrl-c",
            "\x1b[120;3u": "alt-x",
            "\x1b[97;6u": "ctrl-shift-a",
            "\x1b[27u": "esc",
            "\x1b[13;2u": "shift-enter",
            "\x1b[1;5C": "ctrl-right",
            "\x1b[1;2A": "shift-up",
            "\x1b[3;5~": "ctrl-delete",
        }
        for raw, name in cases.items():
            self.assertEqual(decode_input(raw), [("key", name)], repr(raw))

    def test_terminal_answers_are_replies_not_keys(self):
        # Break: the cell size, a mode report or a kitty graphics answer is decoded as Esc plus
        # letters, which cancels screens and types into the composer.
        self.assertEqual(decode_input("\x1b[6;34;16t"), [("cell", 16, 34)])
        self.assertEqual(decode_input("\x1b[?1016;2$y"), [("mode", 1016, 2)])
        self.assertEqual(decode_input("\x1b_Gi=299;OK\x1b\\j"), [("graphics", 299, "OK"), ("key", "j")])
        self.assertEqual(decode_input("\x1b_Gi=300,p=1;ENOENT:no such file\x1b\\"), [("graphics", 300, "ENOENT:no such file")])

    def test_a_graphics_answer_split_across_reads_waits_for_its_end(self):
        # Break: half an answer is decoded as keys.
        decoder = InputDecoder()
        self.assertEqual(decoder.feed("\x1b_Gi=299;O"), [])
        self.assertEqual(decoder.feed("K\x1b\\"), [("graphics", 299, "OK")])

    def test_pixel_mouse_reports_carry_their_cell_and_pixel_position(self):
        # Break: in SGR-pixel mode a click is read as a huge cell number (lands nowhere) or
        # loses the pixel position the page needs for exact clicks and selection.
        decoder = InputDecoder()
        decoder.pixel_cell = (16, 34)
        self.assertEqual(decoder.feed("\x1b[<0;117;205M"), [("mouse", 0, 8, 7, True, (116, 204))])
        self.assertEqual(decoder.feed("\x1b[<32;181;207M"), [("drag", 12, 7, (180, 206))])
        self.assertEqual(decoder.feed("\x1b[<65;1;1M"), [("wheel", "down", 1, 1, (0, 0))])


if __name__ == "__main__":
    unittest.main()
