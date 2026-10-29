"""Selected agent workspace: header facts, the Changes tab (file picker and scope/Git
controls, numbered diff, line selection and the inline messages anchored to it), Git confirms."""

import hashlib
import os
import time

from firstpane import gitops
from firstpane.cells import char_width, fit, spread, tail, width
from firstpane.composer_view import locate
from firstpane.diffview import apply_key, push_jump
from firstpane.review import mark_findings, patch_line_paths, skill_catalog
from firstpane.sanitize import sanitize
from firstpane.style import (
    ACCENT,
    ADD_BG,
    ADD_PICK_BG,
    DEL_BG,
    DEL_PICK_BG,
    FIELD_BG,
    GREEN,
    HUNK_BG,
    HUNK_GUTTER_BG,
    INK,
    MENU_BG,
    MUTED,
    PINK,
    SELECTED_BG,
    YELLOW,
    paint,
    rgb_bg,
    rgb_fg,
)
from firstpane.syntax import ROLE_SGR, tokens
from firstpane.workspace import FileEntry, Workspace, commit_list_argv, content_id, parse_commit_list, parse_diff, read_commit_diff, read_commit_parent, read_counts, read_diff, read_status, resolve_workspace

WS_CADENCE = 10
QUIET = ("idle", "done")
GROUP_TITLES = (("conflicted", "Conflicted"), ("staged", "Staged"), ("unstaged", "Unstaged"), ("untracked", "Untracked"))
# key, label, short label (narrow panes), groups shown
SCOPES = (
    ("all", "All changes", "All", ("conflicted", "staged", "unstaged", "untracked")),
    ("workspace", "Workspace", "Work", ("conflicted", "unstaged", "untracked")),
    ("staged", "Staged", "Staged", ("staged",)),
)
SCOPE_EMPTY = {"workspace": "no unstaged or untracked changes", "staged": "nothing staged"}
GIT_KEYS = {"s": "stage", "u": "unstage", "C": "commit", "f": "fetch", "p": "pull", "P": "push"}
GIT_MENU = (("commit", "Commit staged\u2026", "C"), ("fetch", "Fetch", "f"), ("pull", "Pull (fast-forward)", "p"), ("push", "Push", "P"))
MENU_KEYS = {"tab": "files", "w": "scope", "g": "git", ".": "more"}
HIDDEN_HEADERS = ("diff --git ", "index ", "--- ", "+++ ")
DOUBLE_CLICK = 0.5
SCROLL_STEP = 8
WIDE = 60
ADD_COMMENT = "[+] Add comment"
ADD_SHORT = "[+]"
MORE = " \u22ef "
SIGN = {"+": "1;" + rgb_fg(GREEN), "-": "1;" + rgb_fg(PINK), " ": ""}
GUTTER = rgb_fg(MUTED)
GUTTER_PICK = "1;" + rgb_fg(ACCENT)
RULE = rgb_fg(MUTED)
BAR = rgb_fg(ACCENT)
HEADER_STATUS = {
    "working": rgb_fg(GREEN),
    "blocked": rgb_fg(YELLOW),
    "done": rgb_fg(GREEN),
    "idle": rgb_fg(MUTED),
    "agent closed": rgb_fg(PINK),
}


def _edge(left, right, cols):
    """`left` cut short enough that `right` stays whole at the right edge."""
    left = fit(left, max(0, cols - width(right) - (1 if right else 0)))
    return left + " " * max(0, cols - width(left) - width(right)) + right


def _fg(text, sgr, plain):
    """`text` in `sgr`, then back to the surface ink. Unlike `paint`, it keeps the background,
    so it can color text inside a tinted row or a menu."""
    if plain or not sgr or not text:
        return text
    return f"\x1b[{sgr}m{text}\x1b[22;{rgb_fg(INK)}m"


def _band(text, sgr, plain):
    """`text` over a background `sgr` that survives the `_fg` runs inside it."""
    return text if plain else f"\x1b[{sgr}m{text}\x1b[0m"


def _pill(label, plain, shade):
    """A one-row control: accent text on a field, or `[label]` without color."""
    if plain:
        return f"[{label}]"
    return paint(f" {label} ", rgb_fg(ACCENT) + ";" + shade, plain)


def _counts_text(value, plain):
    if value == "binary":
        return _fg("binary", rgb_fg(MUTED), plain)
    if not value:
        return ""
    added, deleted = value
    plus = [_fg(f"+{added}", "1;" + rgb_fg(GREEN), plain)] if added else []
    minus = [_fg(f"-{deleted}", "1;" + rgb_fg(PINK), plain)] if deleted else []
    return " ".join(plus + minus)


def _scope(key):
    return next((scope for scope in SCOPES if scope[0] == key), SCOPES[0])


def _cells(parts, start, room):
    """Cells [start, start + room) of the (text, sgr) runs: (runs, cells used, cut short)."""
    out, used, skipped = [], 0, 0
    for text, sgr in parts:
        kept = []
        for char in text:
            step = char_width(char)
            if skipped < start:
                skipped += step
                continue
            if used + step > room:
                if kept:
                    out.append(("".join(kept), sgr))
                return out, used, True
            kept.append(char)
            used += step
        if kept:
            out.append(("".join(kept), sgr))
    return out, used, False


def _same_row(lines, index, fresh, default):
    """Index in `fresh` of the row `lines[index]` (same kind, numbers and text), else `default`."""
    if index is None or index >= len(lines):
        return default
    old = lines[index]
    for position, line in enumerate(fresh):
        if (line.kind, line.old, line.new, line.text) == (old.kind, old.old, old.new, old.text):
            return position
    return default


