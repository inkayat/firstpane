"""In-tab browser helpers: frame coalescing and quiet windows, CDP-only capture, child env.

Each test names the production break it exists to catch. Frames, registry records and CDP
replies are hand-built fixtures shaped like terminal-browser 0.13.4 output; shared-memory frames
name real POSIX shared-memory objects this test creates and removes.
"""

import _posixshmem
import base64
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from firstpane.embed import LAYOUT_QUIET, CaptureError, FrameStream, browser_env, capture_context

IMAGE = 32322


def frame(n, image=IMAGE, chunks=3, medium="d", size=(1952, 1190), repeat=40):
    """One complete chunked a=T transmission, shaped like pixel-core kitty_transmit_placed."""
    body = base64.b64encode(f"frame-{n:04d}-".encode() * repeat).decode()
    step = -(-len(body) // chunks)
    parts = [body[i:i + step] for i in range(0, len(body), step)]
    out = []
    for i, part in enumerate(parts):
        more = int(i != len(parts) - 1)
        if i == 0:
            keys = (f"a=T,f=32,o=z,s={size[0]},v={size[1]},t={medium},i={image},"
                    f"U=1,c=122,r=35,q=2,m={more}")
        else:
            keys = f"m={more}"
        out.append(f"\x1b_G{keys};{part}\x1b\\".encode())
    return b"".join(out)


def delete(image=IMAGE):
    return f"\x1b_Ga=d,d=I,i={image},q=2\x1b\\".encode()


def named(name, image=IMAGE, medium="s", size=(1904, 1564)):
    """One a=T frame naming a shared-memory object (t=s) or file (t=f), as kitty_transmit_named."""
    payload = base64.b64encode(name.encode()).decode()
    return (f"\x1b_Ga=T,f=32,s={size[0]},v={size[1]},t={medium},i={image},U=1,c=119,r=46,q=2;"
            f"{payload}\x1b\\").encode()


def shm_exists(name):
    try:
        os.close(_posixshmem.shm_open(name, os.O_RDONLY, mode=0o600))
    except FileNotFoundError:
        return False
    return True


def shm_remove(name):
    """Unlink `name` as the terminal would after reading it; already gone is fine."""
    try:
        _posixshmem.shm_unlink(name)
    except FileNotFoundError:
        pass


class SharedMemoryFrames:
    """Real shared-memory objects named like terminal-browser's, one per frame; whatever is left
    at the end (what the terminal would have read and unlinked) is removed."""

    def __init__(self, test, count):
        tag = f"/fpt-{os.getpid()}-{os.urandom(3).hex()}"
        self.names = [f"{tag}-{n}" for n in range(count)]
        for name in self.names:
            fd = _posixshmem.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode=0o600)
            os.ftruncate(fd, 4)
            os.close(fd)
        test.addCleanup(lambda: [shm_remove(name) for name in self.names])


def run_stream(stream, arrivals, end_ms, tick_ms=10):
    """Feed `arrivals` ({ms: bytes}) and poll `due` every tick, as the host loop does; returns
    [(seconds, bytes)] for every non-empty write."""
    emitted = []
    for ms in range(0, end_ms + 1, tick_ms):
        now = ms / 1000
        out = stream.feed(arrivals[ms], now) if ms in arrivals else b""
        out += stream.due(now)
        if out:
            emitted.append((now, out))
    return emitted


