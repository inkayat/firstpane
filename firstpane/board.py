"""Board projection reader.

Column meaning stays in fm-board reconcile(). This module does not classify
work and does not write the backlog. It resolves the installed list command
and reads its JSON.
"""

import hashlib
import json
import os
import re
import shutil

SUMMARY_INTERVAL = 300
LIST_FIELDS = (
    "id", "project", "title", "state", "agent", "kind", "source",
    "since", "running", "captain_review", "updated_at",
)
COLUMNS = (
    ("todo", "TODO"),
    ("in_progress", "Working"),
    ("waiting_review", "Waiting Review"),
    ("blocked", "Blocked"),
)
DONE = ("done", "Done")
COLUMN_GLYPH = {
    "todo": "\u25cb",
    "in_progress": "\u25cf",
    "waiting_review": "\u25c9",
    "blocked": "\u2298",
    "done": "\u2713",
}
KNOWN_STATES = {key for key, _ in COLUMNS} | {DONE[0]}
STALE_AFTER = 180
BADGE_CADENCE = 60
COVERAGE_INVENTORY = "reconcile coverage: inventory"
SHOW_TIMEOUT = 15
OPEN_TIMEOUT = 10
STATUS_TAIL = 8
STATUS_BYTES = 64 * 1024
IDENTITY_RE = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)?)?#[0-9]+$"
)
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$"
)
SHOW_FIELD_RE = re.compile(r"^\s*([a-z_]+):\s*(\S.*)$")
STATUS_LINE_RE = re.compile(r"^(\w[\w-]*) \[at=(\d+)\](?: \[key=[^\]]+\])?: (.*)$")
PR_URL_RE = re.compile(r"https://[^\s\"'<>]+/pull/\d+")
BACKLOG_EMPTY = ("-", "none")
MACHINE_BODY_PREFIXES = (
    "Captain hold set:",
    "Captain hold origin:",
    "Resolution recorded by fm-captain-hold.",
    "Resolution recorded by fm-decision-hold.",
    "Decision digest:",
    "Resolution mode:",
    "Captain decision:",
    "Reconciliation evidence:",
)


def resolve_fm(env):
    """The fm launcher, symlink-resolved the way bin/fm resolves itself.

    fm-board is that file's sibling. It is not under firstmate_root.
    """
    found = shutil.which("fm", path=env.get("PATH"))
    if not found:
        return None
    path = found
    seen = set()
    while os.path.islink(path):
        if path in seen:
            return None
        seen.add(path)
        target = os.readlink(path)
        path = target if os.path.isabs(target) else os.path.join(os.path.dirname(path), target)
    return path if os.path.isfile(path) else None


def fm_board_path(env):
    launcher = resolve_fm(env)
    if not launcher:
        return None
    sibling = os.path.join(os.path.dirname(launcher), "fm-board")
    return sibling if os.path.isfile(sibling) else None


def unavailable(home, root, env):
    """Why the board cannot be read, or None when both homes and fm-board resolve."""
    if not home:
        return "FirstMate home is not configured"
    if not root:
        return "FirstMate tool checkout is not configured"
    if not fm_board_path(env):
        return "fm-board is not next to the fm launcher"
    return None


def command_env(base, home, root):
    """Explicit homes only. Ambient FM_* overrides are not forwarded."""
    env = {
        "PATH": base.get("PATH") or "/usr/bin:/bin",
        "HOME": base.get("HOME") or "/",
        "LANG": base.get("LANG") or "C.UTF-8",
        "TMPDIR": base.get("TMPDIR") or "/tmp",
        "FM_HOME": home,
        "FIRSTMATE_ROOT": root,
    }
    system_root = base.get("SYSTEMROOT")
    if system_root:
        env["SYSTEMROOT"] = system_root
    return env


