"""Official harness icons on Agents rows: kitty graphics images that Firstpane transmits once and
draws with unicode placeholders in the icon slot of each row's second line. A shown icon stands
alone before model and effort; the harness name is the fallback only where the image cannot be
shown. Each test names the production break it exists to catch."""

import base64
import io
import re
import zlib

from firstpane.__main__ import TtyTerminal
from firstpane.cells import width
from firstpane.kitty import DIACRITICS
from tests.test_agent_identity import IdentityFixture
from tests.test_panel import plain
from tests.vtscreen import Screen

PLACEHOLDER = "\U0010EEEE"
APC = re.compile(r"\x1b_G([^;\x1b]*)(?:;([^\x1b]*))?\x1b\\")
CELL_RUN = re.compile("\x1b\\[38;2;(\\d+);(\\d+);(\\d+)m((?:\U0010EEEE..)+)\x1b\\[39m", re.S)
ICON = re.compile("\U0010EEEE..", re.S)
SILENCE = 5  # seconds without any terminal answer; far beyond any graphics reply


def keys_of(head):
    return dict(part.split("=", 1) for part in head.split(",") if "=" in part)


def images(data):
    """{image id: (keys, width, height, rgba)} of the kitty a=T images in `data`, chunks joined."""
    text = data.decode() if isinstance(data, bytes) else data
    found, current, payload = {}, None, ""
    for head, body in APC.findall(text):
        keys = keys_of(head)
        if current is None:
            if keys.get("a") != "T":
                continue
            current, payload = keys, ""
        payload += body
        if keys.get("m", "0") == "0":
            raw = base64.b64decode(payload)
            if current.get("o") == "z":
                raw = zlib.decompress(raw)
            w, h = int(current["s"]), int(current["v"])
            found[int(current["i"])] = (current, w, h, raw)
            current = None
    return found


def pixel(image, x, y):
    _, w, _, raw = image
    at = (y * w + x) * 4
    return tuple(raw[at : at + 4])


def close(actual, expected, slack=12):
    return all(abs(a - b) <= slack for a, b in zip(actual, expected))


def shown(text):
    """`text` with each icon placeholder cell (placeholder + row + column marks) drawn as `#`."""
    return ICON.sub("#", text)


class IconFixture(IdentityFixture):
    def known_harnesses(self):
        """pi on a Grok model, omp on an OpenAI model, Claude, Grok and Codex agents."""
        self.panes["alpha-task"]["agent"] = "pi"
        self.panes["beta-task"]["agent"] = "omp"
        self.bind("alpha-task", harness="pi", model="xai/grok-4.7", effort="high", worktree=str(self.wt_a))
        self.bind("beta-task", harness="omp", model="openai-codex/gpt-5.6", effort="xhigh", worktree=str(self.wt_b))
        self.add_agent("gamma-task", 3, "claude", harness="claude", model="anthropic/claude-opus-4-6", effort="medium")
        self.add_agent("delta-task", 4, "grok", harness="grok", model="xai/grok-4", effort="low")
        self.add_agent("echo-task", 5, "codex", harness="codex", model="openai/gpt-5", effort="high")

    def sent(self, app):
        """What the main loop would write to the pane tty now."""
        return app.pump([], self.clock.t)

    def answer(self, app, image_ids, message="OK"):
        for image_id in image_ids:
            app.handle_bytes(f"\x1b_Gi={image_id};{message}\x1b\\".encode())

    def accepted(self, app, cell=(10, 20)):
        """Transmit the icons for `cell` and let the terminal accept every one of them."""
        app.cell_size(*cell)
        sent = images(self.sent(app))
        self.answer(app, sent)
        return sent

    def raw_line(self, app, title, offset=1):
        lines = app.render().split("\n")
        at = next(index for index, line in enumerate(lines) if title in plain(line))
        return lines[at + offset]

    def icon_of(self, app, title):
        """(image id, [(row, column)] of its cells) the identity line of `title` places."""
        runs = CELL_RUN.findall(self.raw_line(app, title))
        if not runs:
            return None
        (r, g, b, cells), = runs
        marks = [(DIACRITICS.index(cell[1]), DIACRITICS.index(cell[2])) for cell in ICON.findall(cells)]
        return (int(r) << 16) | (int(g) << 8) | int(b), marks