class FrameStreamTests(unittest.TestCase):
    def test_burst_is_forwarded_at_most_twice_per_second_and_ends_on_the_latest_frame(self):
        # Break: every superseded frame is forwarded (the 9 fps agent-glow burst reaches Herdr),
        # or the trailing frame is dropped so the pane stalls on an old image.
        stream = FrameStream(IMAGE)
        frames = [frame(n) for n in range(90)]
        arrivals = {round(n * 100 / 9) * 10: n for n in range(90)}  # 9 fps for 10 s, in ms ticks
        emitted = []
        for ms in range(0, 11001, 10):  # the host loop wakes at least every 10 ms here
            now = ms / 1000
            out = stream.feed(frames[arrivals[ms]], now) if ms in arrivals else b""
            out += stream.due(now)
            if out:
                emitted.append((now, out))
        self.assertLessEqual(len(emitted), 21)
        self.assertGreaterEqual(len(emitted), 19)
        order = [frames.index(out) for _, out in emitted]  # whole input frames only, never mixed
        self.assertEqual(order, sorted(order))
        gaps = [b[0] - a[0] for a, b in zip(emitted, emitted[1:])]
        self.assertGreaterEqual(min(gaps), 0.5 - 1e-9)
        last_time, last = emitted[-1]
        self.assertEqual(last, frames[89])
        self.assertLessEqual(last_time, 9.89 + 0.5)
        self.assertIsNone(stream.deadline())

    def test_low_rate_frames_are_forwarded_immediately(self):
        # Break: coalescing delays or drops legitimate one-per-second updates (typing, navigation).
        stream = FrameStream(IMAGE)
        for n in range(5):
            self.assertEqual(stream.feed(frame(n), float(n)), frame(n))
        self.assertEqual(stream.due(10.0), b"")

    def test_control_commands_wait_behind_the_held_frame_and_are_never_dropped(self):
        # Break: a delete/other-image/file-backed command is discarded, overtakes the held
        # frame it followed (Ghostty sees frames and deletes out of order), or flushes that
        # frame before the interval has passed.
        cases = {
            "delete": delete(),
            "other image id": frame(7, image=99),
            "temp-file medium": b"\x1b_Ga=T,f=32,s=8,v=8,t=t,i=32322,U=1,c=1,r=1,q=2;L3RtcC94\x1b\\",
            "plain bytes": b"\x1b[?25l",
        }
        for name, control in cases.items():
            with self.subTest(name):
                stream = FrameStream(IMAGE)
                self.assertEqual(stream.feed(frame(1), 0.0), frame(1))
                self.assertEqual(stream.feed(frame(2), 0.1), b"")  # held
                self.assertEqual(stream.feed(control, 0.2), b"")
                self.assertEqual(stream.due(0.5 - 0.01), b"")
                self.assertEqual(stream.due(0.5), frame(2) + control)
                self.assertEqual(stream.due(5.0), b"")

    def test_frames_split_at_any_byte_are_forwarded_whole(self):
        # Break: a partial escape sequence is written to the pane tty, where the host's next
        # screen write would land inside the graphics command.
        stream = FrameStream(IMAGE)
        data = frame(1) + delete() + frame(2)
        out = b""
        for i in range(len(data)):
            piece = stream.feed(data[i:i + 1], 0.0)
            out += piece
            if piece:
                self.assertTrue(out.endswith(b"\x1b\\"), "forwarded a partial sequence")
        self.assertEqual(out, frame(1) + delete())
        self.assertEqual(stream.due(0.5), frame(2))

    def test_exit_with_an_unfinished_frame_forwards_only_complete_ones(self):
        # Break: the chunks of a frame whose final chunk never arrived reach the pane on exit,
        # leaving a half-transmitted image for the terminal to finish with later bytes.
        stream = FrameStream(IMAGE)
        self.assertEqual(stream.feed(frame(1), 0.0), frame(1))
        self.assertEqual(stream.feed(frame(2), 0.1), b"")
        unfinished = frame(3)
        cut = unfinished.rindex(b"\x1b_G")  # drop the m=0 chunk
        self.assertEqual(stream.feed(unfinished[:cut] + b"\x1b_Gm=1;QUJD", 0.2), b"")
        self.assertEqual(stream.close(), frame(2))

    def test_a_frame_interrupted_by_another_command_is_forwarded_unchanged(self):
        # Break: a malformed stream (new command before the final chunk) is reordered or
        # swallowed instead of being passed through byte for byte.
        stream = FrameStream(IMAGE)
        broken = frame(1)
        cut = broken.rindex(b"\x1b_G")
        data = broken[:cut] + delete() + broken[cut:]
        self.assertEqual(stream.feed(data, 0.0), data)

    def test_buffering_is_bounded(self):
        # Break: an endless frame or unterminated sequence grows host memory without limit.
        stream = FrameStream(IMAGE, max_frame=4096, max_pending=4096)
        big = frame(1, chunks=8, repeat=800)  # about 1.5 KB per chunk, 11.8 KB over the 4 KB cap
        self.assertEqual(stream.feed(big, 0.0), big)
        self.assertEqual(stream.feed(b"\x1b_Ga=T,i=5;" + b"A" * 5000, 0.1), b"")
        self.assertEqual(stream.feed(b"\x1b_Ga=d,d=I,i=5\x1b\\", 0.2), b"\x1b_Ga=d,d=I,i=5\x1b\\")

    def test_shared_memory_frames_at_15_fps_are_bounded_end_on_the_newest_and_dropped_ones_are_unlinked(self):
        # Break: every shm frame still reaches the pane at the page's rate (Herdr's client then
        # queues full images ahead of every key), the trailing frame is dropped so the pane
        # stalls on an old image, or a superseded frame's shared memory is never unlinked (no
        # terminal ever reads it, so each leaks a full RGBA canvas until reboot).
        shm = SharedMemoryFrames(self, 90)
        frames = [named(name) for name in shm.names]
        arrivals = {round(n * 1000 / 15 / 10) * 10: frames[n] for n in range(90)}  # 15 fps, 6 s
        emitted = run_stream(FrameStream(IMAGE), arrivals, 7000)
        self.assertLessEqual(len(emitted), 13)  # one at 0 s, then at most one per 0.5 s to 6 s
        order = [frames.index(out) for _, out in emitted]
        self.assertEqual(order, sorted(order))
        gaps = [b[0] - a[0] for a, b in zip(emitted, emitted[1:])]
        self.assertGreaterEqual(min(gaps), 0.5 - 1e-9)
        self.assertEqual(emitted[-1][1], frames[89])
        forwarded = {shm.names[n] for n in order}
        for name in shm.names:
            self.assertEqual(shm_exists(name), name in forwarded, name)

    def test_file_frames_are_bounded_and_the_browsers_ring_files_are_never_deleted(self):
        # Break: file frames pass at the page's rate, or a dropped frame's file is deleted:
        # terminal-browser rewrites the same eight files in turn, so deleting one breaks a
        # later frame that names it.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ring = Path(tmp.name)
        slots = [ring / f"terminal-browser-1-0-0-{n}.rgba" for n in range(8)]
        for slot in slots:
            slot.write_bytes(b"\0\0\0\xff")
        frames = [named(str(slots[n % 8]), medium="f") for n in range(45)]
        arrivals = {round(n * 1000 / 15 / 10) * 10: frames[n] for n in range(45)}  # 15 fps, 3 s
        emitted = run_stream(FrameStream(IMAGE), arrivals, 4000)
        self.assertLessEqual(len(emitted), 7)
        self.assertEqual(emitted[-1][1], frames[44])
        self.assertTrue(all(slot.exists() for slot in slots))

    def test_controls_between_shared_memory_frames_keep_their_order(self):
        # Break: a delete or other command overtakes the held shm frame it followed, is
        # dropped, or the held frame is unlinked although it was forwarded.
        shm = SharedMemoryFrames(self, 3)
        one, two, three = (named(name) for name in shm.names)
        stream = FrameStream(IMAGE)
        self.assertEqual(stream.feed(one, 0.0), one)
        self.assertEqual(stream.feed(two, 0.1), b"")  # held
        self.assertEqual(stream.feed(delete() + b"\x1b[?25l", 0.2), b"")
        self.assertEqual(stream.due(0.5), two + delete() + b"\x1b[?25l")
        self.assertEqual(stream.feed(three, 0.6) + stream.due(5.0), three)
        self.assertTrue(all(shm_exists(name) for name in shm.names))

    def test_a_resized_frame_after_the_browsers_delete_waits_for_the_quiet_window(self):
        # Break: the browser's delete-and-resize reaches the pane right behind the previous
        # frame, while Herdr may still be sending that frame to its client natively; a size
        # change then turns Herdr's fast image path off for that client for good.
        shm = SharedMemoryFrames(self, 3)
        old, held, resized = named(shm.names[0]), named(shm.names[1]), named(shm.names[2], size=(1904, 1530))
        stream = FrameStream(IMAGE, quiet=1.0)
        self.assertEqual(stream.feed(old, 0.0), old)
        self.assertEqual(stream.feed(held, 0.1), b"")
        self.assertEqual(stream.feed(delete() + resized, 0.2), b"")
        self.assertEqual(stream.due(1.0 - 0.02), b"")
        self.assertEqual(stream.deadline(), 1.0)
        self.assertEqual(stream.due(1.0), delete() + resized)
        self.assertFalse(shm_exists(shm.names[1]))
        self.assertTrue(shm_exists(shm.names[2]))

    def test_control_output_cannot_release_a_frame_inside_the_hold_after_a_placed_layout(self):
        # Break: the delete the browser sends before its resized frame flushes the held old-size
        # frame (and the delete) right behind the layout change the page was just told about.
        stream = FrameStream(IMAGE, interval=0.0)
        self.assertEqual(stream.feed(frame(1), 0.0), frame(1))
        stream.placed(1.0)
        self.assertEqual(stream.feed(frame(2), 1.01), b"")
        self.assertEqual(stream.feed(delete(), 1.03), b"")
        self.assertEqual(stream.feed(frame(3, size=(1000, 800)), 1.04), b"")
        self.assertEqual(stream.due(1.0 + LAYOUT_QUIET - 0.01), b"")
        self.assertEqual(stream.due(1.0 + LAYOUT_QUIET), delete() + frame(3, size=(1000, 800)))

    def test_control_output_cannot_release_a_frame_while_a_layout_change_waits(self):
        # Break: a cursor command flushes the held frame although `fenced` says a layout change
        # waits for the frames to stop, so the change never finds the quiet it needs.
        stream = FrameStream(IMAGE)
        stream.feed(frame(1), 0.0)
        stream.feed(frame(2), 0.1)
        stream.fenced = True
        self.assertEqual(stream.feed(b"\x1b[?25l", 0.7), b"")
        self.assertEqual(stream.due(9.0), b"")
        stream.fenced = False
        self.assertEqual(stream.due(9.0), frame(2) + b"\x1b[?25l")

    def test_a_browser_interleaving_controls_is_still_limited_to_the_frame_interval(self):
        # Break: a control command between frames resets the interval, so every frame of a
        # 15 fps page that sends a cursor command with each one reaches the pane.
        frames = [frame(n) for n in range(90)]
        arrivals = {round(n * 1000 / 15 / 10) * 10: b"\x1b[?25l" + frames[n] + delete() for n in range(90)}
        stream = FrameStream(IMAGE)
        data, frame_times = b"", []
        for ms in range(0, 7001, 10):
            now = ms / 1000
            before = stream.frames_out
            data += stream.feed(arrivals[ms], now) if ms in arrivals else stream.due(now)
            if stream.frames_out > before:
                frame_times.append(now)
        self.assertLessEqual(len(frame_times), 13)
        self.assertGreaterEqual(min(b - a for a, b in zip(frame_times, frame_times[1:])), 0.5 - 1e-9)
        self.assertIn(frames[89], data)
        self.assertEqual(data.count(delete()), 90)
        self.assertEqual(data.count(b"\x1b[?25l"), 90)

    def test_output_held_with_a_frame_is_bounded_and_dropped_with_the_stream(self):
        # Break: a browser flooding control output while a frame is held grows memory without
        # limit; discarding the stream forwards or keeps what was held.
        stream = FrameStream(IMAGE, max_pending=100)
        stream.feed(frame(1), 0.0)
        stream.feed(frame(2), 0.1)
        self.assertEqual(stream.feed(b"x" * 60, 0.2), b"")
        self.assertEqual(stream.feed(b"y" * 60, 0.3), frame(2) + b"x" * 60 + b"y" * 60)
        self.assertEqual(stream.deferred, 0)
        stream.feed(frame(3), 0.4)
        stream.feed(b"z", 0.45)
        stream.discard()
        self.assertEqual((stream.lead, stream.behind, stream.deferred), ([], [], 0))
        self.assertEqual(stream.due(9.0), b"")

    def test_discarding_the_stream_unlinks_its_held_shared_memory_frame(self):
        # Break: closing or switching the browser drops the held frame's shared memory without
        # unlinking it (nothing else will: the browser is gone).
        shm = SharedMemoryFrames(self, 2)
        stream = FrameStream(IMAGE)
        stream.feed(named(shm.names[0]), 0.0)
        stream.feed(named(shm.names[1]), 0.1)
        stream.discard()
        self.assertEqual([shm_exists(name) for name in shm.names], [True, False])
        self.assertEqual(stream.due(5.0), b"")