def parse_list(text):
    try:
        payload = json.loads(text or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    tasks = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        tasks.append({key: item.get(key) for key in LIST_FIELDS})
    return tasks


def attach_projection(tasks, state):
    """Copy hold_reason from the projection list already returned. State stays the list's."""
    records = state.get("tasks") if isinstance(state, dict) else None
    if not isinstance(records, dict):
        return tasks
    attached = []
    for task in tasks:
        record = records.get(task["id"]) if isinstance(records.get(task["id"]), dict) else {}
        copy = dict(task)
        copy["hold_reason"] = record.get("hold_reason")
        copy["note"] = record.get("note")
        attached.append(copy)
    return attached


def parse_board_data(text):
    raw = (text or "").strip()
    prefix = "window.BOARD_DATA = "
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    if raw.endswith(";"):
        raw = raw[:-1]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        return None
    tasks = []
    for item in payload["tasks"]:
        if isinstance(item, dict) and item.get("id"):
            tasks.append(item)
    return {"generated_at": payload.get("generated_at"), "tasks": tasks}


def omitted_counts(omitted):
    """Real summary shape is a list of {surface, count}, not a dict of caps."""
    counts = {}
    if not isinstance(omitted, list):
        return counts
    for item in omitted:
        if not isinstance(item, dict):
            continue
        surface = item.get("surface")
        count = item.get("count")
        if isinstance(surface, str) and isinstance(count, int) and count > 0:
            counts[surface] = counts.get(surface, 0) + count
    return counts


def summary_facts(payload, now):
    if not isinstance(payload, dict):
        return {"present": False, "lines": ["summary unavailable"]}
    lines = []
    valid = payload.get("valid")
    if valid is False:
        reason = payload.get("reason") or "unspecified"
        lines.append("summary invalid: " + str(reason))
    omitted = omitted_counts(payload.get("omitted"))
    if omitted:
        shown = ", ".join(f"{name} {count}" for name, count in sorted(omitted.items()))
        lines.append("omitted from this snapshot: " + shown)
    generated = payload.get("generated_epoch")
    published = payload.get("generated")
    age = None
    if isinstance(generated, (int, float)) and isinstance(now, (int, float)):
        age = max(0, int(now - generated))
        if age > SUMMARY_INTERVAL:
            lines.append(f"summary published {published or age} ({age}s ago)")
    return {
        "present": True,
        "valid": valid,
        "omitted": omitted,
        "generated": published,
        "generated_epoch": generated,
        "age": age,
        "lines": lines,
    }


def reconcile_note(stderr):
    for line in (stderr or "").splitlines():
        if "reconcile skipped" in line:
            return line.strip()[:240]
    return None


def visible_tasks(tasks, project, query, active):
    query = (query or "").strip().lower()
    shown = []
    for task in tasks:
        if project and (task.get("project") or "") != project:
            continue
        if active and task.get("state") == "done":
            continue
        if query:
            haystack = " ".join(
                str(task.get(key) or "")
                for key in ("id", "title", "project", "agent", "running", "hold_reason", "kind")
            ).lower()
            if query not in haystack:
                continue
        shown.append(task)
    return shown


def column_tasks(tasks):
    grouped = {key: [] for key, _ in COLUMNS}
    grouped[DONE[0]] = []
    other = []
    for task in tasks:
        state = task.get("state")
        if state in grouped:
            grouped[state].append(task)
        else:
            other.append(task)
    return grouped, other


def is_pending_request(task):
    """A FirstMate row whose list says the captain call is actionable now.

    Waiting Review, a manual row, and a false flag are not inputs.
    """
    return (
        isinstance(task, dict)
        and task.get("source") == "firstmate"
        and task.get("captain_review") is True
        and bool(task.get("id"))
    )


def pending_request_ids(tasks):
    """Unique task ids, first-seen order, over the full unfiltered list."""
    found = []
    seen = set()
    for task in tasks or []:
        if not is_pending_request(task):
            continue
        ident = task.get("id")
        if ident in seen:
            continue
        seen.add(ident)
        found.append(ident)
    return found


def hold_origin(body):
    """The first `Captain hold origin:` line, or None when the body records none."""
    prefix = "Captain hold origin: "
    for line in (body or "").splitlines():
        if line.startswith(prefix) and line[len(prefix):].strip():
            return line[len(prefix):].strip()
    return None


def explicit_request_ids(meta, tasks, origins=None):
    """Current requests this meta explicitly owns.

    The meta's own id counts when it is a current request. A `decision_keys`
    id counts when it is a different current request. An answered key is not
    current, so it is absent. When `origins` has an entry, it must name this
    meta; a different origin is not a link. A missing entry is an unrecorded
    origin and is accepted. Names are not a link.
    """
    pending = set(pending_request_ids(tasks))
    worker = (meta or {}).get("task_id")
    found = []
    if worker in pending:
        found.append(worker)
    for key in (meta or {}).get("decision_keys") or []:
        if key == worker or key in found or key not in pending:
            continue
        if origins is not None and key in origins and origins[key] not in (None, "", worker):
            continue
        found.append(key)
    return found


def waiting_agent_ids(tasks, agents, metas):
    """Pane ids of Waiting agents explicitly bound to a current user request.

    A binding is the existing one: exactly one meta for the agent's task, that
    meta's pane is the agent's pane, and no other meta claims that pane. The
    request is that meta's own id, or a current id on its `decision_keys`.
    Several current calls are still one agent. Names, titles and paths are not
    a link. Idle, done and working rows are not a user wait. A request with no
    such agent is omitted; no row is invented.
    """
    by_task = {}
    claims = {}
    for meta in metas or []:
        task_id = meta.get("task_id")
        pane_id = meta.get("herdr_pane_id")
        by_task.setdefault(task_id, []).append(meta)
        if pane_id:
            claims[pane_id] = claims.get(pane_id, 0) + 1
    found = []
    seen = set()
    for agent in agents or []:
        pane_id = agent.get("pane_id")
        task_id = agent.get("task_id")
        if agent.get("status") != "blocked" or not pane_id or pane_id in seen or not task_id:
            continue
        bound = by_task.get(task_id) or []
        if len(bound) != 1 or bound[0].get("herdr_pane_id") != pane_id or claims.get(pane_id) != 1:
            continue
        if not explicit_request_ids(bound[0], tasks):
            continue
        seen.add(pane_id)
        found.append(pane_id)
    return found


def coverage_is_inventory(stderr):
    """True only for the exact producer line that the official inventory was read.

    Silence, a fallback note, and every other line are not that signal.
    """
    needle = COVERAGE_INVENTORY
    for line in (stderr or "").splitlines():
        text = line.strip()
        if text == needle or text == "fm-board: " + needle:
            return True
    return False


def list_is_fresh(exit_ok, reconcile_skipped, summary_present, loaded_at, now, stale_after=STALE_AFTER):
    """True only when the list itself and the summary signals are not already stale.

    This is not inventory coverage. Exit 0 plus a present summary can still be the
    silent bounded fallback. coverage_is_inventory is the only current-coverage signal.
    """
    if not exit_ok or reconcile_skipped or not summary_present:
        return False
    if loaded_at is None or now is None:
        return False
    return now - loaded_at <= stale_after


def badge_state(configured, have_list, fresh):
    if not configured:
        return "unconfigured"
    if not have_list:
        return "unknown"
    return "fresh" if fresh else "stale"


def badge_suffix(state, count, narrow):
    """(suffix, urgent). urgent paints the suffix yellow. A fresh zero has no suffix."""
    if state in ("unconfigured",) or (state == "fresh" and count == 0):
        return "", False
    if state == "unknown" or count <= 0:
        return " ?", False
    mark = "\u26a0"
    if narrow:
        text = f"{mark}{count}" if state == "fresh" else f"{mark}{count}?"
    else:
        text = f" {mark} {count}" if state == "fresh" else f" {mark} {count}?"
    return text, state == "fresh"


def parse_identity(stdout):
    text = (stdout or "").strip()
    if not text:
        return None
    line = text.splitlines()[-1].strip()
    return line if IDENTITY_RE.fullmatch(line) else None


def interpret_open(result):
    """Map an `open --identity` result to code 0/1/2/3. Unreadable stdout is 2."""
    if getattr(result, "timed_out", False):
        return {"code": 2, "lifecycle": None, "reason": "timeout"}
    code = getattr(result, "code", 2)
    if code == 0:
        life = parse_identity(getattr(result, "stdout", ""))
        if life is None:
            return {"code": 2, "lifecycle": None, "reason": "unreadable"}
        return {"code": 0, "lifecycle": life, "reason": None}
    if code in (1, 2, 3):
        return {"code": code, "lifecycle": None, "reason": None}
    return {"code": 2, "lifecycle": None, "reason": "unexpected"}


def _decode_show_value(value):
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = value[1:-1] if value.endswith('"') and len(value) >= 2 else value
        if not isinstance(decoded, str):
            decoded = "" if decoded is None else str(decoded)
        value = decoded
    return "" if value in BACKLOG_EMPTY else value


def parse_backlog_show(stdout, task_id):
    """fm-board's show --full grammar. NOT_FOUND is not a record."""
    record = {}
    for line in (stdout or "").splitlines():
        match = SHOW_FIELD_RE.match(line)
        if not match:
            continue
        record[match.group(1)] = _decode_show_value(match.group(2).strip())
    if record.get("code") == "NOT_FOUND" or record.get("id") != task_id:
        reason = "not_found" if record.get("code") == "NOT_FOUND" or "NOT_FOUND" in (stdout or "") else "unreadable"
        return {"found": False, "reason": reason, "record": {}}
    return {"found": True, "reason": None, "record": record}


def question_sha256(text):
    return hashlib.sha256((text or "").encode()).hexdigest()


def purpose_text(body):
    """Backlog body with the machine hold and resolution lines left out."""
    kept = []
    for line in (body or "").splitlines():
        if line.startswith(MACHINE_BODY_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def pull_urls(*texts):
    found = []
    seen = set()
    for text in texts:
        if not text:
            continue
        for match in PR_URL_RE.findall(str(text)):
            if match not in seen:
                seen.add(match)
                found.append(match)
    return found


def column_for(task):
    state = (task or {}).get("state")
    if state in KNOWN_STATES:
        return state
    return "other"
