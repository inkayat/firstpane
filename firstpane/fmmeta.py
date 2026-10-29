"""Read FirstMate task binding files. Display labels only; no fleet writes. `harness`, `model`
and `effort` are the launch values FirstMate recorded; a missing, empty or repeated key is None."""

import hashlib
from pathlib import Path

META_KEYS = (
    "project",
    "herdr_session",
    "herdr_workspace_id",
    "herdr_tab_id",
    "herdr_pane_id",
    "endpoint_task_id",
)


def _field(text, key):
    prefix = key + "="
    found = [line[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]
    if len(found) != 1:
        return None
    return found[0]


def _last(text, key):
    """The last `key=` line, as the metadata writer reads an appended value."""
    prefix = key + "="
    found = [line[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]
    return found[-1] if found else None


def decision_keys(text):
    """Ids on the last `decision_keys=` line. An older line is not the inventory."""
    raw = _last(text, "decision_keys")
    if not raw:
        return []
    keys = []
    for part in raw.split(","):
        part = part.strip()
        if part and part not in keys and " " not in part:
            keys.append(part)
    return keys


def parse_meta(text, task_id):
    fields = {key: _field(text, key) for key in META_KEYS}
    if any(fields[key] is None for key in META_KEYS if key != "endpoint_task_id"):
        return None
    if fields["endpoint_task_id"] not in (None, task_id):
        return None
    if not fields["project"].startswith("/"):
        return None
    for key in ("herdr_session", "herdr_workspace_id", "herdr_tab_id", "herdr_pane_id"):
        if not fields[key] or any(char.isspace() for char in fields[key]):
            return None
    worktree = _field(text, "worktree")
    fields["worktree"] = worktree if worktree and worktree.startswith("/") else None
    pr = _field(text, "pr")
    fields["pr"] = pr if pr and pr.startswith("https://") and "/pull/" in pr else None
    for key in ("harness", "model", "effort"):
        value = (_field(text, key) or "").strip()
        fields[key] = value or None
    fields["task_id"] = task_id
    fields["decision_keys"] = decision_keys(text)
    return fields


def load_metas(home):
    if not home:
        return []
    state = Path(home) / "state"
    if not state.is_dir():
        return []
    metas = []
    for path in sorted(state.glob("*.meta")):
        task_id = path.name[: -len(".meta")]
        if not task_id or "/" in task_id:
            continue
        try:
            parsed = parse_meta(path.read_text(), task_id)
        except OSError:
            continue
        if parsed:
            metas.append(parsed)
    return metas


def display_labels(paths):
    names = {path: Path(path).name or "project" for path in paths}
    counts = {}
    for name in names.values():
        counts[name] = counts.get(name, 0) + 1
    labels = {}
    for path, name in names.items():
        if counts[name] == 1:
            labels[path] = name
        else:
            parent = Path(path).parent.name or "root"
            labels[path] = name + "/" + parent
    again = {}
    for label in labels.values():
        again[label] = again.get(label, 0) + 1
    for path, label in list(labels.items()):
        if again[label] > 1:
            digest = hashlib.sha256(path.encode()).hexdigest()[:6]
            labels[path] = label + "/" + digest
    return labels