class BrowserEnvTests(unittest.TestCase):
    def test_child_env_shortens_the_agent_glow_without_touching_the_caller(self):
        # Break: the browser child keeps terminal-browser's 10 s agent-control glow, or the
        # short TTL leaks into the host's own (or the user's) environment.
        base = {"HOME": "/private/home", "PATH": "/usr/bin"}
        before = os.environ.get("TERMINAL_BROWSER_AGENT_CONTROL_MS")
        env = browser_env(base, "/tmp/s/embed.sock", "/dev/ttys099")
        self.assertEqual(env["PIXEL_EMBED"], "/tmp/s/embed.sock")
        self.assertEqual(env["PIXEL_TTY"], "/dev/ttys099")
        self.assertEqual(env["HOME"], "/private/home")
        self.assertIn("TERMINAL_BROWSER_AGENT_CONTROL_MS", env)
        self.assertGreater(int(env["TERMINAL_BROWSER_AGENT_CONTROL_MS"]), 0)
        self.assertLessEqual(int(env["TERMINAL_BROWSER_AGENT_CONTROL_MS"]), 1000)
        self.assertEqual(base, {"HOME": "/private/home", "PATH": "/usr/bin"})
        self.assertEqual(os.environ.get("TERMINAL_BROWSER_AGENT_CONTROL_MS"), before)


