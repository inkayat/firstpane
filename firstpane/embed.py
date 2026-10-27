"""terminal-browser helpers for the Browser tab's in-tab browser (firstpane/browser.py).

- FrameStream: forwards the browser's PIXEL_TTY output whole-sequence-at-a-time. Frames sent
  through shared memory or a file pass straight through. Only directly transmitted (inline)
  full frames of the embed image are coalesced: Herdr removes an image while each inline
  replacement arrives, so back-to-back inline frames reach the pane at most once per
  IMAGE_FRAME_INTERVAL and the latest frame always does. Any other output between frames resets
  the interval (order is preserved), so such a stream is not coalesced.
- capture_context: reads URL, title and selection of the exact registry tab over loopback
  CDP. It never calls `terminal-browser action`, which marks the tab agent-controlled.
- browser_env: the embed variables, a short agent-control glow (terminal-browser 0.13.4 reads
  TERMINAL_BROWSER_AGENT_CONTROL_MS; its default is 10 s of ~10 fps re-renders) and no quiet-tty
  wait before each frame (EMBED_GATE_QUIET_MS; Firstpane is the pty's only writer).
- AgentBridge: terminal-browser's embedded agent bridge on loopback, so the toolbar's select
  tool hands its pick to Firstpane (which opens a FirstMate message about it) instead of
  finding no agent and only copying it.
"""

import base64
import hmac
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import time
import urllib.request

from firstpane.sanitize import sanitize

# Herdr's client fell back to about two presents per second once its fast image path was
# lost; forwarding faster than that only builds a backlog of replacements.
IMAGE_FRAME_INTERVAL = 0.5
# terminal-browser sweeps agent control every 500 ms, so the glow ends 1.0-1.5 s after the
# last agent call instead of 10 s.
AGENT_CONTROL_MS = "1000"
MAX_FRAME_BYTES = 64 << 20
MAX_PENDING_BYTES = 1 << 20
BRIDGE_LIMIT = 1 << 20
BRIDGE_SEND_TIMEOUT = 0.5
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict",
           413: "Payload Too Large"}


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CaptureError(Exception):
    pass


def seq_length(buf):
    """Length of the complete escape sequence at buf[0] (ESC), or 0 if incomplete."""
    if len(buf) < 2:
        return 0
    kind = buf[1]
    if kind == 0x5B:  # CSI
        for i in range(2, len(buf)):
            c = buf[i]
            if 0x40 <= c <= 0x7E:
                return i + 1
            if c < 0x20 or c > 0x3F:
                return i
        return 0
    if kind in (0x5D, 0x50, 0x5F, 0x5E, 0x58):  # OSC DCS APC PM SOS
        for i in range(2, len(buf)):
            if buf[i] == 0x07:
                return i + 1
            if buf[i] == 0x1B:
                return i + 2 if i + 1 < len(buf) else 0
        return 0
    return 2


def _graphics_keys(seq):
    body = seq[3:-2] if seq.endswith(b"\x1b\\") else seq[3:-1]
    head = body.partition(b";")[0]
    return dict(part.split(b"=", 1) for part in head.split(b",") if b"=" in part)


class FrameStream:
    """Whole-sequence forwarder that coalesces superseded direct a=T frames of one image.

    Only complete, directly transmitted (t=d) a=T commands for `image_id` are held; the
    newest replaces an older held one. Any other byte or command first flushes the held
    frame, so order is preserved and nothing else is ever dropped. Partial sequences and
    unfinished frames are never forwarded.
    """

    def __init__(self, image_id, interval=IMAGE_FRAME_INTERVAL, max_frame=MAX_FRAME_BYTES,
                 max_pending=MAX_PENDING_BYTES):
        self.image_id = str(image_id).encode()
        self.interval = interval
        self.max_frame = max_frame
        self.max_pending = max_pending
        self.pending = b""
        self.collecting = None
        self.collected = 0
        self.held = None
        self.next_at = None
        self.frames_in = 0
        self.frames_out = 0
        self.superseded = 0
        self.discarded = 0

    def feed(self, data, now):
        """Bytes safe to write to the pane now, given new browser output at time `now`."""
        self.pending += data
        out = []
        while self.pending:
            buf = self.pending
            if buf[0] != 0x1B:
                stop = buf.find(b"\x1b")
                stop = len(buf) if stop < 0 else stop
                self.pending = buf[stop:]
                self._other(buf[:stop], out)
                continue
            n = seq_length(buf)
            if n == 0:
                if len(buf) > self.max_pending:
                    self.discarded += len(buf)
                    self.pending = b""
                break
            self.pending = buf[n:]
            seq = buf[:n]
            if seq.startswith(b"\x1b_G"):
                self._graphics(seq, out)
            else:
                self._other(seq, out)
        out.append(self.due(now))
        return b"".join(out)

    def due(self, now):
        """The held frame, once the interval since the last forwarded frame has passed."""
        if self.held is None or (self.next_at is not None and now < self.next_at):
            return b""
        frame, self.held = self.held, None
        self.frames_out += 1
        self.next_at = now + self.interval
        return frame

    def deadline(self):
        """When `due` will release the held frame, or None when nothing is held."""
        return None if self.held is None else self.next_at

    def close(self):
        """End of browser output: the held complete frame; unfinished bytes are dropped."""
        self.discarded += len(self.pending) + self.collected
        self.pending, self.collecting, self.collected = b"", None, 0
        frame, self.held = self.held or b"", None
        return frame

    def _graphics(self, seq, out):
        keys = _graphics_keys(seq)
        if self.collecting is not None:
            if b"a" in keys or b"i" in keys:
                self._other(seq, out)
                return
            self.collecting.append(seq)
            self.collected += len(seq)
        elif (keys.get(b"a") == b"T" and keys.get(b"i") == self.image_id
              and keys.get(b"t", b"d") == b"d"):
            self.collecting, self.collected = [seq], len(seq)
        else:
            self._other(seq, out)
            return
        if keys.get(b"m", b"0") == b"0":
            frame = b"".join(self.collecting)
            self.collecting, self.collected = None, 0
            self.frames_in += 1
            if self.held is not None:
                self.superseded += 1
            self.held = frame
        elif self.collected > self.max_frame:
            self._other(b"", out)

    def _other(self, unit, out):
        if self.held is not None:
            out.append(self.held)
            self.held = None
            self.frames_out += 1
        if self.collecting is not None:
            out.extend(self.collecting)
            self.collecting, self.collected = None, 0
        out.append(unit)
        self.next_at = None


