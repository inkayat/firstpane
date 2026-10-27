"""Official harness icons for the Agents rows, drawn as kitty graphics.

Each known harness has one bundled image (firstpane/icons/, provenance in SOURCES.md). Firstpane
transmits every image once, directly in the tty stream, as a virtual placement of two cells by
one row (`U=1`), and a row shows its icon by drawing the two unicode placeholder cells that name
the image. Placeholders are ordinary text, so icons scroll, clip, get selected and disappear with
the row text; nothing is ever placed at a screen position that could go stale.

Herdr stretches a placeholder image to its cell box (aspect ratio lost), so the square logo is
letterboxed with transparent pixels to the box's pixel aspect before it is sent, and sent again
only when the cell size changes. A row draws an icon only after the terminal answered `OK` for
that image; until then, after a refusal, or when no answer arrives within TIMEOUT, the row names
the harness instead, and the pane says once that the terminal shows no kitty graphics.
"""

import base64
import struct
import zlib
from pathlib import Path

from firstpane.kitty import placeholder_rows

ICON_DIR = Path(__file__).with_name("icons")
# harness id: (image file, kitty image id). Ids sit next to the in-tab browser's 0x7E4301.
HARNESS_ICONS = {
    "pi": ("pi.png", 0x7E4311),
    "omp": ("omp.png", 0x7E4312),
    "claude": ("claude.png", 0x7E4313),
    "grok": ("grok.png", 0x7E4314),
    "codex": ("codex.png", 0x7E4315),
}
SLOT_COLS = 2
SLOT_ROWS = 1
TIMEOUT = 1.5
CHUNK = 4096
NO_GRAPHICS = "harness icons need kitty graphics, which this terminal does not show: harnesses are named"


def decode_png(data):
    """(width, height, RGBA bytes) of an 8-bit RGBA non-interlaced PNG (the bundled form)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    at, idat, head = 8, [], None
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        kind, body = data[at + 4 : at + 8], data[at + 8 : at + 8 + length]
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        at += 12 + length
    if head is None or head[2:] != (8, 6, 0, 0, 0):
        raise ValueError("bundled icons are 8-bit RGBA, non-interlaced")
    w, h = head[0], head[1]
    raw = zlib.decompress(b"".join(idat))
    stride = w * 4
    out = bytearray()
    prev = bytearray(stride)
    for y in range(h):
        kind = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b = prev[i]
            c = prev[i - 4] if i >= 4 else 0
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line
        prev = line
    return w, h, bytes(out)


def letterbox(w, h, rgba, box_w, box_h):
    """The image centred in transparent margins so its aspect is box_w:box_h."""
    if box_w * h >= box_h * w:
        new_w, new_h = max(w, round(h * box_w / box_h)), h
    else:
        new_w, new_h = w, max(h, round(w * box_h / box_w))
    left, top = (new_w - w) // 2, (new_h - h) // 2
    blank = bytes(new_w * 4)
    rows = [blank] * top
    for y in range(h):
        rows.append(bytes(left * 4) + rgba[y * w * 4 : (y + 1) * w * 4] + bytes((new_w - w - left) * 4))
    rows += [blank] * (new_h - h - top)
    return new_w, new_h, b"".join(rows)


def transmit(image_id, w, h, rgba):
    """Kitty commands that send RGBA pixels (zlib, direct) as a 2x1-cell virtual placement."""
    payload = base64.b64encode(zlib.compress(rgba)).decode()
    chunks = [payload[at : at + CHUNK] for at in range(0, len(payload), CHUNK)] or [""]
    out = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            keys = f"a=T,U=1,i={image_id},f=32,s={w},v={h},o=z,c={SLOT_COLS},r={SLOT_ROWS},m={more}"
        else:
            keys = f"m={more}"
        out.append(f"\x1b_G{keys};{chunk}\x1b\\")
    return "".join(out)


def delete(image_id):
    return f"\x1b_Ga=d,d=I,i={image_id}\x1b\\"


class HarnessIcons:
    """Which harness images the pane's terminal holds, and the bytes that keep them current."""

    def __init__(self, clock, enabled=True):
        self.clock = clock
        self.enabled = enabled
        self.box = None  # (width, height) in pixels of the slot the images were sent for
        self.sent_at = None
        self.ready = set()
        self.refused = set()
        self.unavailable = False
        self._pixels = {}

    def _image(self, name):
        if name not in self._pixels:
            self._pixels[name] = decode_png((ICON_DIR / name).read_bytes())
        return self._pixels[name]

    def pending(self, cell):
        """Bytes to send for a pane whose cells are `cell` (w, h) px: every image, the first
        time and whenever the slot's pixel aspect changes; otherwise nothing."""
        if not self.enabled:
            return b""
        box = (cell[0] * SLOT_COLS, cell[1] * SLOT_ROWS)
        if box == self.box:
            return b""
        self.box = box
        if self.sent_at is None:
            self.sent_at = self.clock()
        out = []
        for name, image_id in HARNESS_ICONS.values():
            out.append(transmit(image_id, *letterbox(*self._image(name), *box)))
        return "".join(out).encode()

    def answer(self, image_id, message):
        """A terminal answer about one image; answers about other images are not ours."""
        if image_id not in {image_id for _, image_id in HARNESS_ICONS.values()}:
            return
        if message == "OK":
            self.ready.add(image_id)
            self.refused.discard(image_id)
            self.unavailable = False
        else:
            self.ready.discard(image_id)
            self.refused.add(image_id)

    def poll(self):
        """True once, when the images were sent and none was accepted: every one refused, or
        no acceptance within TIMEOUT."""
        if self.unavailable or self.sent_at is None or self.ready:
            return False
        if len(self.refused) == len(HARNESS_ICONS) or self.clock() - self.sent_at >= TIMEOUT:
            self.unavailable = True
            return True
        return False

    def cells(self, harness):
        """The two placeholder cells that draw `harness`'s icon, or None where it cannot show."""
        entry = HARNESS_ICONS.get(harness)
        if entry is None or entry[1] not in self.ready:
            return None
        return placeholder_rows(entry[1], SLOT_ROWS, SLOT_COLS)[0]

    def release(self):
        """Bytes that free every image this pane sent (at exit)."""
        if self.box is None:
            return b""
        return "".join(delete(image_id) for _, image_id in HARNESS_ICONS.values()).encode()