class FakeCdp:
    """Loopback stand-in for Chromium's DevTools HTTP list and one page websocket."""

    def __init__(self, page):
        self.page = page
        self.requests = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def targets(self):
        ws = f"ws://127.0.0.1:{self.port}/devtools/page/{self.page['id']}"
        return [
            {"description": "", "devtoolsFrontendUrl": f"/devtools/inspector.html?ws=127.0.0.1:{self.port}",
             "id": "SERVICEWORKER0", "title": "other", "type": "service_worker", "url": "file:///sw.js",
             "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/SERVICEWORKER0"},
            {"description": "", "devtoolsFrontendUrl": f"/devtools/inspector.html?ws=127.0.0.1:{self.port}",
             "id": self.page["id"], "title": self.page["title"], "type": "page", "url": self.page["url"],
             "webSocketDebuggerUrl": ws},
        ]

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    head += chunk
                line = head.split(b"\r\n", 1)[0].decode()
                self.requests.append(line)
                if line.startswith("GET /json/list "):
                    body = json.dumps(self.targets()).encode()
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                    continue
                key = [h.split(b":", 1)[1].strip() for h in head.split(b"\r\n")
                       if h.lower().startswith(b"sec-websocket-key:")][0]
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                             b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                request = json.loads(self.read_frame(conn))
                self.requests.append(request["method"])
                value = json.dumps({"href": self.page["href"], "title": self.page["title"],
                                    "selection": self.page["selection"]})
                reply = json.dumps({"id": request["id"],
                                    "result": {"result": {"type": "string", "value": value}}}).encode()
                header = b"\x81" + (bytes([len(reply)]) if len(reply) < 126
                                    else b"\x7e" + len(reply).to_bytes(2, "big"))
                conn.sendall(header + reply)

    @staticmethod
    def read_frame(conn):
        def need(n):
            data = b""
            while len(data) < n:
                data += conn.recv(n - len(data))
            return data

        _, b1 = need(2)
        length = b1 & 0x7F
        if length == 126:
            length = int.from_bytes(need(2), "big")
        mask = need(4)
        return bytes(b ^ mask[i % 4] for i, b in enumerate(need(length)))

    def close(self):
        self.server.close()


