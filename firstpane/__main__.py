"""Run Firstpane as a Herdr plugin pane.

This is a terminal pane, not a section of Herdr's Agents or Spaces sidebar.
"""

import fcntl
import os
import select
import signal
import subprocess
import struct
import sys
import termios
import time
import tty
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from firstpane.app import CELL_QUERY, App
from firstpane.config import load_config
from firstpane.transport import LiveTransport

MODES_ON = "\x1b[?1002h\x1b[?1006h\x1b[?2004h\x1b[?25l"
# Also pops kitty keyboard reporting, SGR-pixel mouse and any-motion (hover) tracking, which the
# in-tab browser may have on.
MODES_OFF = "\x1b[<u\x1b[?1016l\x1b[?1003l\x1b[?1002l\x1b[?1006l\x1b[?2004l\x1b[?25h"
PAGE_KEYBOARD_ON = "\x1b[>1u"
PAGE_KEYBOARD_OFF = "\x1b[<u"
PIXEL_MOUSE_ON = "\x1b[?1016h"
HOVER_MOUSE_ON = "\x1b[?1003h"
HOVER_MOUSE_OFF = "\x1b[?1003l"
# While only browser frame bytes arrive, repaint the panel at most this often (seconds); any
# other wakeup repaints at once.
STREAM_REPAINT_INTERVAL = 0.1


class TtyTerminal:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.fd = None
        self.old = None
        self.raw = False
        self.last_frame = None
        self.pixels = False
        self.page_keys = False
        self.hover = False

    def enter_raw(self):
        """cbreak with ISIG kept (Ctrl-C leaves the pane), but no IEXTEN or IXON, so ^O,
        ^V, ^S and ^Q reach the pane instead of the tty driver."""
        self.last_frame = None
        if not self.stdin.isatty():
            return
        self.fd = self.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] &= ~termios.IXON
        attrs[3] &= ~termios.IEXTEN
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.raw = True
        self.enable_mouse()

    def leave_raw(self):
        self.last_frame = None
        self.disable_mouse()
        if self.fd is None or self.old is None:
            self.raw = False
            return
        try:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        except termios.error:
            pass  # the terminal hung up (EIO on Linux); there is nothing left to restore
        self.raw = False

    def enable_mouse(self):
        """Press/release and drag (button-event) mouse with SGR coordinates (in pixels once the
        terminal said it reports them), plain motion while hover is on, bracketed paste, hidden
        cursor (the inline message editor draws its own caret)."""
        self.stdout.write(MODES_ON + (PIXEL_MOUSE_ON if self.pixels else "") + (HOVER_MOUSE_ON if self.hover else ""))
        self.stdout.flush()

    def disable_mouse(self):
        self.page_keys = False
        self.stdout.write(MODES_OFF)
        self.stdout.flush()

    def pixel_mouse(self, on):
        self.pixels = on
        if self.raw:
            self.stdout.write(PIXEL_MOUSE_ON if on else "\x1b[?1016l")
            self.stdout.flush()

    def hover_mouse(self, on):
        """Pointer motion with no button held too (any-event tracking), while the Browser tab
        shows the page: its hover effects and the select tool's target follow the pointer."""
        if on != self.hover:
            self.hover = on
            if self.raw:
                self.stdout.write(HOVER_MOUSE_ON if on else HOVER_MOUSE_OFF)
                self.stdout.flush()

    def page_keyboard(self, on):
        """Kitty keyboard reporting (disambiguated keys with modifiers) while the page has keys."""
        if on != self.page_keys and self.raw:
            self.page_keys = on
            self.stdout.write(PAGE_KEYBOARD_ON if on else PAGE_KEYBOARD_OFF)
            self.stdout.flush()

    def write_raw(self, data):
        """Whole escape sequences (browser frames, queries) straight to the pane tty."""
        if not data:
            return
        self.stdout.flush()
        stream = getattr(self.stdout, "buffer", None)
        if stream is not None:
            stream.write(data)
            stream.flush()
        else:
            self.stdout.write(data.decode("utf-8", errors="replace"))
            self.stdout.flush()

    def write(self, frame, size=None):
        """Repaint only the lines that changed. A full clear happens on the first frame,
        on resize and after the editor. A colored frame is cleared onto the Agents surface so
        the rest of each row stays that dark; `NO_COLOR` frames clear to the terminal default."""
        lines = frame.split("\n")
        fill = "\x1b[48;2;16;22;35m" if "\x1b[" in frame else ""
        clear = fill + "\x1b[2K" if fill else "\x1b[0m\x1b[2K"
        previous = self.last_frame
        if previous is not None and previous[1] == size:
            old = previous[0]
            out = []
            for index, line in enumerate(lines):
                if index < len(old) and old[index] == line:
                    continue
                out.append(f"\x1b[{index + 1};1H{clear}{line}")
            for index in range(len(lines), len(old)):
                out.append(f"\x1b[{index + 1};1H{clear}")
            if out:
                self.stdout.write("".join(out))
                self.stdout.flush()
        else:
            self.stdout.write((fill + "\x1b[2J\x1b[H") if fill else "\x1b[2J\x1b[H")
            self.stdout.write("".join(f"\x1b[{index + 1};1H{clear}{line}" for index, line in enumerate(lines)))
            self.stdout.flush()
        self.last_frame = (lines, size)

    def read_input(self, timeout):
        fd = self.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return b""
        data = os.read(fd, 65536)
        if not data:
            return None
        return data


