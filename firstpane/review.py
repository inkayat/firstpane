"""User-started review command. Firstpane only consumes; it never reviews.

The operator's command reads one request JSON document on stdin and writes one result on
stdout. There is no default command, no shell, and no Quen detection. Output is untrusted.
"""

import hashlib
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from firstpane.workspace import parse_diff

REQUEST_SCHEMA = "firstpane.review.request.v1"
RESULT_SCHEMA = "firstpane.review.result.v1"
PATCH_LIMIT = 2 * 1024 * 1024
STDOUT_LIMIT = 8 * 1024 * 1024
STDERR_LIMIT = 1 * 1024 * 1024
SKILL_LIMIT = 256 * 1024
MAX_FINDINGS = 500
SEVERITIES = {"info", "low", "medium", "high", "critical"}


@dataclass(frozen=True)
class ReviewConfig:
    command: tuple
    skills: tuple
    timeout_seconds: int


def parse_review_config(data):
    """A usable review command, or None when the key is missing or invalid (no menu)."""
    raw = data.get("review") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
        return None
    skills = raw.get("skills")
    if not isinstance(skills, list):
        skills = []
    skills = tuple(item for item in skills if isinstance(item, str) and item.startswith("/"))
    timeout = raw.get("timeout_seconds", 900)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 30 <= timeout <= 7200:
        timeout = 900
    return ReviewConfig(tuple(command), skills, timeout)


def skill_usable(path, repository):
    """(usable, reason). A skill inside the reviewed repository is never usable."""
    try:
        real = Path(path).resolve()
        repo = Path(repository).resolve() if repository else None
    except OSError as exc:
        return False, f"unreadable: {exc.strerror or exc}"
    if repo is not None and (real == repo or repo in real.parents):
        return False, "inside the repository being reviewed"
    skill = real / "SKILL.md"
    if not real.is_dir() or not skill.is_file():
        return False, "missing SKILL.md"
    try:
        if skill.stat().st_size > SKILL_LIMIT:
            return False, "SKILL.md over 256 KiB"
        digest = hashlib.sha256(skill.read_bytes()).hexdigest()
    except OSError as exc:
        return False, f"unreadable SKILL.md: {exc.strerror or exc}"
    return True, digest