class CaptureTests(unittest.TestCase):
    URL = "file:///lab/fixture/beta.html"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "terminal-browser"
        root.mkdir()
        self.root = str(root)
        self.argv_log = Path(self.tmp.name) / "tb-argv.jsonl"
        self.registry = Path(self.tmp.name) / "registry.json"
        self.tb = root / "terminal-browser"
        self.tb.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"open({str(self.argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:] == ['ls', '--all', '--json']:\n"
            f"    sys.stdout.write(open({str(self.registry)!r}).read())\n"
            "elif sys.argv[1:2] == ['action']:\n"
            "    print(json.dumps({'success': True, 'data': {'url': 'about:blank'}}))\n"
            "else:\n"
            "    sys.exit(2)\n"
        )
        self.tb.chmod(self.tb.stat().st_mode | stat.S_IXUSR)
        self.browser = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                         str(root / "browser" / "main.js")])
        self.addCleanup(self.browser.wait)
        self.addCleanup(self.browser.kill)
        self.page = {"id": "5FF7E6C2804A849DFDD16D38E2AD56A8", "url": self.URL, "href": self.URL,
                     "title": "FP Fixture Beta", "selection": "SYNTHETIC-BETA-3K selection target sentence."}
        self.cdp = FakeCdp(self.page)
        self.addCleanup(self.cdp.close)

    def write_registry(self, pid=None, browsers=1):
        record = {
            "key": "87631-1", "pid": pid or self.browser.pid, "cdpPort": self.cdp.port,
            "socket": "/tmp/fp/terminal-browser-721bcf8c/instances/87631-1.sock", "tty": "/dev/ttys012",
            "pane": {"tab": None, "pane": None}, "splitDir": None, "parentTty": None, "inCurrentTab": False,
            "viewport": {"width": 1952, "height": 1190},
            "tabs": [{"id": 1, "url": self.URL, "title": "FP Fixture Beta", "active": True,
                      "targetId": self.page["id"], "timeOrigin": 1792844880533.3, "agentControlled": False}],
        }
        self.registry.write_text(json.dumps({"self": None, "browsers": [record] * browsers}))

    def capture(self, root=None, timeout=20):
        return capture_context(str(self.tb), self.root if root is None else root,
                               {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, timeout=timeout)

    def test_capture_reads_the_registry_target_over_cdp_without_an_agent_action(self):
        # Break: capture calls `terminal-browser action` (marks the tab agent-controlled and
        # starts the 10 s glow burst), or evaluates a different target than the registry tab.
        self.write_registry()
        result = self.capture()
        self.assertEqual([json.loads(line) for line in self.argv_log.read_text().splitlines()],
                         [["ls", "--all", "--json"]])
        self.assertEqual(self.cdp.requests[-1], "Runtime.evaluate")
        self.assertIn(f"GET /devtools/page/{self.page['id']} HTTP/1.1", self.cdp.requests)
        self.assertEqual(result["url"], self.URL)
        self.assertEqual(result["title"], "FP Fixture Beta")
        self.assertEqual(result["selection"], "SYNTHETIC-BETA-3K selection target sentence.")
        self.assertEqual((result["browser_key"], result["browser_pid"], result["tab_id"], result["target_id"],
                          result["cdp_port"]),
                         ("87631-1", self.browser.pid, 1, self.page["id"], self.cdp.port))

    def test_capture_bound_to_an_owner_reads_only_that_process_or_its_descendants(self):
        # Break: the in-tab browser's capture reads a registry entry for a browser some other
        # process launched from the same install.
        self.write_registry()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        self.assertEqual(capture_context(str(self.tb), self.root, env, owner_pid=self.browser.pid)["url"], self.URL)
        self.assertEqual(capture_context(str(self.tb), self.root, env, owner_pid=os.getpid())["url"], self.URL)
        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(stranger.wait)
        self.addCleanup(stranger.kill)
        evaluated = self.cdp.requests.count("Runtime.evaluate")
        with self.assertRaises(CaptureError):
            capture_context(str(self.tb), self.root, env, owner_pid=stranger.pid)
        self.assertEqual(self.cdp.requests.count("Runtime.evaluate"), evaluated)

    def test_page_text_is_terminal_inert_before_preview(self):
        # Break: a hostile page title or selection carries escapes or bidi overrides into the
        # composer preview and the pane.
        self.page.update(title="Beta\x1b]0;owned\x07\u202e", selection="pick\x1b[2J me\nline two")
        self.write_registry()
        result = self.capture()
        self.assertEqual(result["title"], "Beta<bidi>")
        self.assertEqual(result["selection"], "pick me\nline two")

    def test_capture_refuses_a_browser_outside_the_task_private_install(self):
        # Break: a registry entry for another (personal) terminal-browser instance is read.
        self.write_registry(pid=os.getpid())
        with self.assertRaises(CaptureError):
            self.capture()
        self.assertNotIn("Runtime.evaluate", self.cdp.requests)

    def test_capture_refuses_an_ambiguous_registry(self):
        # Break: with two instances listed, capture picks one and reads the wrong browser.
        self.write_registry(browsers=2)
        with self.assertRaises(CaptureError):
            self.capture()
        self.assertEqual(self.cdp.requests, [])

    def test_malformed_registries_surface_only_as_capture_errors(self):
        # Break: a registry the caller cannot trust escapes as ValueError/TypeError/AttributeError,
        # which a caller handling only CaptureError never catches.
        self.write_registry()
        good = json.loads(self.registry.read_text())
        record = good["browsers"][0]
        cases = {
            "list": [],
            "null": None,
            "browsers-null": {"browsers": None},
            "pid-text": {"browsers": [dict(record, pid="abc")]},
            "pid-list": {"browsers": [dict(record, pid=[1])]},
            "port-text": {"browsers": [dict(record, cdpPort="abc")]},
            "port-zero": {"browsers": [dict(record, cdpPort=0)]},
            "record-not-object": {"browsers": ["x"]},
            "tabs-not-objects": {"browsers": [dict(record, tabs=["x"])]},
            "no-active-tab": {"browsers": [dict(record, tabs=[])]},
        }
        for name, payload in cases.items():
            with self.subTest(name):
                self.registry.write_text(json.dumps(payload))
                with self.assertRaises(CaptureError):
                    self.capture()
        self.registry.write_text("not json")
        with self.assertRaises(CaptureError):
            self.capture()
        self.assertEqual(self.cdp.requests, [])

    def test_capture_refuses_an_empty_install_root(self):
        # Break: an empty root makes the "running from the private install" check match any process.
        self.write_registry()
        for root in ("", "/"):
            with self.subTest(root=root), self.assertRaises(CaptureError):
                self.capture(root=root)
        self.assertEqual(self.cdp.requests, [])

    def test_a_hung_registry_listing_times_out_as_a_capture_error(self):
        # Break: a stuck `terminal-browser ls` blocks the caller forever.
        self.write_registry()
        self.tb.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        started = time.monotonic()
        with self.assertRaises(CaptureError):
            self.capture(timeout=0.5)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_missing_terminal_browser_is_a_capture_error(self):
        # Break: a missing or non-executable binary escapes as OSError.
        self.write_registry()
        with self.assertRaises(CaptureError):
            capture_context(str(self.tb) + "-missing", self.root, {"PATH": "/usr/bin:/bin"})

    def test_cdp_requests_bypass_environment_proxies(self):
        # Break: the "loopback only" /json/list request is sent through http_proxy/all_proxy.
        proxy = socket.socket()
        proxy.bind(("127.0.0.1", 0))
        proxy.listen(4)
        proxy.settimeout(0.3)
        self.addCleanup(proxy.close)
        url = f"http://127.0.0.1:{proxy.getsockname()[1]}"
        self.write_registry()
        env = {"http_proxy": url, "HTTP_PROXY": url, "all_proxy": url, "ALL_PROXY": url}
        with mock.patch.dict(os.environ, env):
            for name in ("no_proxy", "NO_PROXY"):
                os.environ.pop(name, None)
            result = self.capture()
        self.assertEqual(result["title"], "FP Fixture Beta")
        self.assertIn("GET /json/list HTTP/1.1", self.cdp.requests)
        with self.assertRaises(socket.timeout):
            proxy.accept()


if __name__ == "__main__":
    unittest.main()