def editor_from_config(config):
    if config.editor:
        return config.editor
    visual = os.environ.get("VISUAL")
    editor = os.environ.get("EDITOR")
    if visual:
        return [visual]
    if editor:
        return [editor]
    return ["vi"]


class ConfiguredTransport(LiveTransport):
    def __init__(self, socket_path, editor):
        super().__init__(socket_path)
        self.editor = editor

    def open_editor(self, path, terminal):
        completed = subprocess.run([*self.editor, path], check=False)
        return completed.returncode


def _stop(signum, frame):
    raise SystemExit(128 + signum)


def window_size(fd):
    """(rows, cols, width px, height px) of the pane tty, or None."""
    try:
        return struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        return None


def main():
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR") or str(Path.home() / ".local/state/firstpane")
    path = Path(config_dir) / "config.json" if config_dir else None
    config = load_config(path, os.environ)
    if not config.socket_path:
        config.socket_path = os.environ.get("HERDR_SOCKET_PATH")
    transport = ConfiguredTransport(config.socket_path, editor_from_config(config))
    terminal = TtyTerminal()
    executor = ThreadPoolExecutor(max_workers=4)
    size = window_size(sys.stdout.fileno()) or (24, 80, 0, 0)
    rows, cols = size[0] or 24, size[1] or 80
    app = App(
        config=config,
        transport=transport,
        state_dir=state_dir,
        env=os.environ,
        clock=time.monotonic,
        terminal=terminal,
        rows=max(3, rows),
        cols=max(20, cols),
        submit=executor.submit,
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)
    terminal.enter_raw()
    terminal.write_raw(app.terminal_queries().encode())
    stdin_fd = terminal.stdin.fileno()
    next_refresh = 0
    frame_bytes_only = False
    last_render = 0.0
    last_size = None
    try:
        while True:
            size = window_size(sys.stdout.fileno())
            if size and size[0] and size[1]:
                app.rows, app.cols = max(3, size[0]), max(20, size[1])
                if size != last_size:
                    # A pane that reports its pixel size gives the cell size at once (and follows
                    # client attach, resize and font changes); otherwise ask the terminal again.
                    if size[2] and size[3]:
                        app.cell_size(size[2] // size[1], size[3] // size[0])
                    elif last_size is not None:
                        terminal.write_raw(CELL_QUERY.encode())
                    last_size = size
            now = time.monotonic()
            if now >= next_refresh:
                app.refresh()
                next_refresh = now + 2
            if not frame_bytes_only or now - last_render >= STREAM_REPAINT_INTERVAL:
                terminal.write(app.render(), (app.rows, app.cols))
                last_render = now
            deadline = app.io_deadline()
            wait = 0.2 if deadline is None else max(0.0, min(0.2, deadline - time.monotonic()))
            ready, _, _ = select.select([stdin_fd, *app.io_fds()], [], [], wait)
            terminal.write_raw(app.pump(ready, time.monotonic()))
            frame_bytes_only = ready == [app.browser.master]
            if stdin_fd in ready:
                data = terminal.read_input(0)
                if data is None:
                    return
                app.handle_bytes(data)
    except KeyboardInterrupt:
        return
    finally:
        terminal.write_raw(app.icons.release())
        terminal.leave_raw()
        app.browser.shutdown()
        app.placed.release()
        app.frames.cleanup()
        executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