def revision_key(scope, revision, patch_sha256):
    blob = json.dumps(
        {"scope": scope, "revision": revision, "patch_sha256": patch_sha256},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(blob).hexdigest()


def build_request(*, run_id, created_at, skills, repository, scope, revision, files, patch_text, max_findings=MAX_FINDINGS):
    text = patch_text if isinstance(patch_text, str) else ""
    encoded = text.encode()
    if len(encoded) > PATCH_LIMIT:
        raise ValueError(f"change too large to review ({len(encoded) / (1024 * 1024):.1f} MiB > 2 MiB)")
    if not files or not text.strip():
        raise ValueError("nothing to review")
    digest = hashlib.sha256(encoded).hexdigest()
    return {
        "schema": REQUEST_SCHEMA,
        "run_id": run_id,
        "created_at": created_at,
        "skills": skills,
        "repository": repository,
        "scope": scope,
        "revision": revision,
        "revision_key": revision_key(scope, revision, digest),
        "files": files,
        "patch": {"format": "git-unified", "sha256": digest, "text": text},
        "limits": {"max_findings": max_findings},
    }


_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


def _quoted_end(text):
    """Index of the quote closing the git-quoted token that starts `text`, or -1."""
    index = 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index
        index += 1
    return -1


def _unquote(token):
    """A path as git prints it: plain, or in double quotes with C and octal escapes."""
    if not token.startswith('"') or _quoted_end(token) != len(token) - 1:
        return token
    body, out, index = token[1:-1], bytearray(), 0
    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 >= len(body):
            out += char.encode()
            index += 1
            continue
        nxt = body[index + 1]
        digits = body[index + 1 : index + 4]
        if len(digits) == 3 and all(d in "01234567" for d in digits):
            out.append(int(digits, 8) & 0xFF)
            index += 4
        else:
            out.append(_ESCAPES.get(nxt, ord(nxt) if ord(nxt) < 128 else 63))
            index += 2
    return out.decode("utf-8", "replace")


def _header_path(rest):
    """The new path of a `diff --git <a> <b>` header's operands."""
    if rest.startswith('"'):
        end = _quoted_end(rest)
        second = rest[end + 2 :] if end != -1 else ""
    elif ' "b/' in rest:
        second = '"b/' + rest.split(' "b/', 1)[1]
    else:
        half = (len(rest) - 1) // 2
        if len(rest) % 2 == 1 and rest[half] == " " and rest[:2] == "a/" and rest[half + 1 : half + 3] == "b/" and rest[2:half] == rest[half + 3 :]:
            second = rest[half + 1 :]
        elif " b/" in rest:
            second = "b/" + rest.rsplit(" b/", 1)[1]
        else:
            return None
    path = _unquote(second)
    return path[2:] if path.startswith("b/") else None


def _scan(patch_text):
    """(file rows, file path of each parse_diff row) read from the patch's own headers."""
    rows = parse_diff(patch_text)
    files, starts = [], []
    for index, line in enumerate(rows):
        if line.kind != "h":
            continue
        text = line.text
        if text.startswith("diff --git "):
            path = _header_path(text[len("diff --git ") :])
            if path is not None:
                files.append({"path": path, "old_path": None, "status": "modified", "old_blob": None, "new_blob": None})
                starts.append(index)
        elif files and text.startswith("new file mode"):
            files[-1]["status"] = "added"
        elif files and text.startswith("deleted file mode"):
            files[-1]["status"] = "deleted"
        elif files and text.startswith("rename from "):
            files[-1].update(status="renamed", old_path=_unquote(text[len("rename from ") :]))
        elif files and text.startswith("rename to "):
            files[-1]["path"] = _unquote(text[len("rename to ") :])
    paths = [None] * len(rows)
    for file, start, stop in zip(files, starts, starts[1:] + [len(rows)]):
        for index in range(start, stop):
            paths[index] = file["path"]
    return files, paths


def patch_files(patch_text):
    return _scan(patch_text)[0]


def patch_line_paths(patch_text):
    return _scan(patch_text)[1]


def _spans(patch_text):
    """{path: {side: [(start, end)]}} from hunk headers. A range must sit inside one of them."""
    spans = {}
    for line, path in zip(parse_diff(patch_text), patch_line_paths(patch_text)):
        if line.kind != "@" or path is None:
            continue
        spans.setdefault(path, {"new": [], "old": []})
        parts = line.text.split("@@")[1].strip().split()
        for token in parts[:2]:
            side = "old" if token.startswith("-") else "new" if token.startswith("+") else None
            if side is None:
                continue
            start_text, _, count_text = token[1:].partition(",")
            start = int(start_text)
            count = int(count_text or "1")
            if count:
                spans[path][side].append((start, start + count - 1))
    return spans


def _reject_constant(value):
    raise ValueError(value)


def _finding_error(item, request, spans):
    if not isinstance(item, dict):
        return "not an object"
    if isinstance(item.get("start_line"), bool) or (item.get("start_line") is not None and not isinstance(item.get("start_line"), int)):
        return "start_line"
    if isinstance(item.get("end_line"), bool) or (item.get("end_line") is not None and not isinstance(item.get("end_line"), int)):
        return "end_line"
    ident = item.get("id")
    if not isinstance(ident, str) or not ident or len(ident) > 64 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for char in ident):
        return "id"
    path = item.get("path")
    files = request["files"]
    names = {row.get("path") for row in files} | {row.get("old_path") for row in files}
    if path is not None and path not in names:
        return "path"
    side = item.get("side")
    start, end = item.get("start_line"), item.get("end_line")
    if start is None:
        if end is not None:
            return "end_line"
    else:
        if side not in ("new", "old") or start < 1 or end is None or end < start:
            return "lines"
        known = spans.get(path, {}).get(side, [])
        if not any(span[0] <= start and end <= span[1] for span in known):
            return "outside hunk"
    if item.get("severity") not in SEVERITIES:
        return "severity"
    title, body = item.get("title"), item.get("body")
    if not isinstance(title, str) or not 1 <= len(title) <= 200:
        return "title"
    if not isinstance(body, str) or len(body) > 8000:
        return "body"
    skill = item.get("skill")
    known_skills = {row.get("name") for row in request["skills"]}
    if skill is not None and skill not in known_skills:
        return "skill"
    return None


