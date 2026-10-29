"""Selected agent workspace: identity, status, diffs. Read-only Git, argv only.

Every read runs with GIT_OPTIONAL_LOCKS=0 so polling never rewrites an agent's index, with
external diff and textconv drivers and fsmonitor hooks disabled.
"""

import os
import re
import stat
from dataclasses import dataclass, field

STATUS_TIMEOUT = 5
DIFF_TIMEOUT = 10
DIFF_CAP = 400_000
COMMIT_LIST_CAP = 50
_OID = re.compile(r"^[0-9a-f]{7,64}$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_INERT = ["--literal-pathspecs", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false"]


def read_env(base):
    env = dict(base or {})
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git_argv(path, *args):
    return ["git", "-C", path, *_INERT, *args]


def canonical(path):
    resolved = os.path.realpath(path)
    if os.path.basename(resolved) == ".git":
        return os.path.dirname(resolved)
    return resolved


@dataclass
class FileEntry:
    group: str  # staged | unstaged | untracked | conflicted
    code: str
    path: str
    orig: str | None = None


@dataclass
class GitStatus:
    oid: str | None = None
    head: str | None = None
    detached: bool = False
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    entries: list = field(default_factory=list)

    @property
    def changed(self):
        return len({entry.path for entry in self.entries})

    @property
    def clean(self):
        return not self.entries

    def branch_label(self):
        if self.detached:
            return "detached at " + (self.oid or "?")[:7]
        name = self.head or "?"
        notes = []
        if self.oid is None:
            notes.append("no commits")
        if not self.upstream:
            notes.append("no upstream")
        return name + (f" ({', '.join(notes)})" if notes else "")


def parse_status(raw):
    status = GitStatus()
    records = raw.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("# "):
            _branch_header(status, record[2:])
            continue
        kind = record[0]
        if kind == "?":
            status.entries.append(FileEntry("untracked", "?", record[2:]))
        elif kind == "1":
            parts = record.split(" ", 8)
            _add_xy(status, parts[1], parts[8], None)
        elif kind == "2":
            parts = record.split(" ", 9)
            orig = records[index] if index < len(records) else None
            index += 1
            _add_xy(status, parts[1], parts[9], orig)
        elif kind == "u":
            parts = record.split(" ", 10)
            status.entries.append(FileEntry("conflicted", parts[1], parts[10]))
    return status


def _branch_header(status, text):
    key, _, value = text.partition(" ")
    if key == "branch.oid":
        status.oid = None if value == "(initial)" else value
    elif key == "branch.head":
        if value == "(detached)":
            status.detached = True
            status.head = None
        else:
            status.head = value
    elif key == "branch.upstream":
        status.upstream = value
    elif key == "branch.ab":
        match = re.match(r"^\+(\d+) -(\d+)$", value)
        if match:
            status.ahead, status.behind = int(match.group(1)), int(match.group(2))


def _add_xy(status, xy, path, orig):
    staged, unstaged = xy[0], xy[1]
    if staged != ".":
        status.entries.append(FileEntry("staged", staged, path, orig))
    if unstaged != ".":
        status.entries.append(FileEntry("unstaged", unstaged, path, None))