class ChangesMixin:
    # ---- workspace reads -------------------------------------------------------------
    def _agent_rows(self):
        rows = []
        for project in self.projects:
            rows.extend(project["agents"])
        rows.extend(self.other_agents)
        return rows

    def _visible_agent_rows(self):
        rows = []
        for project in self.projects:
            if project["key"] not in self.folded_projects:
                rows.extend(project["agents"])
        return rows

    def _target_row(self):
        if not self.target:
            return None
        return next((row for row in self._agent_rows() if row["key"] == self.target["key"]), None)

    def _target_live(self):
        return self._target_row() is not None

    def _ws_info(self, row):
        if not row:
            return None
        return self.agent_ws.get(tuple(row["key"]))

    def _ws_word(self, info):
        if info is None:
            return "loading"
        ws = info.get("workspace")
        if ws is None:
            return "loading"
        if ws.state == "unknown":
            return "workspace unknown"
        if ws.state == "unavailable":
            return "unavailable: " + ws.reason
        if info.get("error"):
            return info["error"]
        if ws.state == "ambiguous" and info.get("status") is None:
            return "workspace ambiguous"
        return "loading"

    def _schedule_workspaces(self, force=False):
        now = self.clock()
        rows = list(self._visible_agent_rows())
        target = self._target_row()
        if target and target not in rows:
            rows.append(target)
        for row in rows:
            key = tuple(row["key"])
            if key in self._ws_jobs:
                continue
            info = self.agent_ws.get(key)
            if not force and info and now - info["at"] < WS_CADENCE:
                continue
            args = (row["project_key"], row.get("recorded"), row.get("herdr_checkout"), row.get("cwd_hint"), now)
            if not row.get("project_key"):
                ws = Workspace("unavailable", reason="no git repository")
            elif not any(args[1:4]):
                ws = Workspace("unknown", reason="workspace unknown")
            else:
                self._ws_jobs[key] = self.submit(lambda args=args: self._ws_read(*args))
                continue
            self.agent_ws[key] = {"workspace": ws, "status": None, "error": "", "at": now}

    def _refresh_target_now(self):
        """Synchronous fresh read of the selected agent's workspace (preview and send)."""
        row = self._target_row()
        if row is None or not row.get("project_key"):
            return
        args = (row.get("recorded"), row.get("herdr_checkout"), row.get("cwd_hint"))
        if any(args):
            self._store_ws(tuple(row["key"]), self._ws_read(row["project_key"], *args, self.clock()))

    def _store_ws(self, key, info):
        old = self.agent_ws.get(key)
        old_ws, ws = (old or {}).get("workspace"), info.get("workspace")
        if old_ws is not None and ws is not None and old_ws.path and old_ws.path == ws.path:
            info["last_counts"] = old.get("counts") or old.get("last_counts")
        self.agent_ws[key] = info

    def _ws_read(self, project_key, recorded, herdr, cwd, now):
        ws = resolve_workspace(self.transport.run, self.env, project_key, recorded=recorded, herdr=herdr, cwd=cwd)
        status, error = (None, "")
        if ws.path:
            status, error = read_status(self.transport.run, ws.path, self.env)
        return {"workspace": ws, "status": status, "error": error, "at": now}

    def _poll_workspaces(self):
        for key, future in list(self._ws_jobs.items()):
            if not future.done():
                continue
            del self._ws_jobs[key]
            try:
                self._store_ws(key, future.result())
            except Exception:
                self.agent_ws[key] = {"workspace": None, "status": None, "error": "unavailable: read failed", "at": self.clock()}
        if self.git_job is not None and self.git_job.done():
            job, self.git_job = self.git_job, None
            try:
                outcome = job.result()
                self.status = outcome.detail
            except Exception:
                self.status = f"{self.git_action} outcome unknown: check the repository before retrying"
            self._schedule_workspaces(force=True)

    def _shared_count(self, path):
        return sum(
            1
            for row in self._agent_rows()
            if (info := self._ws_info(row)) and info.get("workspace") and info["workspace"].path == path
        )

    def _busy_agents(self, path):
        busy = []
        for row in self._agent_rows():
            info = self._ws_info(row)
            if info and info.get("workspace") and info["workspace"].path == path and row.get("status") not in QUIET:
                busy.append(f"{row.get('label')} {row.get('status')}")
        return busy

    # ---- agent row and header facts ---------------------------------------------------
    def _changed_count(self):
        info = self._ws_info(self.target) if self.target else None
        status = info.get("status") if info else None
        return None if status is None else status.changed

    def _header_lines(self, plain, full):
        target = self.target
        if not target:
            return [" no agent selected"] + ([""] if full else [])
        info = self._ws_info(target)
        ws = info.get("workspace") if info else None
        status = info.get("status") if info else None
        project = paint(target.get("project_label") or "[other]", "1;" + rgb_fg(ACCENT), plain)
        label = paint(sanitize(target.get("label")), "1;" + rgb_fg(ACCENT), plain)
        left = " " + project + paint(" / ", rgb_fg(MUTED), plain) + label
        branch = status.branch_label() if status else self._ws_word(info)
        right = paint(branch + " ", rgb_fg(MUTED), plain)
        first = spread(left, right, self.cols)
        if not full:
            return [first]
        live = self._target_live()
        word = target.get("status") if live else "agent closed"
        muted = rgb_fg(MUTED)
        parts = [paint(word, HEADER_STATUS.get(word, rgb_fg(PINK)), plain)]
        if status is not None:
            parts.append(paint(f"{status.changed} file" + ("" if status.changed == 1 else "s"), muted, plain))
        else:
            parts.append(paint("? files", muted, plain))
        if ws is not None and ws.path:
            if ws.state == "ambiguous":
                parts.append(paint("workspace ambiguous", rgb_fg(PINK), plain))
            shared = self._shared_count(ws.path)
            if shared > 1:
                parts.append(paint(f"shared by {shared} agents", muted, plain))
            home = os.path.expanduser("~")
            where = "~" + ws.path[len(home) :] if home != "~" and ws.path.startswith(home + "/") else ws.path
            parts.append(paint(f"{where} ({ws.provenance})", muted, plain))
        elif info is not None:
            parts.append(paint(self._ws_word(info), muted, plain))
        return [first, " " + paint(" · ", muted, plain).join(parts)]

    # ---- Changes tab -----------------------------------------------------------------
    def _change_entries(self):
        """The selected agent's changed files in the chosen scope, grouped in GROUP_TITLES order."""
        info = self._ws_info(self.target) if self.target else None
        status = info.get("status") if info else None
        if status is None:
            return []
        groups = _scope(self.changes_scope)[3]
        ordered = []
        for group, _ in GROUP_TITLES:
            if group in groups:
                ordered.extend(entry for entry in status.entries if entry.group == group)
        return ordered

    def _counts(self, info, status):
        """Per-file +/- counts of the selected agent, read once per workspace status read in
        a worker; the last known counts show until that read lands."""
        if info.get("counts") is None:
            job = info.get("counts_job")
            if job is None:
                path, entries = info["workspace"].path, status.entries
                job = info["counts_job"] = self.submit(lambda: read_counts(self.transport.run, path, entries, self.env))
            if not job.done():
                shown = {(entry.group, entry.path) for entry in status.entries}
                return {name: value for name, value in (info.get("last_counts") or {}).items() if name in shown}
            try:
                info["counts"] = job.result()
            except Exception:
                info["counts"] = {}
        return info["counts"]

    def _changes_body(self, plain, budget):
        info = self._ws_info(self.target) if self.target else None
        status = info.get("status") if info else None
        if status is None or not info.get("workspace") or not info["workspace"].path:
            rows = [(" no agent selected" if not self.target else " " + self._ws_word(info), None)]
            rows.extend(self._earlier_rows(plain))
            return [text for text, _ in rows], [hit for _, hit in rows]
        self._ensure_diff()
        entries = self._change_entries()
        counts = self._counts(info, status)
        drafts = self._agent_drafts("diff")
        rows = self._toolbar(status, entries, counts, drafts, plain)
        if budget < 6:
            rows = rows[:1]
        if self.changes_menu is not None and self.focus == "list":
            rows += self._menu_rows(plain, max(1, budget - len(rows) - 2))
        rows.extend(self._earlier_rows(plain))
        changed = {(entry.group, entry.path) for entry in status.entries}
        for draft in drafts:
            if draft["anchor"].get("group") == "commit":
                continue
            if (draft["anchor"].get("group"), draft["anchor"].get("path")) not in changed and not self._footer_holds(draft):
                rows.extend(self._inline_rows(draft, plain, 1, stale=True))
        avail = budget - len(rows)
        if avail > 0 and self.diff_view is not None:
            rows += self._diff_rows(plain, avail, drafts)
        elif avail > 0 and not entries:
            empty = SCOPE_EMPTY.get(self.changes_scope, "") if status.entries else ""
            rows.append((" " + (empty or "clean: no changes"), None))
        return [text for text, _ in rows], [hit for _, hit in rows]

    def _shades(self):
        return {
            "pill": rgb_bg(FIELD_BG),
            "menu": rgb_bg(MENU_BG),
            "pick": rgb_bg(SELECTED_BG),
            "hunk": rgb_fg(MUTED) + ";" + rgb_bg(HUNK_BG),
            "hunk_gutter": rgb_bg(HUNK_GUTTER_BG),
            "+": rgb_bg(ADD_BG),
            "-": rgb_bg(DEL_BG),
            "+pick": rgb_bg(ADD_PICK_BG),
            "-pick": rgb_bg(DEL_PICK_BG),
        }

    def _toolbar(self, status, entries, counts, drafts, plain):
        """Two control rows: the file picker with the file's place in the scope, then the
        scope and Git menus (top row when wide), the file's +/- lines, Stage and ⋯."""
        cols, wide = self.cols, self.cols >= WIDE
        entry = None if self.revision_oid else entries[self.changes_cursor] if entries else None
        scope = _scope(self.changes_scope)
        shade = self._shades()["pill"]
        scope_pill = ("scope", _pill((scope[1] if wide else scope[2]) + " \u25be", plain, shade))
        git_pill = ("git", _pill("Git \u25be", plain, shade))
        waiting = sum(self._draft_counts(drafts).values())
        place = f"{self.changes_cursor + 1} / {len(entries)}" if entries else "0 / 0"
        after = "  " + paint(place, rgb_fg(MUTED), plain) + ("  " + paint(f"\u270e{waiting}", rgb_fg(ACCENT), plain) if waiting else "")
        top_right = [scope_pill, git_pill] if wide else []
        room = cols - 1 - width(after) - sum(width(text) + 1 for _, text in top_right) - 1 - 4
        label = f"commit {self.revision_oid[:7]}" if self.revision_oid else sanitize(entry.path) if entry else "no files"
        if width(label) > room:
            label = "\u2026" + tail(label, max(1, room - 1))
        self._bar_spans = {}
        first = self._control_row([("files", _pill(label + " \u25be", plain, shade))], after, top_right)
        facts = []
        if entry is not None:
            value = counts.get((entry.group, entry.path))
            if isinstance(value, tuple):
                facts.append(" " + paint(f"+{value[0]}", "1;" + rgb_fg(GREEN), plain) + " " + paint(f"-{value[1]}", "1;" + rgb_fg(PINK), plain))
            elif value == "binary":
                facts.append(" " + paint("binary", rgb_fg(MUTED), plain))
            note = entry.group + (f" · from {sanitize(entry.orig)}" if entry.orig else "")
            facts.append("  " + paint(note, rgb_fg(MUTED), plain))
        right = [] if wide else [scope_pill, git_pill]
        if entry is not None:
            word = "Unstage" if entry.group == "staged" else "Stage"
            right.append(("stage", _pill(word + (" file" if wide else ""), plain, shade)))
        right.append(("more", MORE))
        for name in ("stage", "git", "scope"):
            if sum(width(text) + 1 for _, text in right) + 1 <= cols:
                break
            right = [item for item in right if item[0] != name]
        # The group note, then the counts, give way whole when the row is short: never a cut number.
        room = cols - 2 - sum(width(text) + 1 for _, text in right)
        while len(facts) > 1 and width("".join(facts)) > room:
            facts.pop()
        second = self._control_row([], "".join(facts) if facts and width(facts[0]) <= room else "", right)
        return [first, second]

    def _control_row(self, left_items, after, right_items):
        """One row of clickable controls: `left_items` from the left edge followed by `after`,
        `right_items` right-aligned; spans are 0-based cells [start, end)."""
        spans, at, left = [], 1, " "
        for name, text in left_items:
            spans.append((at, at + width(text), name))
            left += text
            at += width(text)
        right = "".join(text + " " for _, text in right_items)
        at = self.cols - width(right)
        for name, text in right_items:
            spans.append((at, at + width(text), name))
            at += width(text) + 1
        self._bar_spans.update({name: (start, end) for start, end, name in spans})
        return _edge(left + after, right, self.cols), {"kind": "changes-bar", "spans": spans}

    # ---- the Changes menus (file picker, scope, Git, ⋯) ------------------------------
    def _menu_items(self, kind, plain):
        """[(label, right, action)] of a menu; a None action is a heading or a fact."""
        info = self._ws_info(self.target) if self.target else None
        status = info.get("status") if info else None
        if status is None or not info.get("workspace") or not info["workspace"].path:
            return []
        entries = self._change_entries()
        entry = entries[min(self.changes_cursor, len(entries) - 1)] if entries else None
        items = []
        if kind == "files":
            counts = self._counts(info, status)
            waiting = self._draft_counts(self._agent_drafts("diff"))
            known = [value for value in (counts.get((item.group, item.path)) for item in entries) if isinstance(value, tuple)]
            total = f"{len(entries)} file" + ("" if len(entries) == 1 else "s")
            if known:
                total += f"  +{sum(value[0] for value in known)} -{sum(value[1] for value in known)}"
            items.append((_fg(f"{_scope(self.changes_scope)[1]} · {total}", rgb_fg(MUTED), plain), "", None))
            for group, title in GROUP_TITLES:
                members = [item for item in entries if item.group == group]
                if not members:
                    continue
                items.append((_fg(f"{title} ({len(members)})", "1;" + rgb_fg(INK), plain), "", None))
                for item in members:
                    lead = f"\u2713 {item.code}  " if item is entry else f"  {item.code}  "
                    shown = sanitize(item.path) + (f" <- {sanitize(item.orig)}" if item.orig else "")
                    right = _counts_text(counts.get((item.group, item.path)), plain)
                    if waiting.get((item.group, item.path)):
                        right = _fg(f"\u270e{waiting[(item.group, item.path)]}", rgb_fg(ACCENT), plain) + ("  " + right if right else "")
                    room = self.cols - 2 - width(right) - 1 - width(lead)
                    if width(shown) > room:
                        shown = "\u2026" + tail(shown, max(1, room - 1))
                    items.append((lead + shown, right, ("file", item.group, item.path)))
        elif kind == "scope":
            for key, label, _, groups in SCOPES:
                count = len({item.path for item in status.entries if item.group in groups})
                items.append((("\u2713 " if key == self.changes_scope else "  ") + label, str(count), ("scope", key)))
        elif kind == "git":
            if status.upstream:
                fact = f"{sanitize(status.upstream)} \u2191{status.ahead or 0} \u2193{status.behind or 0}"
            else:
                fact = "no upstream"
            items.append((_fg(fact, rgb_fg(MUTED), plain), "", None))
            items.extend((label, key, ("git", action)) for action, label, key in GIT_MENU)
        elif kind == "revision":
            items.append(("\u2713 Uncommitted" if not self.revision_oid else "  Uncommitted", "", ("rev", None)))
            result = self.transport.run(commit_list_argv(info["workspace"].path), env=self.env, timeout=5, cwd=info["workspace"].path)
            note = "excluding remotes" if result and "--remotes" in (result.stdout or "") else ""
            if note:
                items.append((_fg(note, rgb_fg(MUTED), plain), "", None))
            for row in parse_commit_list((result.stdout if result else "") or ""):
                mark = "\u2713 " if row["oid"] == self.revision_oid else "  "
                items.append((mark + row["subject"], row["oid"][:7], ("rev", row["oid"])))
        elif kind == "more":
            if entry is not None and not self.revision_oid:
                staged = entry.group == "staged"
                items.append(("Unstage file" if staged else "Stage file", "u" if staged else "s", ("git", "unstage" if staged else "stage")))
            if self.diff_view is not None and self.diff_view["lines"]:
                items.append(("Add comment on selection", "Enter", ("comment",)))
                items.append(("Start or drop a line range", "v", ("range",)))
            if len(entries) > 1:
                items.append(("Previous file", "[", ("step", -1)))
                items.append(("Next file", "]", ("step", 1)))
            items.append(("Revisions", "", ("revisions",)))
            items.append(("Refresh", "r", ("refresh",)))
            items.extend((label, "", action) for label, action in self._review_choices())
        return items

    def _review_choices(self):
        """[(label, action)] of the skill toggles and Run review; none without a review command."""
        review = getattr(self.config, "review", None)
        if review is None:
            return []
        rows = []
        for skill in skill_catalog(review.skills, (self.diff_view or {}).get("workspace")):
            name = skill["path"].rstrip("/").split("/")[-1]
            mark = "\u2713 " if skill["path"] in self.review_skills else "  "
            note = "" if skill["usable"] else " disabled"
            rows.append((mark + name + note, ("skill", skill["path"]) if skill["usable"] else None))
        rows.append(("Run review", ("review",)))
        return rows

    def _toggle_skill(self, path):
        if path in self.review_skills:
            self.review_skills = [item for item in self.review_skills if item != path]
        else:
            self.review_skills = [*self.review_skills, path]

    def _toggle_menu(self, kind):
        if self.changes_menu is not None and self.changes_menu["kind"] == kind:
            self.changes_menu = None
            return
        items = self._menu_items(kind, True)
        picks = [index for index, item in enumerate(items) if item[2] is not None]
        if not picks:
            self.changes_menu = None
            self.status = "no changed files" if kind == "files" else "nothing to choose yet"
            return
        current = next((index for index in picks if items[index][0].startswith("\u2713")), picks[0])
        self.changes_menu = {"kind": kind, "cursor": current}

    def _menu_rows(self, plain, budget):
        """The open menu drawn under the controls: the file picker spans the pane, the other
        menus hang under their control's right edge."""
        menu = self.changes_menu
        items = self._menu_items(menu["kind"], plain)
        if not items:
            self.changes_menu = None
            return []
        menu["cursor"] = min(menu["cursor"], len(items) - 1)
        cols = self.cols
        if menu["kind"] == "files":
            start, end = 0, cols
        else:
            span = max(width(label) + 2 + width(right) for label, right, _ in items) + 4
            end = self._bar_spans.get(menu["kind"], (0, cols))[1]
            end = min(cols, max(end, span))
            start = max(0, end - span)
        inner = max(1, end - start - 2)
        height = max(1, budget)
        top = max(0, min(menu["cursor"] - height + 2, len(items) - height))
        rows = []
        for index in range(top, min(len(items), top + height)):
            label, right, action = items[index]
            text = _edge(label, right, inner)
            picked = index == menu["cursor"]
            if plain:
                line = " " * start + (">" if picked else " ") + text + " "
            else:
                line = " " * start + _band(" " + text + " ", self._shades()["pick" if picked else "menu"], plain)
            rows.append((line, {"kind": "changes-menu", "index": index, "span": (start, end), "action": action}))
        return rows

    def _menu_key(self, key):
        """Keys while a menu is open; False (menu closed) lets the key act as usual."""
        menu = self.changes_menu
        items = self._menu_items(menu["kind"], True)
        picks = [index for index, item in enumerate(items) if item[2] is not None]
        if key == "esc" or MENU_KEYS.get(key) == menu["kind"] or not picks:
            self.changes_menu = None
            return key == "esc" or MENU_KEYS.get(key) == menu["kind"]
        if key in ("j", "down", "pagedown"):
            later = [index for index in picks if index > menu["cursor"]]
            menu["cursor"] = (later[-1] if key == "pagedown" else later[0]) if later else picks[-1]
        elif key in ("k", "up", "pageup"):
            earlier = [index for index in picks if index < menu["cursor"]]
            menu["cursor"] = (earlier[0] if key == "pageup" else earlier[-1]) if earlier else picks[0]
        elif key == "enter" and menu["cursor"] in picks:
            self._menu_pick(items[menu["cursor"]][2])
        else:
            self.changes_menu = None
            return False
        return True

    def _menu_pick(self, action):
        self.changes_menu = None
        kind = action[0]
        if kind == "file":
            entries = self._change_entries()
            index = next((at for at, item in enumerate(entries) if (item.group, item.path) == action[1:]), None)
            if index is None:
                self.status = "that file is no longer in this list"
            else:
                self._pick_file(index)
        elif kind == "scope":
            if action[1] != self.changes_scope:
                self._close_inline()
                self.changes_scope = action[1]
                self._ensure_diff()
        elif kind == "git":
            self._arm_git(action[1])
        elif kind == "comment" and self.diff_view is not None:
            self._open_diff_message()
        elif kind == "range" and self.diff_view is not None:
            self._diff_key("v")
        elif kind == "step":
            self._step_file(action[1])
        elif kind == "revisions":
            self.changes_menu = {"kind": "revision", "cursor": 0}
            return
        elif kind == "rev":
            self.revision_oid = action[1]
            info = self._ws_info(self.target) if self.target else None
            ws = info.get("workspace") if info else None
            self.revision_parent = read_commit_parent(self.transport.run, ws.path, action[1], self.env) if action[1] and ws and ws.path else None
            self.diff_view = None
            self._ensure_diff()
        elif kind == "refresh":
            self.refresh(manual=True)
        elif kind == "skill":
            self._toggle_skill(action[1])
        elif kind == "review":
            self._arm_review()

    def _pick_file(self, index):
        entries = self._change_entries()
        if not entries:
            return
        index = max(0, min(index, len(entries) - 1))
        if index != self.changes_cursor:
            self._close_inline()
        self.changes_cursor = index
        self.changes_pick = (entries[index].group, entries[index].path)
        self._ensure_diff()

    def _step_file(self, step):
        self._pick_file(self.changes_cursor + step)

    def _earlier_rows(self, plain):
        draft = self._earlier_draft()
        if draft is None:
            return []
        if draft is self.draft:
            return [] if self._footer_holds(draft) else self._inline_rows(draft, plain, 1)
        first = sanitize((draft.get("body") or "").strip().split("\n")[0])
        text = " " + paint("! unsent message to FirstMate", "1;" + rgb_fg(YELLOW), plain) + f" · m opens it · {first}"
        return [(text + "  " + paint(f"({self._inline_label(draft)})", rgb_fg(MUTED), plain), {"kind": "draft", "target": draft["target"]})]

    def _draft_counts(self, drafts):
        """{(group, path): open drafts with text} of the selected agent."""
        per_file = {}
        for draft in drafts:
            if draft is self.draft and not self.composer.text.strip():
                continue
            key = (draft["anchor"].get("group"), draft["anchor"].get("path"))
            per_file[key] = per_file.get(key, 0) + 1
        return per_file

    # ---- the selected file's diff ----------------------------------------------------
    def _ensure_diff(self):
        """Load the selected file's diff (or the chosen commit's); re-read it when the file or the
        workspace status changes, keeping the file (by group and path, else by path), the cursor
        and the range on the same rows when they still exist."""
        if self.review_source == "pr":
            return
        entries = self._change_entries()
        info = self._ws_info(self.target) if self.target else None
        ws = info.get("workspace") if info else None
        oid = self.revision_oid
        if ws is None or not ws.path or (not entries and not oid):
            self.diff_view = None
            return
        if oid:
            entry = FileEntry("commit", "C", f"commit {oid[:7]}")
        else:
            pick = self.changes_pick
            found = [index for index, item in enumerate(entries) if pick and (item.group, item.path) == pick]
            found = found or [index for index, item in enumerate(entries) if pick and item.path == pick[1]]
            self.changes_cursor = found[0] if found else min(self.changes_cursor, len(entries) - 1)
            entry = entries[self.changes_cursor]
            self.changes_pick = (entry.group, entry.path)
        key = (tuple(self.target["key"]), ws.path, entry, None if oid else info.get("at"), oid)
        view = self.diff_view
        if view is not None and view["key"] == key:
            return
        job = self._diff_job
        if job is None or job[0] != key:
            path = ws.path
            if oid:
                job = self._diff_job = (key, self.submit(lambda: read_commit_diff(self.transport.run, path, oid, self.env)))
            else:
                job = self._diff_job = (key, self.submit(lambda: read_diff(self.transport.run, path, entry, self.env)))
        same_file = view is not None and view["entry"] == entry and view["workspace"] == ws.path
        if not job[1].done():
            if not same_file:
                self.diff_view = self._diff_view(None, entry, ws.path, info, [], "reading diff...")
            return
        self._diff_job = None
        try:
            text, error = job[1].result()
        except Exception:
            text, error = "", "unavailable: git diff failed"
        parsed = parse_diff(text)
        paths = patch_line_paths(text) if oid else [None] * len(parsed)
        kept = [(line, path) for line, path in zip(parsed, paths) if not (line.kind == "h" and line.text.startswith(HIDDEN_HEADERS))]
        lines = [line for line, _ in kept]
        cursor = next((index for index, line in enumerate(lines) if line.kind in "+-"), 0)
        mark, top, xoff = None, 0, 0
        if same_file and view["key"] is not None:
            cursor = _same_row(view["lines"], view["cursor"], lines, min(view["cursor"], max(0, len(lines) - 1)))
            mark = _same_row(view["lines"], view["mark"], lines, None)
            top, xoff = view["top"], view.get("xoff", 0)
        self.diff_view = self._diff_view(key, entry, ws.path, info, lines, error, cursor, mark, top)
        self.diff_view["xoff"] = xoff
        self.diff_view["patch"] = text
        self.diff_view["paths"] = [path for _, path in kept] if oid else None
        self.diff_view["revision"] = oid or "uncommitted"

    def _diff_view(self, key, entry, path, info, lines, error, cursor=0, mark=None, top=0):
        status = info.get("status")
        return {
            "key": key,
            "entry": entry,
            "lines": lines,
            "cursor": cursor,
            "mark": mark,
            "top": top,
            "error": error,
            "workspace": path,
            "head": status.oid if status else None,
            "branch": status.branch_label() if status else None,
        }

    def _selection(self):
        view = self.diff_view
        cursor, mark = view["cursor"], view.get("mark")
        return (min(mark, cursor), max(mark, cursor)) if mark is not None else (cursor, cursor)

    def _diff_rows(self, plain, budget, drafts):
        """A window of numbered diff lines with the drafts anchored under their last selected
        line; drafts whose lines changed come first, as stale."""
        view = self.diff_view
        entry, lines = view["entry"], view["lines"]
        if view.get("error"):
            return [(" " + view["error"], None)][:budget]
        if not lines:
            return [(" " + paint("no line changes to show", rgb_fg(MUTED), plain), None)]
        numbers = [number for line in lines for number in (line.old, line.new) if number is not None]
        digits = max(2, len(str(max(numbers, default=0))))
        indent = 2 * digits + 4
        after, display = {}, []
        for draft in drafts:
            anchor = draft["anchor"]
            if view.get("paths"):
                if anchor.get("group") != "commit" or anchor.get("revision") != view["revision"] or anchor.get("path") not in view["paths"]:
                    continue
            elif (anchor.get("group"), anchor.get("path")) != (entry.group, entry.path):
                continue
            if view.get("paths"):
                own = [at for at, path in enumerate(view["paths"]) if path == anchor.get("path")]
                found = locate(anchor, [lines[at] for at in own])
                span = (own[found[0]], own[found[1]]) if found else None
            else:
                span = locate(anchor, lines)
            if self._footer_holds(draft):
                continue
            if span is None:
                display.extend(self._inline_rows(draft, plain, indent, stale=True))
            else:
                after.setdefault(span[1], []).append(draft)
        places = {}
        for index in range(len(lines)):
            places[index] = len(display)
            display.append(index)
            for draft in after.get(index, ()):
                display.extend(self._inline_rows(draft, plain, indent))
        low, high = self._selection()
        active = self.draft.get("target") if self.draft else None
        box = [
            position
            for position, item in enumerate(display)
            if active and isinstance(item, tuple) and item[1] and item[1].get("target") == active
        ]
        need = box + ([places[low]] if low in places else []) if box else [places.get(view["cursor"], 0)]
        rows = max(1, budget)
        top = view.get("top", 0)
        lo, hi = min(need), max(need)
        if hi - lo + 1 > rows:
            top = hi - rows + 1
        elif lo < top:
            top = lo
        elif hi >= top + rows:
            top = hi - rows + 1
        top = max(0, min(top, len(display) - rows))
        view["top"] = top
        icon_at = None
        if self.focus == "list" and self.changes_menu is None and self.review_source == "workspace":
            icon_at = next((index for index in range(low, high + 1) if index < len(lines) and lines[index].kind in "+- "), None)
        out = []
        for item in display[top : top + rows]:
            if isinstance(item, tuple):
                out.append(item)
            else:
                out.append(self._diff_line(item, digits, (low, high), item == icon_at, plain))
        return out

    def _diff_line(self, index, digits, selection, icon, plain):
        """One diff row: old and new line numbers, a rule (an accent bar on selected lines),
        the sign, then the code, tinted to the pane edge on added and deleted lines."""
        view = self.diff_view
        line = view["lines"][index]
        code_path = None if plain else self._line_path(view, index)
        cols = self.cols
        gutter_width = 2 * digits + 4
        hit = {"kind": "diffline", "index": index}
        if line.kind == "@":
            text = fit("  " + sanitize(line.text), max(0, cols - gutter_width))
            text += " " * (cols - gutter_width - width(text))
            shades = self._shades()
            return paint(" " * gutter_width, shades["hunk_gutter"], plain) + paint(text, shades["hunk"], plain), hit
        if line.kind not in "+- ":
            return " " * (gutter_width + 3) + paint(sanitize(line.text), rgb_fg(MUTED), plain), hit
        picked = selection[0] <= index <= selection[1]
        old = str(line.old) if line.old is not None else "-"
        new = str(line.new) if line.new is not None else "-"
        gutter = paint(f" {old:>{digits}}  {new:>{digits}} ", GUTTER_PICK if picked else GUTTER, plain)
        rule = paint("\u258c", BAR, plain) if picked else paint("\u2502", RULE, plain)
        offer = ""
        if icon:
            offer = f" {ADD_COMMENT} " if cols - gutter_width >= 40 else f" {ADD_SHORT} "
            hit["icon"] = cols - width(offer) + 2
        marker = self._finding_marker(index)
        if marker:
            offer = marker + offer
        room = max(0, cols - gutter_width - 4 - width(offer))
        code = sanitize(line.text).expandtabs(4)
        xoff = view.get("xoff", 0)
        lead = "\u2026" if xoff and room and width(code) > xoff else ""
        parts = [(text, ROLE_SGR.get(role, "")) for text, role in tokens(code, code_path)]
        shown, used, cut = _cells(parts, xoff, room - width(lead))
        if cut:
            shown, used, _ = _cells(parts, xoff, max(0, room - width(lead) - 1))
            shown.append(("\u2026", GUTTER))
            used += 1
        body = f" {_fg(line.kind, SIGN[line.kind], plain)} " + lead
        body += "".join(_fg(text, sgr, plain) for text, sgr in shown)
        body += " " * max(0, room - width(lead) - used) + _fg(offer, BAR, plain)
        if line.kind in "+-":
            body = _band(body, self._shades()[line.kind + ("pick" if picked else "")], plain)
        return gutter + rule + body, hit

    # ---- keys and mouse --------------------------------------------------------------
    def _changes_key(self, key):
        self._ensure_diff()
        if self.changes_menu is not None and self._menu_key(key):
            return True
        earlier = self._earlier_draft()
        if key in MENU_KEYS:
            self._toggle_menu(MENU_KEYS[key])
        elif key in ("[", "]"):
            self._step_file(-1 if key == "[" else 1)
        elif key == "m" and earlier is not None:
            self._open_target(earlier["target"])
        elif key in GIT_KEYS:
            self._arm_git(GIT_KEYS[key])
        elif self.diff_view is None or not self._diff_key(key):
            return False
        self._ensure_diff()
        return True

    def _diff_key(self, key):
        view = self.diff_view
        if key in ("enter", "m", "ctrl-enter"):
            self._open_diff_message()
            return True
        before = self._jump_frame()
        view["jumps"] = self.review_jumps
        moved = key == "G" or (key == "g" and view.get("prefix") == "g")
        result = apply_key(view, key, rows=self.rows)
        if result["status"]:
            self.status = result["status"]
        if moved and before is not None and view["cursor"] != before[3]:
            push_jump(view, before)
        if result["jumped"]:
            self._restore_jump(result["jumped"])
        if result["handled"]:
            self._note_finding()
        return result["handled"]

    def _line_path(self, view, index):
        """The file a diff row belongs to: its own header's path in a multi-file patch."""
        paths = view.get("paths")
        return (paths[index] if paths and index < len(paths) else None) or view["entry"].path

    def _jump_frame(self):
        view = self.diff_view
        lines = (view or {}).get("lines")
        if not lines:
            return None
        cursor = min(max(view.get("cursor") or 0, 0), len(lines) - 1)
        return (self.review_source, self._review_revision(), self._line_path(view, cursor), cursor, view.get("xoff", 0))

    def _record_jump(self):
        frame = self._jump_frame()
        if frame is not None:
            self.review_jumps.append(frame)

    def _jump_back(self):
        if not self.review_jumps:
            self.status = "no earlier position"
            return
        self._restore_jump(self.review_jumps.pop())

    def _finding_at(self, index):
        view = self.diff_view or {}
        if not self.review_findings or not view.get("lines") or self.review_origin != self._review_origin():
            return None
        line = view["lines"][index]
        path = self._line_path(view, index)
        for finding in self.review_findings:
            number = line.new if finding.get("side") == "new" else line.old
            if finding.get("path") == path and finding.get("start_line") == number:
                return finding
        return None

    def _finding_marker(self, index):
        finding = self._finding_at(index)
        if finding is None:
            return ""
        return " stale " if finding.get("stale") else " ! "

    def _note_finding(self):
        view = self.diff_view or {}
        lines = view.get("lines") or []
        if not lines or self.review_origin != self._review_origin():
            return
        recorded = getattr(self, "review_head", None)
        current = view.get("head")
        scope = getattr(self, "review_scope_name", "uncommitted")
        listed = True
        if scope == "pr_commit":
            commits = (((self._pr_detail(self._current_pr()) or {}).get("view")) or {}).get("commits")
            listed = commits is None or any(item.get("oid") == recorded for item in commits)
        self.review_findings = mark_findings(
            self.review_findings,
            scope=scope,
            head_moved=bool(recorded and current and recorded != current),
            listed=listed,
        )
        finding = self._finding_at(min(view.get("cursor") or 0, len(lines) - 1))
        if finding is not None:
            self.status = finding.get("stale") or finding.get("title") or "finding"

    def _restore_jump(self, frame):
        source, revision, path, cursor, xoff = frame
        if source != self.review_source:
            self._set_review_source(source, record=False)
        view = self.diff_view
        lines = (view or {}).get("lines")
        if not lines or view.get("revision") != revision or self._line_path(view, min(cursor, len(lines) - 1)) != path:
            self.status = "earlier position is no longer open"
            return
        view["cursor"] = min(cursor, len(lines) - 1)
        view["mark"] = None
        view["xoff"] = xoff
        view["col"] = xoff
        if self.review_source == "pr":
            self.pr_diff_open = True

    def _changes_mouse(self, hit, column, gesture):
        """`press`, `shift` (Shift-click) or `drag` on a control, a menu row or a diff line."""
        self._ensure_diff()
        kind = hit.get("kind")
        if kind in ("changes-bar", "changes-menu"):
            if gesture == "press":
                self._last_press = None
                self._close_inline()
                cell = column - 1
                if kind == "changes-bar":
                    self._click_control(next((name for start, end, name in hit["spans"] if start <= cell < end), None))
                elif self.changes_menu is not None and hit["action"] and hit["span"][0] <= cell < hit["span"][1]:
                    self._menu_pick(hit["action"])
                else:
                    self.changes_menu = None
            return
        if gesture == "press":
            self.changes_menu = None
        view = self.diff_view
        if kind != "diffline" or view is None or hit["index"] >= len(view["lines"]):
            return
        index = hit["index"]
        if gesture == "drag":
            if self._last_press is None:
                return
            self._last_press = (0, None)
            if view.get("mark") is None:
                view["mark"] = view["cursor"]
            view["cursor"] = index
            return
        if gesture == "press" and hit.get("icon") and column >= hit["icon"]:
            self._open_diff_message()
            return
        self._close_inline()
        if gesture == "shift":
            if view.get("mark") is None:
                view["mark"] = view["cursor"]
            view["cursor"] = index
            self._last_press = None
            return
        now = self.clock()
        double = self._last_press is not None and self._last_press[1] == index and now - self._last_press[0] <= DOUBLE_CLICK
        self._last_press = None if double else (now, index)
        view["cursor"] = index
        view["mark"] = None
        if double:
            self._open_diff_message()

    def _click_control(self, name):
        if name in ("files", "scope", "git", "more"):
            self._toggle_menu(name)
            return
        self.changes_menu = None
        if name == "stage":
            entries = self._change_entries()
            if entries:
                self._arm_git("unstage" if entries[self.changes_cursor].group == "staged" else "stage")

    def _open_diff_message(self):
        """Open the inline message for every selected diff line."""
        view = self.diff_view
        if self.review_source == "pr":
            self.status = "messages attach to the workspace diff, not the PR diff"
            return
        if view.get("error") or not view["lines"]:
            self.status = "no diff lines to write about"
            return
        low, high = self._selection()
        chosen = [line for line in view["lines"][low : high + 1] if line.kind in "+- "]
        if not chosen:
            self.status = "select diff lines (not headers) to write about"
            return
        olds = [line.old for line in chosen if line.kind in "- "]
        news = [line.new for line in chosen if line.kind in "+ "]
        side = "new" if any(line.kind == "+" for line in chosen) or not any(line.kind == "-" for line in chosen) else "old"
        numbers = news if side == "new" else olds
        entry = view["entry"]
        path = entry.path
        if view.get("paths"):
            paths = {self._line_path(view, index) for index in range(low, high + 1) if view["lines"][index].kind in "+- "}
            if len(paths) != 1:
                self.status = "select lines of one file to write about"
                return
            path = paths.pop()
        record = {
            "kind": "diff",
            "path": path,
            "orig": entry.orig,
            "code": entry.code,
            "group": entry.group,
            "side": side,
            "start": min(numbers),
            "end": max(numbers),
            "old_start": min(olds) if olds else None,
            "old_end": max(olds) if olds else None,
            "new_start": min(news) if news else None,
            "new_end": max(news) if news else None,
            "head": view.get("head"),
            "branch": view.get("branch"),
            "source": "workspace",
            "revision": view.get("revision") or "uncommitted",
            "blob": content_id(self.transport.run, view["workspace"], entry, self.env, side=side),
            "workspace": view["workspace"],
            "rows": [[line.kind, line.old, line.new, line.text] for line in chosen],
            "lines": [line.kind + line.text for line in chosen],
            "lines_total": len(chosen),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self._open_inline(record)

    # ---- Git actions -----------------------------------------------------------------
    def _arm_git(self, action):
        self._poll_workspaces()
        if self.git_job is not None:
            self.status = f"{action} refused: {self.git_action} still running"
            return
        if not self.target:
            self.status = f"{action}: no agent selected"
            return
        info = self._ws_info(self.target)
        ws = info.get("workspace") if info else None
        if ws is None or not ws.path:
            self.status = f"{action} refused: {self._ws_word(info)}"
            return
        if not ws.writable:
            self.status = f"{action} refused: {ws.reason or 'workspace is read-only'}"
            return
        if not self._target_live():
            self.status = f"{action} refused: agent closed"
            return
        paths = []
        message_path = None
        if action in ("stage", "unstage"):
            if self.revision_oid:
                self.status = f"{action}: choose Uncommitted first"
                return
            entries = self._change_entries()
            if not entries:
                self.status = f"{action}: no file selected"
                return
            paths = [entries[min(self.changes_cursor, len(entries) - 1)].path]
        if action == "commit":
            message_path = self._commit_message(ws.path)
            if message_path is None:
                return
        plan = gitops.prepare(
            action,
            self.transport.run,
            self.env,
            ws.path,
            paths=paths,
            message_path=message_path,
            preferred_remote=self.config.remotes.get(self.target.get("project_key"), "origin"),
            busy_agents=self._busy_agents(ws.path) if action == "pull" else (),
        )
        self.pending = {"plan": plan, "target_key": tuple(self.target["key"])}
        self.screen = "confirm-git"

    def _commit_message(self, ws_path):
        digest = hashlib.sha256(ws_path.encode()).hexdigest()[:16]
        draft = self.state_dir / "commit" / f"draft-{digest}.txt"
        draft.parent.mkdir(parents=True, exist_ok=True)
        if not draft.exists():
            draft.write_text("")
        body = self._edit(draft)
        if body is None:
            return None
        draft.write_text(body)
        if not body.strip():
            self.status = "commit cancelled: empty message"
            return None
        frozen = draft.parent / f"message-{hashlib.sha256(body.encode()).hexdigest()[:16]}.txt"
        frozen.write_text(body)
        return str(frozen)

    def _confirm_git(self, plain):
        plan = (self.pending or {}).get("plan")
        if plan is None:
            return ""
        title = f"{plan.action} in {self.target.get('label') if self.target else '?'}"
        lines = [paint(title, "1", plain)] + [sanitize(line) for line in plan.preview]
        if plan.refusal:
            lines.append("any key returns")
        else:
            lines.append("y runs it, any other key cancels")
        return "\n".join(lines)

    def _fresh_workspace(self):
        """Re-prove the selected agent and its workspace right before a write."""
        row = self._target_row()
        if row is None:
            return None, "agent closed"
        reason = self._verify_live(row)
        if reason:
            return None, reason
        self._load_snapshot()
        row = self._target_row()
        if row is None:
            return None, "agent closed"
        ws = resolve_workspace(
            self.transport.run,
            self.env,
            row["project_key"],
            recorded=row.get("recorded"),
            herdr=row.get("herdr_checkout"),
            cwd=row.get("cwd_hint"),
        )
        if ws.state != "ok" or not ws.writable:
            return None, ws.reason or ws.state
        return ws, ""

    def _run_git(self):
        pending = self.pending or {}
        plan = pending.get("plan")
        self.screen = "tree"
        self.pending = None
        if plan is None or plan.refusal:
            return
        if not self.target or tuple(self.target["key"]) != pending.get("target_key"):
            self.status = f"{plan.action} refused: selected agent changed"
            return
        ws, reason = self._fresh_workspace()
        if ws is None:
            self.status = f"{plan.action} refused: {reason}"
            return
        busy = self._busy_agents(ws.path) if plan.action == "pull" else ()
        remote = self.config.remotes.get(self.target.get("project_key"), "origin")
        run, env = self.transport.run, self.env
        self.git_action = plan.action
        self.status = f"running {plan.action}…"
        self.git_job = self.submit(
            lambda: gitops.execute(plan, run, env, ws.path, busy_agents=busy, preferred_remote=remote)
        )
        self._poll_workspaces()