def validate_result(raw, request, patch_text):
    """(outcome, result or None, error). A document error shows no findings."""
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return "failed", None, f"invalid output: {exc}"
    if not isinstance(payload, dict):
        return "failed", None, "invalid output: not an object"
    if payload.get("schema") != RESULT_SCHEMA:
        return "failed", None, "unsupported result schema" if isinstance(payload.get("schema"), str) else "invalid output: schema"
    if payload.get("run_id") != request["run_id"]:
        return "failed", None, "invalid output: run_id"
    if payload.get("revision_key") != request["revision_key"]:
        return "failed", None, "invalid output: revision_key"
    if payload.get("status") not in ("complete", "partial"):
        return "failed", None, "invalid output: status"
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return "failed", None, "invalid output: findings"
    spans = _spans(patch_text)
    kept, dropped, seen = [], 0, set()
    limit = (request.get("limits") or {}).get("max_findings") or MAX_FINDINGS
    for item in findings:
        reason = _finding_error(item, request, spans)
        ident = item.get("id") if isinstance(item, dict) else None
        if reason is None and ident in seen:
            reason = "duplicate id"
        if reason is None and len(kept) >= limit:
            reason = "over max"
        if reason:
            dropped += 1
            continue
        seen.add(ident)
        kept.append({
            "id": item["id"],
            "path": item.get("path"),
            "side": item.get("side"),
            "start_line": item.get("start_line"),
            "end_line": item.get("end_line"),
            "severity": item["severity"],
            "title": item["title"],
            "body": item["body"],
            "skill": item.get("skill"),
        })
    status = payload["status"]
    if dropped or status == "partial":
        status = "partial"
    return ("partial" if status == "partial" else "complete"), {
        "status": status,
        "summary": str(payload.get("summary") or "")[:4000],
        "notes": str(payload.get("notes") or "")[:4000],
        "findings": kept,
        "dropped": dropped,
    }, ""


def stale_label(scope, head_moved=False, blob_changed=False, listed=True, old="", new=""):
    """The label for one finding, or None when it still matches the pinned revision."""
    if scope in ("commit", "pr_commit"):
        return None if listed else "commit no longer listed"
    if scope == "pr" and head_moved:
        return f"stale: PR head moved {old}→{new}"
    if scope == "uncommitted" and head_moved:
        return "stale: HEAD moved"
    if scope == "uncommitted" and blob_changed:
        return "stale: file changed since review"
    return None


def skill_catalog(paths, repository):
    rows = []
    for path in paths:
        usable, detail = skill_usable(path, repository)
        rows.append({"path": str(path), "usable": usable, "sha": detail if usable else "", "reason": "" if usable else detail})
    return rows


def review_scope(*, source, mode="all", head=None, commit=None, parent=None, pr=None, pr_commit=None):
    """(scope, revision) for a pinned review. Raises ValueError when the view is not pinned."""
    if source == "workspace" and commit:
        return "commit", {"commit": commit, "parent": parent}
    if source == "workspace":
        if not head:
            raise ValueError("nothing to review")
        return "uncommitted", {"mode": mode if mode in ("all", "workspace", "staged") else "all", "head": head}
    if not isinstance(pr, dict) or not pr.get("host") or not pr.get("number"):
        raise ValueError("nothing to review")
    repo = pr.get("repo") or ""
    if pr.get("owner") and "/" not in str(repo):
        repo = f"{pr['owner']}/{pr['repo']}"
    if source == "pr" and pr_commit:
        return "pr_commit", {"host": pr["host"], "repo": repo, "number": pr["number"], "commit": pr_commit}
    if source == "pr" and head:
        return "pr", {"host": pr["host"], "repo": repo, "number": pr["number"], "head": head}
    raise ValueError("nothing to review")


def mark_findings(findings, *, scope, head_moved=False, blob_changed=False, listed=True, old="", new=""):
    """Copy findings and attach a stale label. Never drops a finding."""
    marked = []
    for finding in findings:
        label = stale_label(scope, head_moved=head_moved, blob_changed=blob_changed, listed=listed, old=old, new=new)
        marked.append({**finding, "stale": label})
    return marked


