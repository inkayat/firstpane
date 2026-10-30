"""Message drafts: one open draft per target, atomic writes, no silent cross-pane overwrite."""

import fcntl
import hashlib
import json
import os
import secrets
from pathlib import Path

FINISHED = ("queued", "already-sent", "already-queued", "transferred")


class DraftStore:
    """The open draft of a target lives at a path derived from the target, so two panes
    creating the same draft race on one file and the second save is a visible conflict.
    A finished draft moves to `<id>.json` and the next load starts a new request id."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self._listing = (None, [])

    def _open_path(self, target):
        digest = hashlib.sha256(target.encode()).hexdigest()[:24]
        return self.directory / f"open-{digest}.json"

    def _read(self, path):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def find(self, target):
        data = self._read(self._open_path(target))
        if data and data.get("target") == target and data.get("outcome") not in FINISHED:
            data.setdefault("attachments", [])
            return data
        return None

    def load(self, target, binding):
        """The open draft for `target`, or a new one bound to `binding` (fixed at creation)."""
        found = self.find(target)
        if found:
            return found
        draft_id = secrets.token_hex(8)
        return {
            "id": draft_id,
            "kind": "message",
            "target": target,
            "binding": dict(binding or {}),
            "request_id": "firstpane-" + draft_id,
            "body": "",
            "attachments": [],
            "outcome": None,
            "note_id": None,
            "revision": 0,
        }

    def open_drafts(self):
        """Every open (unsent or attempted) draft on disk. Re-read only when the directory
        changed: every save and archive replaces a file in it."""
        try:
            stamp = self.directory.stat().st_mtime_ns
        except OSError:
            return []
        if self._listing[0] != stamp:
            found = [self._read(path) for path in sorted(self.directory.glob("open-*.json"))]
            self._listing = (stamp, [data for data in found if data and data.get("target") and data.get("outcome") not in FINISHED])
        return [dict(data) for data in self._listing[1]]

    def reload(self, draft):
        """What is on disk for this draft's target now (None when nothing is open)."""
        data = self._read(self._open_path(draft["target"]))
        return data if data and data.get("id") == draft["id"] else None

    def save(self, draft):
        """Compare-and-replace on the revision. False means another pane saved first."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._open_path(draft["target"])
        with open(self.directory / ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            current = self._read(path)
            if current and current.get("outcome") in FINISHED:
                current = None
            if current and current.get("id") != draft["id"]:
                return False
            on_disk = current.get("revision", 0) if current else 0
            if on_disk != draft.get("revision", 0):
                return False
            draft["revision"] = on_disk + 1
            temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(draft))
            if draft.get("outcome") in FINISHED:
                os.replace(temp, self.directory / f"{draft['id']}.json")
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                os.replace(temp, path)
        return True

    def latest_finished(self, target):
        """The newest finished draft for `target`, or None."""
        if not self.directory.is_dir():
            return None
        found = []
        for path in self.directory.glob("*.json"):
            if path.name.startswith("open-") or path.name.startswith("."):
                continue
            data = self._read(path)
            if data and data.get("target") == target and data.get("outcome") in FINISHED:
                found.append(data)
        if not found:
            return None
        return max(found, key=lambda item: (item.get("finished_at") or 0, item.get("revision") or 0))

    def archive(self, draft):
        """Close an attempted draft (unknown or saved-unannounced) without sending it again;
        it stays on disk under its id so `announce` can still find it."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._open_path(draft["target"])
        with open(self.directory / ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            temp = path.with_name(f".{draft['id']}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(draft))
            os.replace(temp, self.directory / f"{draft['id']}.json")
            current = self._read(path)
            if current and current.get("id") == draft["id"]:
                path.unlink()