class IconRowTests(IconFixture):
    def test_each_harness_row_shows_its_own_official_image_alone_before_model_and_effort(self):
        # Break: the harness name is still printed beside a working image, an image is swapped
        # between harnesses, the model provider picks the icon (pi on a Grok model shown as
        # Grok, omp on an OpenAI model shown as Codex), or a row places an image the terminal
        # was never sent.
        self.known_harnesses()
        app = self.app(rows=40)
        sent = self.accepted(app)
        self.assertEqual(len(sent), 5)
        rows = {
            "Alpha task": ("## grok-4.7 \u00b7 effort: high", [((2, 2), (240, 144, 130, 255)), ((2, 30), (77, 154, 191, 255)),
                                                              ((61, 61), (241, 190, 88, 255)), ((61, 2), (0, 0, 0, 0))]),
            # omp's primary logo (omp.sh): gradient bar pink at the left, right leg blue at the
            # foot, the dark plate between the two legs, transparent rounded corners.
            "Beta task": ("## gpt-5.6 \u00b7 effort: xhigh", [((16, 18), (226, 74, 199, 255)), ((36, 52), (120, 152, 242, 255)),
                                                             ((28, 40), (15, 10, 20, 255)), ((2, 2), (0, 0, 0, 0))]),
            "Gamma task": ("## claude-opus-4-6 \u00b7 effort: medium", [((32, 32), (217, 119, 87, 255)), ((2, 2), (0, 0, 0, 0))]),
            "Delta task": ("## grok-4 \u00b7 effort: low", [((20, 32), (252, 252, 252, 255)), ((2, 2), (5, 5, 5, 255))]),
            # Codex's icon (OpenAI's Codex extension): white Blossom strokes on a black square.
            "Echo task": ("## gpt-5 \u00b7 effort: high", [((25, 6), (255, 255, 255, 255)), ((10, 36), (255, 255, 255, 255)),
                                                         ((32, 32), (0, 0, 0, 255))]),
        }
        used = set()
        for title, (line, probes) in rows.items():
            self.assertEqual(shown(self.identity(app, title)), line)
            image_id, marks = self.icon_of(app, title)
            self.assertEqual(marks, [(0, 0), (0, 1)])
            self.assertIn(image_id, sent)
            used.add(image_id)
            keys, w, h, _ = sent[image_id]
            self.assertEqual((keys.get("U"), keys.get("c"), keys.get("r"), w, h), ("1", "2", "1", 64, 64))
            for (x, y), color in probes:
                self.assertTrue(close(pixel(sent[image_id], x, y), color), (title, x, y, pixel(sent[image_id], x, y)))
        self.assertEqual(len(used), 5)
        identities = " ".join(self.identity(app, title) for title in rows)
        for name in ("pi \u00b7", "omp", "Claude", "Grok", "Codex", "codex"):
            self.assertNotIn(name, identities)

    def test_an_unknown_harness_has_no_official_image_and_keeps_its_name(self):
        # Break: opencode (or any other unmapped id) gets one of the known harness images or a
        # blank slot instead of its name.
        self.known_harnesses()
        self.add_agent("foxtrot-task", 6, "opencode", harness="opencode", model="openai/gpt-5", effort="high")
        app = self.app(rows=40)
        self.accepted(app)
        self.assertIsNone(self.icon_of(app, "Foxtrot task"))
        self.assertEqual(self.identity(app, "Foxtrot task"), "opencode \u00b7 gpt-5 \u00b7 effort: high")


class IconFallbackTests(IconFixture):
    def test_before_and_without_a_graphics_answer_rows_name_their_harness_and_the_status_says_so(self):
        # Break: placeholder cells are drawn for images the terminal never confirmed (blank
        # slots, no name), or the missing image support is not disclosed.
        self.known_harnesses()
        app = self.app(rows=40)
        app.cell_size(10, 20)
        self.assertEqual(len(images(self.sent(app))), 5)
        self.assertNotIn(PLACEHOLDER, app.render())
        self.assertEqual(self.identity(app, "Alpha task"), "pi \u00b7 grok-4.7 \u00b7 effort: high")
        self.clock.t += SILENCE
        app.refresh()
        frame = app.render()
        self.assertNotIn(PLACEHOLDER, frame)
        self.assertEqual(self.identity(app, "Beta task"), "omp \u00b7 gpt-5.6 \u00b7 effort: xhigh")
        self.assertEqual(self.identity(app, "Gamma task"), "Claude \u00b7 claude-opus-4-6 \u00b7 effort: medium")
        self.assertEqual(self.identity(app, "Delta task"), "Grok \u00b7 grok-4 \u00b7 effort: low")
        self.assertEqual(self.identity(app, "Echo task"), "Codex \u00b7 gpt-5 \u00b7 effort: high")
        self.assertIn("kitty graphics", plain(frame))

    def test_a_refused_image_falls_back_to_the_name_of_its_harness_only(self):
        # Break: one refused image blanks every row, or its row draws cells the terminal refused.
        self.known_harnesses()
        app = self.app()
        app.cell_size(10, 20)
        sent = images(self.sent(app))
        # Claude's image: the Claude orange spark at the center of a transparent-cornered plate.
        refused = next(i for i, image in sent.items() if close(pixel(image, 32, 32), (217, 119, 87, 255)) and pixel(image, 2, 2)[3] == 0)
        self.answer(app, [image_id for image_id in sent if image_id != refused])
        self.answer(app, [refused], "EINVAL:bad image")
        self.assertIsNone(self.icon_of(app, "Gamma task"))
        self.assertEqual(self.identity(app, "Gamma task"), "Claude \u00b7 claude-opus-4-6 \u00b7 effort: medium")
        self.assertIsNotNone(self.icon_of(app, "Alpha task"))

    def test_no_color_sends_no_images_and_keeps_names(self):
        # Break: NO_COLOR panes still receive colored logos or escape sequences.
        self.known_harnesses()
        app = self.app(env={"NO_COLOR": "1"})
        app.cell_size(10, 20)
        self.assertEqual(images(self.sent(app)), {})
        frame = app.render()
        self.assertNotIn("\x1b", frame)
        self.assertEqual(self.identity(app, "Alpha task"), "pi \u00b7 grok-4.7 \u00b7 effort: high")