def browser_env(base, embed_socket, pixel_tty):
    """Environment for the embedded terminal-browser child; `base` is not modified."""
    env = dict(base)
    env["PIXEL_EMBED"] = embed_socket
    env["PIXEL_TTY"] = pixel_tty
    env["TERMINAL_BROWSER_AGENT_CONTROL_MS"] = AGENT_CONTROL_MS
    env["EMBED_GATE_QUIET_MS"] = "0"
    return env


def _bridge_request(buf, token):
    """None while the request is incomplete, else (status, pick text or None)."""
    head, sep, body = buf.partition(b"\r\n\r\n")
    if not sep:
        return None
    lines = head.decode("latin-1").split("\r\n")
    request = lines[0].split(" ")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length") or 0)
    except ValueError:
        return 400, None
    if len(request) != 3 or length < 0:
        return 400, None
    if length > BRIDGE_LIMIT:
        return 413, None
    if len(body) < length:
        return None
    if request[:2] != ["POST", "/agent-text"]:
        return 404, None
    if not hmac.compare_digest(headers.get("authorization", "").encode(), f"Bearer {token}".encode()):
        return 401, None
    try:
        payload = json.loads(body[:length])
    except ValueError:
        return 400, None
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        return 400, None
    note = payload.get("context") if isinstance(payload, dict) else None
    if note is None:
        note = ""
    if not isinstance(note, str):
        return 400, None
    return 200, text, note


