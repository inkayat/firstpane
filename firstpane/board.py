"""Board projection reader.

Column meaning stays in fm-board reconcile(). This module does not classify
work and does not write the backlog. It resolves the installed list command
and reads its JSON.
"""

import json
import os
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