class IconSizeTests(IconFixture):
    def test_the_image_is_letterboxed_to_the_two_cell_slot_and_resent_only_when_the_cell_changes(self):
        # Break: Herdr stretches the square logo to a non-square slot (aspect lost), the image
        # is sent again on every loop, or a new cell size keeps the old letterbox.
        self.known_harnesses()
        app = self.app()
        app.cell_size(8, 20)  # slot 16 x 20 px: 4 wide to 5 high
        sent = images(self.sent(app))
        pi = next(image for image in sent.values() if close(pixel(image, 2, 10), (240, 144, 130, 255)))
        self.assertEqual(pi[1:3], (64, 80))
        self.assertEqual(pixel(pi, 2, 2), (0, 0, 0, 0))  # transparent band above the logo
        self.assertTrue(close(pixel(pi, 61, 69), (241, 190, 88, 255)))
        self.assertEqual(pixel(pi, 61, 77), (0, 0, 0, 0))  # and below it
        self.assertEqual(images(self.sent(app)), {})
        app.cell_size(8, 20)
        self.assertEqual(images(self.sent(app)), {})
        app.cell_size(12, 20)  # slot 24 x 20 px: 6 wide to 5 high
        again = images(self.sent(app))
        self.assertEqual(set(again), set(sent))
        self.assertEqual({image[1:3] for image in again.values()}, {(77, 64)})

    def test_narrow_panes_never_split_an_icon_or_overflow(self):
        # Break: a narrow pane cuts the two-cell icon in half or pushes a line past the pane.
        self.known_harnesses()
        self.bind("beta-task", harness="omp", model="vendor/" + "very-long-model-name-" * 5, effort="xhigh", worktree=str(self.wt_b))
        for rows, cols in ((30, 100), (24, 56), (20, 44), (16, 36), (12, 30)):
            app = self.app(rows=rows, cols=cols)
            self.accepted(app)
            lines = self.lines(app)
            self.assertTrue(all(width(line) <= cols for line in lines), (cols, lines))
            for line in lines:
                self.assertIn(line.count(PLACEHOLDER), (0, 2), (cols, line))
            beta = shown(self.identity(app, "Beta task"))
            self.assertTrue(beta.startswith("## very-long"), (cols, beta))


class IconScreenTests(IconFixture):
    def screen_after(self, app, terminal, screen):
        terminal.write(app.render(), (app.rows, app.cols))
        screen.feed(terminal.stdout.getvalue())
        terminal.stdout.seek(0)
        terminal.stdout.truncate()
        return [line.count(PLACEHOLDER) for line in screen.lines()]

    def test_scrolling_and_leaving_for_browser_leave_no_stale_or_duplicate_icons(self):
        # Break: icon cells stay on screen after the rows scroll away or the Browser tab
        # replaces the list, or an icon is drawn twice after returning to Agents.
        self.known_harnesses()
        app = self.app(rows=16, cols=80)
        terminal = TtyTerminal(stdout=io.StringIO())
        screen = Screen(app.rows, app.cols)
        self.accepted(app)
        counts = self.screen_after(app, terminal, screen)
        self.assertTrue(set(counts) <= {0, 2}, counts)
        before = sum(counts)
        self.assertGreater(before, 0)
        for _ in range(12):
            app.handle_key("j")
        counts = self.screen_after(app, terminal, screen)
        self.assertTrue(set(counts) <= {0, 2}, counts)
        visible = sum(1 for line in self.lines(app) if PLACEHOLDER in line)
        self.assertEqual(sum(counts), 2 * visible)
        app.handle_key("3")
        self.assertEqual(sum(self.screen_after(app, terminal, screen)), 0)
        app.handle_key("1")
        counts = self.screen_after(app, terminal, screen)
        self.assertTrue(set(counts) <= {0, 2}, counts)
        self.assertEqual(sum(counts), 2 * sum(1 for line in self.lines(app) if PLACEHOLDER in line))