class AgentBridge:
    """terminal-browser 0.13.4's embedded agent bridge, served on loopback for one browser.

    The toolbar's select tool (and the "Send to agent" menu entries) send a pick to
    TERMINAL_BROWSER_AGENT_BRIDGE as `POST /agent-text` with `{"text": ...}` and
    `Authorization: Bearer <TERMINAL_BROWSER_AGENT_TOKEN>`; the browser says "Sent to agent" only
    on a 2xx reply, otherwise it falls back to its own agent search (none in Firstpane's child
    environment). Only requests with this bridge's random token are taken; the owner replies
    to a taken pick with `reply` (200 when a message opened for it, 409 when not)."""

    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(8)
        except OSError:
            self.listener.close()
            raise
        self.listener.setblocking(False)
        self.reading = {}

    def env(self):
        port = self.listener.getsockname()[1]
        return {"TERMINAL_BROWSER_AGENT_BRIDGE": f"http://127.0.0.1:{port}", "TERMINAL_BROWSER_AGENT_TOKEN": self.token}

    def fds(self):
        return [self.listener, *self.reading]

    def pump(self, ready):
        """Read what is ready; returns the picks taken now as (connection, text)."""
        if self.listener in ready:
            while True:
                try:
                    conn, _ = self.listener.accept()
                except OSError:
                    break
                conn.setblocking(False)
                self.reading[conn] = b""
        picks = []
        for conn in [conn for conn in self.reading if conn in ready]:
            try:
                data = conn.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            buf = self.reading.pop(conn) + data
            if not data:
                conn.close()
                continue
            outcome = _bridge_request(buf, self.token)
            if outcome is None:
                if len(buf) > BRIDGE_LIMIT + 65536:
                    self.reply(conn, 413)
                else:
                    self.reading[conn] = buf
            elif outcome[0] == 200:
                picks.append((conn, outcome[1], outcome[2] if len(outcome) > 2 else ""))
            else:
                self.reply(conn, outcome[0])
        return picks

    @staticmethod
    def reply(conn, status):
        try:
            conn.settimeout(BRIDGE_SEND_TIMEOUT)
            conn.sendall(f"HTTP/1.1 {status} {REASONS[status]}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        for conn in self.reading:
            conn.close()
        self.reading = {}
        self.listener.close()


def capture_context(tb_bin, tb_root, env, timeout=20, owner_pid=None):
    """URL, title and selection of the one task-private browser's active tab, with provenance.

    Reads the terminal-browser registry (`ls --all --json`), checks the instance runs from
    `tb_root` (and, with `owner_pid`, is that process or one of its descendants), then
    evaluates on exactly that tab's CDP target over loopback. Page text is sanitized before
    it is returned.
    """
    if not tb_root.strip("/ "):
        raise CaptureError("no task-private install root given")
    try:
        listing = subprocess.run([tb_bin, "ls", "--all", "--json"], env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=timeout)
        records = json.loads(listing.stdout or "{}").get("browsers") or []
        if len(records) != 1:
            raise CaptureError(f"expected exactly one private browser instance, found {len(records)}")
        record = records[0]
        key, pid, port = str(record.get("key")), int(record.get("pid") or 0), int(record.get("cdpPort") or 0)
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                                 timeout=timeout).stdout
        if pid <= 0 or port <= 0 or tb_root.rstrip("/") + "/" not in command:
            raise CaptureError(f"instance {sanitize(key)} pid {pid} is not the task-private install")
        if owner_pid is not None and not _descends_from(pid, owner_pid, timeout):
            raise CaptureError(f"instance {sanitize(key)} pid {pid} is not the launched browser")
        active = [tab for tab in record.get("tabs") or [] if tab.get("active")]
        if len(active) != 1:
            raise CaptureError(f"instance {sanitize(key)} has {len(active)} active tabs")
        tab = active[0]
        with _DIRECT.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as reply:
            targets = json.load(reply)
        exact = [t for t in targets if t.get("id") == tab.get("targetId")]
        if len(exact) != 1:
            raise CaptureError("registry target not found exactly once in CDP /json/list")
        reply = _evaluate(exact[0].get("webSocketDebuggerUrl") or "", port,
                          "JSON.stringify({href: location.href, title: document.title,"
                          " selection: String(window.getSelection())})")
        values = json.loads(reply["result"]["result"]["value"])
        if values.get("href") != tab.get("url"):
            raise CaptureError("CDP target page differs from the registry tab")
    except CaptureError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        raise CaptureError(f"terminal-browser capture failed: {sanitize(error)}") from error
    return {
        "browser_key": key, "browser_pid": pid, "tab_id": tab.get("id"), "target_id": tab.get("targetId"),
        "cdp_port": port, "url": sanitize(values.get("href")), "title": sanitize(values.get("title")),
        "selection": sanitize(values.get("selection")), "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _descends_from(pid, ancestor, timeout, hops=16):
    for _ in range(hops):
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
        parent = subprocess.run(["ps", "-p", str(pid), "-o", "ppid="], capture_output=True, text=True,
                                timeout=timeout).stdout.strip()
        pid = int(parent) if parent.isdigit() else 0
    return False


def _evaluate(ws_url, port, expression, timeout=5):
    """One Runtime.evaluate over a minimal RFC 6455 client, loopback CDP port only."""
    m = re.match(r"^ws://(127\.0\.0\.1|localhost):(\d+)(/.*)$", ws_url)
    if not m or int(m.group(2)) != port:
        raise CaptureError("refusing a CDP websocket outside the registry loopback port")
    host, path = m.group(1), m.group(3)
    key = base64.b64encode(os.urandom(16)).decode()
    with socket.create_connection((host, port), timeout=timeout) as conn:
        conn.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                raise CaptureError("CDP websocket closed during handshake")
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CaptureError("CDP websocket handshake refused")
        payload = json.dumps({"id": 1, "method": "Runtime.evaluate",
                              "params": {"expression": expression, "returnByValue": True}}).encode()
        mask = os.urandom(4)
        size = len(payload)
        header = b"\x81" + (bytes([0x80 | size]) if size < 126 else b"\xfe" + struct.pack(">H", size))
        conn.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

        def need(count):
            nonlocal buf
            while len(buf) < count:
                chunk = conn.recv(65536)
                if not chunk:
                    raise CaptureError("CDP websocket closed")
                buf += chunk
            data, buf = buf[:count], buf[count:]
            return data

        while True:
            b0, b1 = need(2)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", need(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", need(8))[0]
            data = need(length)
            if b0 & 0x0F == 1:
                message = json.loads(data)
                if message.get("id") == 1:
                    return message
