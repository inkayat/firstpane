"""Which way browser frames reach this pane's terminal, asked of the terminal itself.

terminal-browser in embedded mode sends frames in the transport its host names when it joins.
Shared memory is the fast path: each frame is one short kitty command naming a shared-memory
object the terminal reads. A file is the next option, and inline (every frame encoded into the
tty stream) is the last: that path is known to stall to about two updates per second under
Herdr, so it is reported as degraded, never as equal.

Firstpane asks once, at start, with the same 1x1 `a=q` queries terminal-browser uses: one names
a shared-memory object, one a temporary file. The terminal's answers arrive in Firstpane's input;
the first accepted medium in the order shm, file wins, and no accepted answer within TIMEOUT
means inline.
"""

import mmap
import os
import tempfile

from firstpane.kitty import medium_query

SHM_QUERY_ID = 299
FILE_QUERY_ID = 300
TIMEOUT = 1.5
PIXEL = bytes([0, 0, 0, 255])
LABELS = {
    "shm": "shared memory",
    "file": "file frames (speed not measured)",
    "inline": "inline frames: slow, about 2 updates per second",
}

try:
    import _posixshmem
except ImportError:  # pragma: no cover - CPython on macOS and Linux has it
    _posixshmem = None


class FrameMedium:
    def __init__(self, clock):
        self.clock = clock
        self.asked_at = None
        self.answers = {}
        self.result = None
        self._shm = None
        self._file = None

    def queries(self):
        """Create the probe objects and return the query bytes to write to the pane tty."""
        self.asked_at = self.clock()
        out = []
        if _posixshmem is not None:
            name = f"/fp-q-{os.getpid()}-{os.urandom(3).hex()}"
            try:
                fd = _posixshmem.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode=0o600)
                try:
                    os.ftruncate(fd, len(PIXEL))
                    with mmap.mmap(fd, len(PIXEL)) as view:
                        view[:] = PIXEL
                finally:
                    os.close(fd)
                self._shm = name
                out.append(medium_query(SHM_QUERY_ID, "s", name))
            except OSError:
                self._shm = None
        fd, path = tempfile.mkstemp(prefix="fp-q-", suffix=".rgba")
        with os.fdopen(fd, "wb") as handle:
            handle.write(PIXEL)
        self._file = path
        out.append(medium_query(FILE_QUERY_ID, "f", path))
        return "".join(out)

    def answer(self, image_id, message):
        """A terminal answer to one query (`OK` or an error message)."""
        medium = {SHM_QUERY_ID: "shm", FILE_QUERY_ID: "file"}.get(image_id)
        if medium is None or self.result is not None:
            return
        self.answers[medium] = message == "OK"
        self._decide()

    def poll(self):
        """Settle on inline once nothing acceptable arrived within TIMEOUT."""
        if self.result is None and self.asked_at is not None and self.clock() - self.asked_at >= TIMEOUT:
            self.result = next((m for m in ("shm", "file") if self.answers.get(m)), "inline")
            self.cleanup()
        return self.result

    def _decide(self):
        expected = [m for m, made in (("shm", self._shm), ("file", self._file)) if made]
        if self.answers.get("shm"):
            self.result = "shm"
        elif all(m in self.answers for m in expected):
            self.result = "file" if self.answers.get("file") else "inline"
        if self.result is not None:
            self.cleanup()

    def cleanup(self):
        """Remove the probe objects (a terminal that read the shared memory already unlinked it)."""
        if self._shm and _posixshmem is not None:
            try:
                _posixshmem.shm_unlink(self._shm)
            except OSError:
                pass
        self._shm = None
        if self._file:
            try:
                os.unlink(self._file)
            except OSError:
                pass
        self._file = None

    @property
    def label(self):
        return LABELS.get(self.result, "checking how frames reach this terminal")