def selected_skills(chosen, catalog):
    """Realpaths the user selected that are still usable. An unusable skill cannot start a run."""
    usable = {item.get("path") for item in catalog if item.get("usable") and item.get("path")}
    return [path for path in chosen if path in usable]


def orphan_note(record):
    if not isinstance(record, dict) or record.get("state") != "running":
        return None
    return f"a previous review may still be running (pid {record.get('pid')}, started {record.get('started_at')}); not adopted"


class ReviewSupervisor:
    """One run per process. The caller starts it only after preview and y."""

    def __init__(self, state_dir, popen, clock=time.monotonic, kill=os.killpg, wait=time.sleep):
        self.state_dir = Path(state_dir)
        self.popen = popen
        self.clock = clock
        self.kill = kill
        self.wait = wait
        self.child = None
        self.outcome = None
        self.run_dir = None
        self.timeout = None
        self.started = None
        self._stdio = []

    def active(self):
        return self.child is not None and self.child.poll() is None and self.outcome is None

    def start(self, argv, request, cwd, env, timeout):
        if self.active():
            raise RuntimeError("a review is running; cancel it first")
        run_id = request["run_id"]
        self.run_dir = self.state_dir / "reviews" / run_id
        self.run_dir.mkdir(parents=True, mode=0o700)
        os.chmod(self.run_dir, 0o700)
        request_path = self.run_dir / "request.json"
        stdout_path = self.run_dir / "stdout.json"
        stderr_path = self.run_dir / "stderr.log"
        for path in (request_path, stdout_path, stderr_path):
            path.touch(mode=0o600)
        request_path.write_text(json.dumps(request))
        os.chmod(request_path, 0o600)
        clean = {key: value for key, value in env.items() if not str(key).startswith("HERDR_")}
        self._stdio = [open(request_path, "rb"), open(stdout_path, "wb"), open(stderr_path, "wb")]
        self.child = self.popen(
            list(argv),
            stdin=self._stdio[0],
            stdout=self._stdio[1],
            stderr=self._stdio[2],
            cwd=cwd or str(self.run_dir),
            env=clean,
            start_new_session=True,
            shell=False,
        )
        self.timeout = timeout
        self.started = self.clock()
        self.outcome = None
        self._write("running", pid=self.child.pid, argv=list(argv), cwd=cwd)
        return self.run_dir

    def tick(self):
        if self.child is None:
            return self.outcome
        stdout = self.run_dir / "stdout.json"
        stderr = self.run_dir / "stderr.log"
        if stdout.is_file() and stdout.stat().st_size > STDOUT_LIMIT:
            return self._stop("failed: output too large")
        if stderr.is_file() and stderr.stat().st_size > STDERR_LIMIT:
            return self._stop("failed: output too large")
        if self.clock() - self.started > self.timeout:
            return self._stop(f"timed out after {self.timeout}s")
        code = self.child.poll()
        if code is None:
            return None
        return self._finish(code)

    def cancel(self):
        return self._stop("cancelled")

    def _stop(self, outcome):
        child = self.child
        if child is not None and child.poll() is None:
            try:
                self.kill(child.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                child.wait(3)
            except Exception:
                try:
                    self.kill(child.pid, signal.SIGKILL)
                except OSError:
                    pass
        self.outcome = outcome
        self.child = None
        self._close_stdio()
        self._write(outcome)
        return outcome

    def _close_stdio(self):
        for handle in self._stdio:
            try:
                handle.close()
            except OSError:
                pass
        self._stdio = []

    def _finish(self, code):
        self.child = None
        self._close_stdio()
        if code != 0:
            self.outcome = f"failed: exit {code}"
        else:
            raw = (self.run_dir / "stdout.json").read_bytes()
            if not raw.strip():
                self.outcome = "failed: no output"
            else:
                self.outcome = "complete"
        self._write(self.outcome, exit_code=code)
        return self.outcome

    def _write(self, state, **extra):
        if self.run_dir is None:
            return
        payload = {"state": state, "started_at": self.started, "outcome": self.outcome, **extra}
        path = self.run_dir / "run.json"
        path.write_text(json.dumps(payload))
        os.chmod(path, 0o600)