def read_status(run, path, env, timeout=STATUS_TIMEOUT):
    """(GitStatus, "") or (None, reason word)."""
    result = run(
        git_argv(path, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=normal"),
        env=read_env(env),
        timeout=timeout,
        cwd=path if os.path.isdir(path) else None,
    )
    if result.timed_out:
        return None, "unavailable: git status timed out"
    if result.code != 0:
        if "not a git repository" in (result.stderr or ""):
            return None, "not a git repo"
        return None, "unavailable: git status failed"
    return parse_status(result.stdout or ""), ""


@dataclass
class Workspace:
    state: str  # ok | ambiguous | unavailable | unknown
    path: str | None = None
    provenance: str = ""
    reason: str = ""
    writable: bool = False


def _toplevel(run, path, env):
    if not path or not os.path.isdir(path):
        return None, None, "missing"
    result = run(
        git_argv(path, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"),
        env=read_env(env),
        timeout=STATUS_TIMEOUT,
        cwd=path,
    )
    lines = (result.stdout or "").splitlines()
    if result.timed_out or result.code != 0 or len(lines) != 2:
        return None, None, "not a git repo"
    return os.path.realpath(lines[0]), canonical(lines[1]), ""


def _validated(run, env, project_key, path, exact):
    top, common, problem = _toplevel(run, path, env)
    if problem:
        return None, problem
    if common != canonical(project_key):
        return None, "belongs to another repository"
    if exact and top != os.path.realpath(path):
        return None, "is not the worktree root"
    return top, ""


def resolve_workspace(run, env, project_key, recorded=None, herdr=None, cwd=None):
    """Exact workspace of one agent. Never guesses the primary path; never falls back
    from a recorded worktree that fails validation."""
    if recorded:
        top, problem = _validated(run, env, project_key, recorded, exact=True)
        if problem:
            return Workspace("unavailable", reason=f"recorded worktree {problem}")
        if herdr and os.path.realpath(herdr) != top:
            return Workspace(
                "ambiguous",
                path=top,
                provenance="fm",
                reason="recorded worktree differs from the Herdr checkout",
            )
        return Workspace("ok", path=top, provenance="fm", writable=True)
    if herdr:
        top, problem = _validated(run, env, project_key, herdr, exact=True)
        if problem:
            return Workspace("unavailable", reason=f"Herdr checkout {problem}")
        return Workspace("ok", path=top, provenance="worktree", writable=True)
    if cwd:
        top, problem = _validated(run, env, project_key, cwd, exact=False)
        if problem:
            return Workspace("unavailable", reason=f"cwd {problem}")
        return Workspace("ok", path=top, provenance="cwd", reason="inferred from cwd; read-only")
    return Workspace("unknown", reason="workspace unknown")


def commit_list_argv(path, remote="origin", remote_head=True):
    """Commits on HEAD that are not on the remote default branch, capped at 50.

    When `<remote>/HEAD` is missing, exclude every remote-tracking ref and say so in the menu.
    """
    exclude = [f"{remote}/HEAD"] if remote_head else ["--remotes"]
    return git_argv(
        path, "log", "-n", str(COMMIT_LIST_CAP), "--format=%H%x09%s", "HEAD", "--not", *exclude,
    )


def parse_commit_list(text):
    rows = []
    for line in (text or "").splitlines():
        oid, _, subject = line.partition("\t")
        if _OID.match(oid):
            rows.append({"oid": oid, "subject": subject})
    return rows


def commit_diff_argv(path, oid):
    if not isinstance(oid, str) or not _OID.match(oid):
        raise ValueError("commit diff refused: not an oid")
    return git_argv(path, "show", "--format=", "--no-color", "--no-ext-diff", "--no-textconv", oid)


def diff_argv(path, entry):
    common = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"]
    if entry.group == "untracked":
        return git_argv(path, *common, "--no-index", "--", "/dev/null", entry.path)
    if entry.group == "staged":
        return git_argv(path, *common, "--cached", "-M", "--", *([entry.orig] if entry.orig else []), entry.path)
    return git_argv(path, *common, "--", entry.path)


def read_commit_diff(run, path, oid, env, timeout=DIFF_TIMEOUT):
    result = run(commit_diff_argv(path, oid), env=read_env(env), timeout=timeout, cwd=path)
    if result.timed_out:
        return "", "unavailable: git show timed out"
    if result.code != 0:
        return "", "unavailable: git show failed"
    text = result.stdout or ""
    if len(text) > DIFF_CAP:
        text = text[:DIFF_CAP] + "\n[diff truncated]\n"
    return text, ""


def read_diff(run, path, entry, env, timeout=DIFF_TIMEOUT):
    result = run(diff_argv(path, entry), env=read_env(env), timeout=timeout, cwd=path)
    if result.timed_out:
        return "", "unavailable: git diff timed out"
    ok_codes = (0, 1) if entry.group == "untracked" else (0,)
    if result.code not in ok_codes:
        return "", "unavailable: git diff failed"
    text = result.stdout or ""
    if len(text) > DIFF_CAP:
        text = text[:DIFF_CAP] + "\n[diff truncated]\n"
    return text, ""


COUNT_CAP = 2_000_000


def _numstat(run, path, env, *args):
    """{path: (added, deleted) | "binary"} from `git diff --numstat -z`, or None on failure."""
    result = run(
        git_argv(path, "diff", "--numstat", "-z", "--no-color", "--no-ext-diff", "--no-textconv", *args),
        env=read_env(env),
        timeout=DIFF_TIMEOUT,
        cwd=path,
    )
    if result.timed_out or result.code != 0:
        return None
    counts = {}
    records = (result.stdout or "").split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, name = parts
        if not name:  # a rename: the next two records are the old and the new path
            name = records[index + 1] if index + 1 < len(records) else ""
            index += 2
        counts[name] = "binary" if added == "-" else (int(added), int(deleted))
    return counts


def _untracked_count(path, rel):
    """(lines, 0) of an untracked regular file, (1, 0) for a symlink, "binary", or None."""
    full = os.path.join(path, rel)
    try:
        info = os.lstat(full)
        if stat.S_ISLNK(info.st_mode):
            return (1, 0)
        if not stat.S_ISREG(info.st_mode) or info.st_size > COUNT_CAP:
            return None
        with open(full, "rb") as handle:
            data = handle.read(COUNT_CAP + 1)
    except OSError:
        return None
    if b"\0" in data[:8000]:
        return "binary"
    return (data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), 0)


def read_counts(run, path, entries, env):
    """{(group, path): (added, deleted) | "binary" | None} for the changed files of `path`."""
    groups = {entry.group for entry in entries}
    unstaged = _numstat(run, path, env) if "unstaged" in groups else None
    staged = _numstat(run, path, env, "--cached", "-M") if "staged" in groups else None
    counts = {}
    for entry in entries:
        if entry.group == "unstaged":
            counts[(entry.group, entry.path)] = (unstaged or {}).get(entry.path)
        elif entry.group == "staged":
            counts[(entry.group, entry.path)] = (staged or {}).get(entry.path)
        elif entry.group == "untracked":
            counts[(entry.group, entry.path)] = _untracked_count(path, entry.path)
        else:
            counts[(entry.group, entry.path)] = None
    return counts


@dataclass
class DiffLine:
    kind: str  # "h" header, "@" hunk, " ", "+", "-", "\\" no-newline note
    old: int | None
    new: int | None
    text: str


def parse_diff(text):
    lines = []
    old = new = None
    in_hunk = False
    for raw in text.split("\n"):
        match = _HUNK.match(raw)
        if match:
            old, new = int(match.group(1)), int(match.group(3))
            in_hunk = True
            lines.append(DiffLine("@", None, None, raw))
            continue
        if not in_hunk or raw.startswith("diff --git "):
            in_hunk = False
            if raw:
                lines.append(DiffLine("h", None, None, raw))
            continue
        if raw.startswith("+"):
            lines.append(DiffLine("+", None, new, raw[1:]))
            new += 1
        elif raw.startswith("-"):
            lines.append(DiffLine("-", old, None, raw[1:]))
            old += 1
        elif raw.startswith(" "):
            lines.append(DiffLine(" ", old, new, raw[1:]))
            old += 1
            new += 1
        elif raw.startswith("\\"):
            lines.append(DiffLine("\\", None, None, raw))
    return lines


def content_id(run, path, entry, env, side="new"):
    """Blob id of the content on the shown side of the diff, or None."""
    if side == "old":
        if entry.group == "staged":
            argv = git_argv(path, "rev-parse", "--verify", "-q", "HEAD:" + (entry.orig or entry.path))
        elif entry.group == "unstaged":
            argv = git_argv(path, "rev-parse", "--verify", "-q", ":" + entry.path)
        else:
            return None
    elif entry.group == "staged":
        argv = git_argv(path, "rev-parse", "--verify", "-q", ":" + entry.path)
    elif entry.group in ("unstaged", "untracked"):
        if entry.code == "D":
            return "deleted"
        argv = git_argv(path, "hash-object", "--", entry.path)
    else:
        return None
    result = run(argv, env=read_env(env), timeout=STATUS_TIMEOUT, cwd=path)
    value = (result.stdout or "").strip()
    if result.code != 0 or result.timed_out or not re.fullmatch(r"[0-9a-f]{40,64}", value):
        return None
    return value
