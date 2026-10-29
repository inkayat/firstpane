"""The Pull Requests tab: one selected pull request, laid out as a review page.

Rows, top to bottom: the PR selector (number and title; it opens the project-wide PR list)
with the GitHub and PR-actions controls, the state badge with head -> base and the changed
files and +/- lines, check progress and open comments, the Files / Comments / Checks
sub-tabs and the selected sub-tab. Every fact comes from `gh` for the selected PR; a fact gh
did not return is shown as unknown, never guessed.

Writes reuse the existing confirms (GitHub open, PR comment, PR note, the inline FirstMate
message). Resolve conversation is the one GitHub write added here: it names the thread it
resolves, re-reads that thread before the confirm and again at `y`, and reports resolved only
when GitHub's answer says so.
"""

import json
import re
import time
from datetime import datetime

from firstpane.cells import fit, spread, width
from firstpane.sanitize import sanitize
from firstpane.style import ACCENT, ADD_BG, DEL_BG, FIELD_BG, GREEN, INK, MUTED, PINK, YELLOW, paint, rgb_bg, rgb_fg
from firstpane.syntax import ROLE_SGR, tokens
from firstpane.workspace import parse_diff

DETAIL_TIMEOUT = 15
THREADS_TIMEOUT = 20
RESOLVE_TIMEOUT = 30
PR_DETAIL_CADENCE = 120
SECTIONS = ("files", "comments", "checks")
SECTION_LABELS = {"files": "Files", "comments": "Comments", "checks": "Checks"}
VIEW_FIELDS = (
    "number,title,state,isDraft,author,headRefName,baseRefName,headRefOid,additions,deletions,"
    "changedFiles,files,reviewDecision,commits,reviews,comments,statusCheckRollup,updatedAt"
)
_RUN_URL = re.compile(r"/actions/runs/(\d+)(?:/|$)")
THREAD_LIMIT = 100
COMMENT_LIMIT = 50
THREADS_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){"
    "pullRequest(number:$number){headRefOid reviewThreads(first:%d){totalCount pageInfo{hasNextPage} "
    "nodes{id isResolved isOutdated path line startLine originalLine originalStartLine diffSide "
    "viewerCanResolve resolvedBy{login} comments(first:%d){totalCount nodes{id author{login} body "
    "createdAt diffHunk pullRequestReview{state}}}}}}}}" % (THREAD_LIMIT, COMMENT_LIMIT)
)
RESOLVE_MUTATION = "mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId}){thread{id isResolved}}}"
THREAD_ID = re.compile(r"^[A-Za-z0-9_=-]{1,200}$")
ACCENT_SGR = rgb_fg(ACCENT)
INK_SGR = rgb_fg(INK)
MUTED_SGR = rgb_fg(MUTED)
GREEN_SGR = rgb_fg(GREEN)
PINK_SGR = rgb_fg(PINK)
YELLOW_SGR = rgb_fg(YELLOW)
ADD_BG = rgb_bg(ADD_BG)
DEL_BG = rgb_bg(DEL_BG)
CHIP = ACCENT_SGR + ";" + rgb_bg(FIELD_BG)
BODY_ROWS = 6
EXCERPT_LIMIT = 8
CHANGE = {"ADDED": "A", "DELETED": "D", "MODIFIED": "M", "RENAMED": "R", "COPIED": "C", "CHANGED": "M"}
REVIEW_STATES = {
    "CHANGES_REQUESTED": ("Changes requested", YELLOW_SGR),
    "APPROVED": ("Approved", GREEN_SGR),
    "COMMENTED": ("Commented", MUTED_SGR),
    "DISMISSED": ("Dismissed", MUTED_SGR),
    "PENDING": ("Pending review", MUTED_SGR),
}
CHECK_ICONS = {"passing": ("\u2713", GREEN_SGR), "failing": ("\u2717", PINK_SGR), "pending": ("\u25cb", YELLOW_SGR),
               "skipped": ("-", MUTED_SGR), "none": ("-", MUTED_SGR), "unknown": ("?", PINK_SGR)}
MENU = (
    ("open", "Open on GitHub", "o"),
    ("detail", "Description", ""),
    ("review-diff", "Diff", "d"),
    ("commits", "Review a commit", "C"),
    ("comment", "Comment on GitHub", "c"),
    ("note", "Note to FirstMate", "m"),
    ("refresh", "Refresh", "r"),
)
BUBBLE = "\U0001f4ac"
DASH = "\u2013"


# ---- facts from gh -------------------------------------------------------------------
def check_state(item):
    """One check run or status context: passing, failing, pending, skipped or unknown."""
    if not isinstance(item, dict):
        return "unknown"
    conclusion = item.get("conclusion")
    status = item.get("status")
    if "state" in item and not conclusion and not status:
        return {
            "SUCCESS": "passing",
            "FAILURE": "failing",
            "ERROR": "failing",
            "PENDING": "pending",
            "EXPECTED": "pending",
        }.get(item["state"], "unknown")
    if conclusion in (None, "") and status != "COMPLETED":
        return "pending"
    if conclusion == "SUCCESS":
        return "passing"
    if conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
        return "failing"
    if conclusion in ("SKIPPED", "NEUTRAL"):
        return "skipped"
    return "unknown"


def checks_label(rollup):
    if not isinstance(rollup, list):
        return "unknown"
    if not rollup:
        return "none"
    states = [check_state(item) for item in rollup]
    for word in ("unknown", "failing", "pending", "passing"):
        if word in states:
            return word
    return "none"


def review_label(value):
    return {
        "APPROVED": "approved",
        "CHANGES_REQUESTED": "changes",
        "REVIEW_REQUIRED": "required",
    }.get(value, "unknown")


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _line(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _login(value):
    return sanitize(value.get("login") or "") if isinstance(value, dict) else ""


def log_argv(details_url):
    """`gh run view <id> --log-failed` for an Actions run URL, else None."""
    match = _RUN_URL.search(details_url or "")
    if not match:
        return None
    return ["gh", "run", "view", match.group(1), "--log-failed"]


def parse_named(items, text_key, state=False):
    """Commit, review, or issue-comment rows. None when gh did not return the field."""
    if not isinstance(items, list):
        return None
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        oid = item.get("oid")
        row = {"subject": sanitize(item.get(text_key) or "")}
        author = _login(item.get("author"))
        if author:
            row["author"] = author
        if isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{7,64}", oid):
            row["oid"] = oid
        if state:
            row["state"] = item.get("state") if isinstance(item.get("state"), str) else None
        rows.append(row)
    return rows


def parse_checks(rollup):
    """[{state, name, workflow}] in gh's order, or None when gh did not report checks."""
    if not isinstance(rollup, list):
        return None
    checks = []
    for item in rollup:
        item = item if isinstance(item, dict) else {}
        name = sanitize(item.get("name") or item.get("context") or "unnamed check")
        url = item.get("detailsUrl") if isinstance(item.get("detailsUrl"), str) else None
        checks.append({"state": check_state(item), "name": name, "workflow": sanitize(item.get("workflowName") or ""), "details_url": url})
    return checks


def parse_view(payload):
    """The selected PR's facts from `gh pr view --json`; a field gh did not return is None."""
    if not isinstance(payload, dict):
        return None
    files = None
    if isinstance(payload.get("files"), list):
        files = []
        for item in payload["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            files.append(
                {
                    "path": sanitize(item["path"]),
                    "additions": _count(item.get("additions")),
                    "deletions": _count(item.get("deletions")),
                    "change": CHANGE.get(item.get("changeType"), "?"),
                }
            )
    oid = payload.get("headRefOid")
    return {
        "title": sanitize(payload.get("title") or ""),
        "state": payload.get("state") if payload.get("state") in ("OPEN", "CLOSED", "MERGED") else None,
        "draft": payload.get("isDraft") if isinstance(payload.get("isDraft"), bool) else None,
        "author": _login(payload.get("author")),
        "head": sanitize(payload.get("headRefName") or "") or None,
        "base": sanitize(payload.get("baseRefName") or "") or None,
        "head_oid": oid if isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{7,64}", oid) else None,
        "additions": _count(payload.get("additions")),
        "deletions": _count(payload.get("deletions")),
        "changed": _count(payload.get("changedFiles")),
        "files": files,
        "checks": parse_checks(payload.get("statusCheckRollup")),
        "review_decision": review_label(payload["reviewDecision"]) if "reviewDecision" in payload else None,
        "commits": parse_named(payload.get("commits"), "messageHeadline") if "commits" in payload else None,
        "reviews": parse_named(payload.get("reviews"), "body", state=True) if "reviews" in payload else None,
        "issue_comments": parse_named(payload.get("comments"), "body") if "comments" in payload else None,
    }


def parse_threads(payload):
    """{threads, total, truncated, head_oid} from the review-thread query, or None."""
    try:
        pull = payload["data"]["repository"]["pullRequest"]
        connection = pull["reviewThreads"]
        nodes = connection["nodes"]
    except (KeyError, TypeError):
        return None
    if not isinstance(nodes, list):
        return None
    threads = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not THREAD_ID.match(node["id"]):
            continue
        listing = node.get("comments") if isinstance(node.get("comments"), dict) else {}
        comments = []
        for item in listing.get("nodes") or []:
            if not isinstance(item, dict):
                continue
            review = item.get("pullRequestReview")
            comments.append(
                {
                    "id": sanitize(item.get("id") or ""),
                    "author": _login(item.get("author")),
                    "created": sanitize(item.get("createdAt") or ""),
                    "body": sanitize(item.get("body") or "").replace("\r\n", "\n"),
                    "hunk": sanitize(item.get("diffHunk") or ""),
                    "review": review.get("state") if isinstance(review, dict) else None,
                }
            )
        side = node.get("diffSide")
        threads.append(
            {
                "id": node["id"],
                "resolved": node.get("isResolved") is True,
                "outdated": node.get("isOutdated") is True,
                "path": sanitize(node.get("path") or ""),
                "line": _line(node.get("line")),
                "start": _line(node.get("startLine")),
                "original_line": _line(node.get("originalLine")),
                "original_start": _line(node.get("originalStartLine")),
                "side": side if side in ("LEFT", "RIGHT") else None,
                "can_resolve": node.get("viewerCanResolve") is True,
                "resolved_by": _login(node.get("resolvedBy")),
                "comments": comments,
                "comments_total": _count(listing.get("totalCount")) or len(comments),
            }
        )
    info = connection.get("pageInfo") if isinstance(connection.get("pageInfo"), dict) else {}
    oid = pull.get("headRefOid")
    return {
        "threads": threads,
        "total": _count(connection.get("totalCount")) or len(threads),
        "truncated": info.get("hasNextPage") is True,
        "head_oid": oid if isinstance(oid, str) else None,
    }


def thread_range(thread):
    """`41-42`, `41`, or "" for a conversation on the whole file; outdated threads keep their
    original lines."""
    end = thread["line"] if thread["line"] is not None else thread["original_line"]
    start = thread["start"] if thread["line"] is not None else thread["original_start"]
    if end is None:
        return ""
    if start is None or start >= end:
        return str(end)
    return f"{start}-{end}"


def thread_location(thread):
    where = thread["path"] or "(no file)"
    span = thread_range(thread)
    text = f"{where}:{span}" if span else where
    if thread["side"] == "LEFT" and span:
        text += " (old side)"
    return text


def thread_excerpt(thread):
    """(rows, note): the diff lines the conversation is about, from its first comment's hunk,
    each (kind, number, text); note says why there are none or what was cut."""
    first = thread["comments"][0] if thread["comments"] else None
    lines = [line for line in parse_diff(first["hunk"]) if line.kind in "+- "] if first and first["hunk"] else []
    if not lines:
        return [], "code context unavailable"
    span = thread_range(thread)
    if not span:
        return [], "conversation on the whole file"
    start, _, end = span.partition("-")
    start, end = int(start), int(end or start)
    side = "old" if thread["side"] == "LEFT" else "new"
    chosen = [line for line in lines if start <= (getattr(line, side) or 0) <= end]
    if not chosen:
        return [], "code context unavailable: the commented lines are not in GitHub's excerpt"
    note = ""
    if len(chosen) > EXCERPT_LIMIT:
        note = f"first {len(chosen) - EXCERPT_LIMIT} lines not shown"
        chosen = chosen[-EXCERPT_LIMIT:]
    rows = [(line.kind, line.old if side == "old" or line.new is None else line.new, line.text) for line in chosen]
    return rows, note


def parse_time(text):
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def age_label(created, now):
    """`12 min ago`; the date when it is older than a month or ahead of the clock; "" unknown."""
    at = parse_time(created)
    if at is None:
        return ""
    delta = now - at
    if delta < -60 or delta >= 30 * 86400:
        return created[:10]
    return elapsed_label(delta)


def elapsed_label(delta):
    """`just now`, `12 min ago`, `3 h ago`, `2 d ago`."""
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return f"{int(delta // 86400)} d ago"


def pr_url(pr):
    return f"https://{pr['host']}/{pr['owner']}/{pr['repo']}/pull/{pr['number']}"


def thread_signature(thread):
    """What the confirm showed: the conversation is the same only if all of this still is."""
    return (
        thread["id"],
        tuple(comment["id"] for comment in thread["comments"]),
        thread["comments_total"],
        thread["path"],
        thread_range(thread),
        thread["side"],
    )


def _plural(count, noun, many=None):
    return f"{count} {noun if count == 1 else (many or noun + 's')}"


def word_wrap(text, cols):
    """Lines of at most `cols` cells, broken at spaces; a longer word is cut across lines."""
    out = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = word if not line else line + " " + word
            if width(candidate) <= cols:
                line = candidate
                continue
            if line:
                out.append(line)
            while width(word) > cols:
                piece = fit(word, cols)
                out.append(piece)
                word = word[len(piece):]
            line = word
        out.append(line)
    return out


def _cut(text, cols):
    """`text` cut to `cols` cells, ending in an ellipsis when something was cut."""
    cols = max(1, cols)
    return text if width(text) <= cols else fit(text, cols - 1) + "\u2026"


def _flow(parts, cols, lead=" ", gap="   "):
    """Painted `parts` joined on as few lines as fit `cols`."""
    lines, current = [], lead
    for part in parts:
        if current.strip() and width(current) + width(gap) + width(part) > cols:
            lines.append(current)
            current = lead
        current += (gap if current.strip() else "") + part
    if current.strip():
        lines.append(current)
    return lines


def file_section(text, path):
    """The part of a `gh pr diff` for `path` (matched on its `+++`/`---` lines), or None."""
    sections, current = [], []
    for line in text.split("\n"):
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)
    for lines in sections:
        names = {line[6:] for line in lines if line.startswith(("+++ b/", "--- a/"))}
        names |= {line.split(" ", 2)[2] for line in lines if line.startswith(("rename to ", "rename from "))}
        if path in names:
            return "\n".join(lines) + "\n"
    return None


def _spans(lead, labels):
    """(text, [(start, end, name)]) for labels laid out after `lead`, three spaces apart."""
    spans, column, out = [], width(lead), lead
    for name, shown, label in labels:
        if out != lead:
            out += "   "
            column += 3
        spans.append((column, column + width(label), name))
        out += shown
        column += width(label)
    return out, spans


class PullRequestsMixin:
    def _init_prs(self):
        self.pr_choice = None
        self.pr_picker = False
        self.pr_menu = None
        self.pr_menu_kind = "actions"
        self.pr_commit_oid = None
        self.pr_diff_open = False
        self.pr_section = "comments"
        self.pr_resolved_open = False
        self.pr_thread_id = None
        self.pr_file_index = 0
        self.pr_check_index = 0
        self.pr_expanded = set()
        self.pr_details = {}
        self._pr_jobs = {}
        self._pr_top = 0
        self._pr_follow = True
        self._pr_avail = 1
        self._pr_menu_at = 3

    # ---- which PR --------------------------------------------------------------------
    def _all_prs(self):
        return [row for project in self._pr_projects() if self._pr_state(project) == "prs" for row in project["pr_rows"]]

    def _target_branch(self):
        info = self._ws_info(self.target) if self.target else None
        status = info.get("status") if info else None
        if status is None or status.detached or not status.head:
            return None
        return sanitize(status.head)

    def _current_pr(self):
        """The chosen PR while it is listed; else the selected agent's branch PR; else, once
        every project answered, the first PR of the agent's project or of the inventory."""
        prs = self._all_prs()
        choice = tuple(self.pr_choice) if self.pr_choice else None
        for row in prs:
            if tuple(row["key"]) == choice:
                return row
        loading = any(self._pr_state(project) == "loading" for project in self._pr_projects())
        if choice and loading:
            return None
        project_key = (self.target or {}).get("project_key")
        branch = self._target_branch()
        if branch and project_key:
            for row in prs:
                if row["project_key"] == project_key and row.get("head") == branch:
                    return row
        if loading or not prs:
            return None
        return next((row for row in prs if row["project_key"] == project_key), prs[0])

    def _choose_pr(self, row):
        if self.pr_choice is None or tuple(self.pr_choice) != tuple(row["key"]):
            self._close_inline()
            self.pr_thread_id = None
            self.pr_file_index = self.pr_check_index = 0
            self.pr_expanded = set()
            self.pr_commit_oid = None
            self.pr_diff_open = False
            self._parked_diff["pr"] = None
            if self.review_source == "pr":
                self.diff_view = None
            self._pr_top = 0
        self.pr_choice = tuple(row["key"])
        self.pr_picker = False
        self.pr_menu = None
        self._fetch_pr_detail(row, force=False)

    def _open_picker(self):
        pr = self._current_pr()
        self._close_inline()
        self.pr_menu = None
        self.pr_picker = True
        if pr:
            self.pr_selection_key = tuple(pr["key"])

    # ---- reads -----------------------------------------------------------------------
    def _pr_detail(self, pr):
        return self.pr_details.get(tuple(pr["key"])) if pr else None

    def _fetch_pr_detail(self, pr, force=False):
        """Read the PR's facts and its review threads in two background jobs, so the facts
        are drawn while the threads are still loading. Earlier results stay until new land."""
        key = tuple(pr["key"])
        if (key, "view") in self._pr_jobs or (key, "threads") in self._pr_jobs:
            return
        entry = self.pr_details.get(key)
        now = self.clock()
        if not force and entry is not None and now - entry["fetched_at"] < PR_DETAIL_CADENCE:
            return
        if entry is None:
            entry = self.pr_details[key] = {
                "view": None, "view_error": "", "view_at": None,
                "threads": None, "threads_error": "", "threads_at": None,
            }
        entry["fetched_at"] = now
        snapshot = dict(pr)
        self._pr_jobs[(key, "view")] = self.submit(lambda: self._read_view(snapshot))
        self._pr_jobs[(key, "threads")] = self.submit(lambda: self._read_threads(snapshot))
        self._poll_pr_details()

    def _poll_pr_details(self):
        for job, future in list(self._pr_jobs.items()):
            if not future.done():
                continue
            del self._pr_jobs[job]
            key, part = job
            try:
                value, error = future.result()
            except Exception:
                value, error = None, "PR unavailable: gh failed" if part == "view" else "comments unavailable: gh failed"
            entry = self.pr_details.get(key)
            if entry is not None:
                if value is not None:
                    entry[part], entry[part + "_at"] = value, self.wall()
                entry[part + "_error"] = error

    def _read_view(self, pr):
        result = self.transport.run(
            ["gh", "pr", "view", str(pr["number"]), "-R", pr["slug"], "--json", VIEW_FIELDS],
            env=self._gh_env(),
            timeout=DETAIL_TIMEOUT,
            cwd=str(self.state_dir),
        )
        if result.timed_out or result.code != 0:
            return None, "PR unavailable: gh failed"
        try:
            view = parse_view(json.loads(result.stdout or "{}"))
        except json.JSONDecodeError:
            view = None
        return (view, "") if view else (None, "PR unavailable: gh returned unreadable output")

    def _read_threads(self, pr):
        result = self.transport.run(
            [
                "gh", "api", "graphql", "--hostname", pr["host"],
                "-f", f"owner={pr['owner']}", "-f", f"repo={pr['repo']}", "-F", f"number={int(pr['number'])}",
                "-f", "query=" + THREADS_QUERY,
            ],
            env=self._gh_env(),
            timeout=THREADS_TIMEOUT,
            cwd=str(self.state_dir),
        )
        if result.timed_out or result.code != 0:
            return None, "comments unavailable: gh failed"
        try:
            threads = parse_threads(json.loads(result.stdout or "{}"))
        except json.JSONDecodeError:
            threads = None
        return (threads, "") if threads else (None, "comments unavailable: gh returned unreadable output")

    def _store_threads(self, pr, threads):
        entry = self.pr_details.get(tuple(pr["key"])) if pr.get("key") else None
        if entry is not None:
            entry["threads"], entry["threads_error"], entry["threads_at"] = threads, "", self.wall()

    def _threads(self, detail):
        data = (detail or {}).get("threads")
        threads = data["threads"] if data else []
        return [t for t in threads if not t["resolved"]], [t for t in threads if t["resolved"]]

    # ---- the selected item of each sub-tab -------------------------------------------
    def _comment_items(self, detail):
        open_, resolved = self._threads(detail)
        items = [thread["id"] for thread in open_]
        if resolved:
            items.append("resolved-group")
            if self.pr_resolved_open:
                items.extend(thread["id"] for thread in resolved)
        return items

    def _selected_comment(self, detail):
        items = self._comment_items(detail)
        if not items:
            return None
        return self.pr_thread_id if self.pr_thread_id in items else items[0]

    def _selected_thread(self, detail):
        chosen = self._selected_comment(detail)
        open_, resolved = self._threads(detail)
        return next((thread for thread in open_ + resolved if thread["id"] == chosen), None)

    def _move(self, step):
        detail = self._pr_detail(self._current_pr())
        view = (detail or {}).get("view") or {}
        self._pr_follow = True
        if self.pr_section == "comments":
            items = self._comment_items(detail)
            if items:
                current = self._selected_comment(detail)
                index = min(max(items.index(current) + step, 0), len(items) - 1)
                self.pr_thread_id = items[index]
        elif self.pr_section == "files":
            count = len(view.get("files") or [])
            self.pr_file_index = min(max(self.pr_file_index + step, 0), max(0, count - 1))
        else:
            count = len(view.get("checks") or [])
            self.pr_check_index = min(max(self.pr_check_index + step, 0), max(0, count - 1))

    def _set_section(self, name):
        if name != self.pr_section:
            self._close_inline()
            self.pr_section = name
            self._pr_top = 0
            self._pr_follow = True

    # ---- keys and mouse --------------------------------------------------------------
    def _pr_key(self, key):
        """Keys of the PR tab; False lets the project-wide PR list (the picker) handle it."""
        pr = self._current_pr()
        if self.pr_menu is not None and pr:
            if self._pr_menu_key(key, pr):
                return True
        if self.pr_diff_open and pr and (self.diff_view or {}).get("pr") == tuple(pr["key"]) and not self.pr_picker:
            if key == "esc":
                self.pr_diff_open = False
                return True
            if self._diff_key(key):
                return True
        if key == "ctrl-o" and not self.pr_picker:
            self._jump_back()
            return True
        if key == "esc":
            if self.pr_picker:
                self.pr_picker = False
            elif self.pr_expanded:
                self.pr_expanded = set()
            return True
        if key == "." and pr:
            self.pr_picker = False
            self._open_pr_menu()
            return True
        if key == "p":
            if self.pr_picker:
                self.pr_picker = False
            else:
                self._open_picker()
            return True
        if self.pr_picker or pr is None:
            return False
        detail = self._pr_detail(pr)
        if key in ("tab", "backtab"):
            step = -1 if key == "backtab" else 1
            self._set_section(SECTIONS[(SECTIONS.index(self.pr_section) + step) % len(SECTIONS)])
        elif key in ("j", "down", "k", "up"):
            self._move(1 if key in ("j", "down") else -1)
        elif key in ("pagedown", "pageup"):
            self._pr_follow = False
            step = max(1, self._pr_avail // 2)
            self._pr_top = max(0, self._pr_top + (step if key == "pagedown" else -step))
        elif key == "enter":
            self._pr_enter(pr, detail)
        elif key == "o":
            self._arm_browser(pr)
        elif key in ("d", "v"):
            self._open_pr_review(pr)
        elif key == "c":
            self._arm_comment(pr)
        elif key == "m":
            thread = self._selected_thread(detail) if self.pr_section == "comments" else None
            if thread:
                self._pr_add(pr, detail, thread)
            else:
                self._arm_note(pr)
        elif key == "R":
            thread = self._selected_thread(detail) if self.pr_section == "comments" else None
            if thread:
                self._arm_resolve(pr, thread)
            else:
                self.status = "select an open conversation first"
        elif key == "a":
            self._announce()
        elif key == "L" and self.pr_section == "checks":
            self._show_check_log(pr, detail)
        elif key == "C":
            self._open_pr_menu("commits")
        elif key == "U":
            self.pr_commit_oid = None
            self._open_pr_review(pr)
        return True

    def _pr_commits(self, pr):
        return [item for item in ((((self._pr_detail(pr) or {}).get("view")) or {}).get("commits") or []) if item.get("oid")]

    def _pr_menu_items(self, pr):
        """[(action, label, key)] of the open PR menu: the actions, or the PR's commits newest first."""
        if self.pr_menu_kind == "commits":
            pinned = self.pr_commit_oid
            items = [(("commit", None), ("\u2713 " if not pinned else "  ") + "Whole PR (head)", "U")]
            for item in reversed(self._pr_commits(pr)):
                mark = "\u2713 " if item["oid"] == pinned else "  "
                items.append((("commit", item["oid"]), f"{mark}{item['oid'][:7]} {item['subject']}", ""))
            return items
        return list(MENU)

    def _open_pr_menu(self, kind="actions"):
        pr = self._current_pr()
        if kind == "commits" and pr is not None and not self._pr_commits(pr):
            self.status = "no PR commit to review"
            return
        self.pr_menu_kind = kind
        self.pr_menu = 0

    def _show_check_log(self, pr, detail):
        checks = ((detail or {}).get("view") or {}).get("checks") or []
        if not checks:
            self.status = "no checks"
            return
        check = checks[min(self.pr_check_index, len(checks) - 1)]
        argv = log_argv(check.get("details_url"))
        if argv is None:
            self.status = check.get("details_url") or "no Actions log for this check"
            return
        result = self.transport.run(argv, env=self._gh_env(), timeout=DETAIL_TIMEOUT, cwd=str(self.state_dir))
        text = (result.stdout or result.stderr or "")[:200_000]
        self.detail_text = f"Log {check.get('name')}\n" + (text or "no log output")
        self.screen = "detail"

    def _pr_enter(self, pr, detail):
        if self.pr_section == "comments":
            chosen = self._selected_comment(detail)
            if chosen == "resolved-group":
                self.pr_resolved_open = not self.pr_resolved_open
            elif chosen:
                self.pr_expanded ^= {chosen}
        elif self.pr_section == "files":
            files = ((detail or {}).get("view") or {}).get("files") or []
            if files:
                self._open_pr_review(pr, path=files[min(self.pr_file_index, len(files) - 1)]["path"])

    def _pr_menu_key(self, key, pr):
        items = self._pr_menu_items(pr)
        if key in ("j", "down", "k", "up"):
            step = 1 if key in ("j", "down") else -1
            self.pr_menu = min(max(self.pr_menu + step, 0), len(items) - 1)
            return True
        if key == "enter":
            action = items[min(self.pr_menu, len(items) - 1)][0]
            if action is not None:
                self._run_menu(action, pr)
            return True
        self.pr_menu = None
        return key in ("esc", ".")

    def _run_menu(self, action, pr):
        self.pr_menu = None
        if isinstance(action, tuple):
            if action[0] == "commit":
                self.pr_commit_oid = action[1]
                self._open_pr_review(pr)
            return
        if action == "review-diff":
            self._open_pr_review(pr)
        elif action == "commits":
            self._open_pr_menu("commits")
        elif action == "open":
            self._arm_browser(pr)
        elif action == "detail":
            self._open_detail(pr)
        elif action == "comment":
            self._arm_comment(pr)
        elif action == "note":
            self._arm_note(pr)
        elif action == "refresh":
            self.refresh(manual=True)

    def _pr_mouse(self, hit, column):
        pr = self._current_pr()
        if pr is None:
            return
        kind = hit["kind"]
        name = next((span[2] for span in hit.get("spans", ()) if span[0] <= column - 1 < span[1]), None)
        if kind == "pr-head":
            if name == "selector":
                self.pr_picker = not self.pr_picker
                if self.pr_picker:
                    self._open_picker()
            elif name == "github":
                self._arm_browser(pr)
            elif name == "actions":
                if self.pr_menu is not None:
                    self.pr_menu = None
                else:
                    self._open_pr_menu()
            elif name == "commits":
                self._open_pr_menu("commits")
            return
        if kind == "pr-menu":
            self._run_menu(hit["action"], pr)
            return
        self.pr_menu = None
        detail = self._pr_detail(pr)
        if kind == "pr-sections" and name:
            self._set_section(name)
        elif kind == "pr-thread":
            if self.pr_thread_id != hit["id"]:
                self._close_inline()
            self.pr_thread_id = hit["id"]
            self._pr_follow = True
        elif kind == "pr-resolved":
            self.pr_thread_id = "resolved-group"
            self.pr_resolved_open = not self.pr_resolved_open
        elif kind == "pr-controls" and name:
            self.pr_thread_id = hit["id"]
            thread = self._selected_thread(detail)
            if thread is None:
                self.status = "click ignored: stale conversation"
            elif name == "add":
                self._pr_add(pr, detail, thread)
            elif name == "resolve":
                self._arm_resolve(pr, thread)
        elif kind == "pr-file":
            again = self.pr_file_index == hit["index"]
            self.pr_file_index = hit["index"]
            if again:
                self._pr_enter(pr, detail)
        elif kind == "pr-check":
            self.pr_check_index = hit["index"]

    # ---- Add to FirstMate ------------------------------------------------------------
    def _pr_add(self, pr, detail, thread):
        """Open the inline FirstMate message bound to this PR conversation and its code."""
        if not self.config.firstmate_home:
            self.status = "FirstMate not configured"
            return
        view = (detail or {}).get("view") or {}
        data = (detail or {}).get("threads") or {}
        rows, note = thread_excerpt(thread)
        record = {
            "kind": "pr-thread",
            "url": pr_url(pr),
            "slug": pr["slug"],
            "host": pr["host"],
            "owner": pr["owner"],
            "repo": pr["repo"],
            "number": pr["number"],
            "title": view.get("title") or pr.get("title") or "",
            "head": view.get("head_oid") or data.get("head_oid"),
            "branch": view.get("head"),
            "base": view.get("base"),
            "thread_id": thread["id"],
            "path": thread["path"],
            "range": thread_range(thread),
            "side": thread["side"],
            "outdated": thread["outdated"],
            "resolved": thread["resolved"],
            "excerpt": [list(row) for row in rows],
            "excerpt_note": note,
            "comments": [
                {name: comment[name] for name in ("id", "author", "created", "review", "body")}
                for comment in thread["comments"]
            ],
            "comments_total": thread["comments_total"],
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.pr_thread_id = thread["id"]
        self._pr_follow = True
        self._open_inline(record)

    def _pr_thread_drift(self, index, item):
        """Preview warnings for a PR conversation attachment, from a fresh read of the PR."""
        pr = {name: item.get(name) for name in ("host", "owner", "repo", "number", "slug")}
        data, error = self._read_threads(pr)
        if error:
            return [f"attachment {index}: could not re-read the conversation ({error}); the message carries it as added"]
        current = self._current_pr()
        if current and (current["slug"], current["number"]) == (pr["slug"], pr["number"]):
            self._store_threads(current, data)
        thread = next((thread for thread in data["threads"] if thread["id"] == item.get("thread_id")), None)
        if thread is None:
            return [f"attachment {index}: the conversation is no longer listed on #{pr['number']}"]
        warnings = []
        if thread["resolved"] != bool(item.get("resolved")):
            warnings.append(f"attachment {index}: the conversation is now {'resolved' if thread['resolved'] else 'unresolved'}")
        then = [comment.get("id") for comment in item.get("comments") or []]
        if [comment["id"] for comment in thread["comments"]] != then or thread["comments_total"] != item.get("comments_total"):
            warnings.append(
                f"attachment {index}: the conversation changed since it was added (now "
                f"{_plural(thread['comments_total'], 'comment')}); the message carries it as added"
            )
        if data.get("head_oid") and item.get("head") and data["head_oid"] != item["head"]:
            warnings.append(f"attachment {index}: the PR head moved from {item['head'][:7]} to {data['head_oid'][:7]}")
        return warnings

    # ---- Resolve conversation --------------------------------------------------------
    def _fresh_thread(self, pr, thread_id):
        """(thread, refusal) from a fresh read of the PR's conversations."""
        data, error = self._read_threads(pr)
        if error:
            return None, f"resolve refused: could not re-read the conversation ({error})"
        self._store_threads(pr, data)
        thread = next((thread for thread in data["threads"] if thread["id"] == thread_id), None)
        if thread is None:
            return None, "resolve refused: the conversation is no longer listed on the PR"
        if thread["resolved"]:
            return None, "resolve not needed: GitHub already shows it resolved"
        if not thread["can_resolve"]:
            return None, "resolve refused: GitHub says this account cannot resolve it"
        return thread, ""

    def _arm_resolve(self, pr, thread):
        if thread["resolved"]:
            self.status = "this conversation is already resolved"
            return
        if not thread["can_resolve"]:
            self.status = "resolve refused: GitHub says this account cannot resolve it"
            return
        fresh, refusal = self._fresh_thread(pr, thread["id"])
        if fresh is None:
            self.status = refusal
            return
        login = self.logins.get(pr["host"]) or self._fetch_login(pr["host"])
        if login:
            self.logins[pr["host"]] = login
        self.pending = {"pr": dict(pr), "thread": fresh, "login": login, "warnings": []}
        self.screen = "confirm-resolve"

    def _confirm_resolve(self, plain):
        pending = self.pending or {}
        pr, thread = pending.get("pr") or {}, pending.get("thread")
        if not thread:
            return ""
        now = self.wall()
        first = thread["comments"][0] if thread["comments"] else None
        last = thread["comments"][-1] if thread["comments"] else None
        lines = [paint(f"Resolve conversation on {pr.get('slug')}#{pr.get('number')} as @{pending.get('login') or 'unknown'}", "1", plain)]
        lines.extend(paint("! " + warning, "1;33", plain) for warning in pending.get("warnings") or [])
        location = thread_location(thread) + (" \u00b7 outdated" if thread["outdated"] else "")
        lines.append(location)
        if first:
            facts = [f"started by @{first['author'] or 'unknown'} {age_label(first['created'], now)}".rstrip()]
            facts.append(_plural(thread["comments_total"], "comment"))
            if last is not first:
                facts.append(f"latest @{last['author'] or 'unknown'} {age_label(last['created'], now)}".rstrip())
            lines.append(" \u00b7 ".join(facts))
            lines.extend("> " + line for line in first["body"].split("\n")[:3])
        lines.append(f"thread {thread['id']}")
        lines.append("GitHub marks this conversation resolved for everyone on the PR. No comment is posted.")
        lines.append("y resolves, any other key cancels")
        return "\n".join(lines)

    def _resolve_thread(self):
        pending = self.pending or {}
        pr, before = pending.get("pr"), pending.get("thread")
        if not pr or not before:
            self.screen, self.pending = "tree", None
            return
        fresh, refusal = self._fresh_thread(pr, before["id"])
        if fresh is None:
            self.screen, self.pending = "tree", None
            self.status = refusal
            return
        if thread_signature(fresh) != thread_signature(before):
            self.pending = dict(pending, thread=fresh, warnings=["the conversation changed since the preview; this is its current state"])
            return
        self.screen, self.pending = "tree", None
        result = self.transport.run(
            ["gh", "api", "graphql", "--hostname", pr["host"], "-f", f"threadId={fresh['id']}", "-f", "query=" + RESOLVE_MUTATION],
            env=self._gh_env(),
            timeout=RESOLVE_TIMEOUT,
            cwd=str(self.state_dir),
        )
        confirmed = False
        if not result.timed_out and result.code == 0:
            try:
                answer = json.loads(result.stdout or "{}")["data"]["resolveReviewThread"]["thread"]
                confirmed = answer.get("id") == fresh["id"] and answer.get("isResolved") is True
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                confirmed = False
        if confirmed:
            fresh["resolved"] = True
            self.status = "resolve outcome: resolved"
        else:
            self.status = "resolve outcome: unknown; check the conversation on GitHub before trying again"
        self._fetch_pr_detail(pr, force=True)

    # ---- layout ----------------------------------------------------------------------
    def _prs_body(self, plain, budget):
        pr = self._current_pr()
        if pr is None:
            return self._pr_list_body(plain, budget, [], [])
        self._fetch_pr_detail(pr)
        detail = self._pr_detail(pr)
        rows = [self._selector_row(pr, plain)]
        if self.pr_menu is not None:
            rows.extend(self._pr_menu_rows(plain))
        if self.pr_picker:
            return self._pr_list_body(plain, budget - len(rows), [row[0] for row in rows], [row[1] for row in rows])
        states = [self._pr_state(project) for project in self._pr_projects()]
        if "error" in states or "loading" in states:
            notice = self._pr_summary() + " \u00b7 p lists every project"
            rows.append((" " + paint(notice, "35" if "error" in states else "2", plain), {"kind": "pr-head", "spans": [(0, self.cols, "selector")]}))
        if self.pr_choice and tuple(self.pr_choice) != tuple(pr["key"]):
            gone = f"#{self.pr_choice[-1]} is no longer in the open PR list; showing #{pr['number']}"
            rows.append((" " + paint(gone, "35", plain), {"kind": "pr-head", "spans": [(0, self.cols, "selector")]}))
        view = self.diff_view
        if self.pr_diff_open and view is not None and view.get("pr") == tuple(pr["key"]):
            pill = f"Commit {self.pr_commit_oid[:7]} \u25be" if self.pr_commit_oid else "Whole PR \u25be"
            lead = " Diff  "
            start = width(lead)
            row = lead + paint(f"[ {pill} ]", CHIP, plain) + "  " + paint("Esc closes", MUTED_SGR, plain)
            rows.append((row, {"kind": "pr-head", "spans": [(start, start + width(pill) + 4, "commits")]}))
            rows.extend(self._diff_rows(plain, max(1, budget - len(rows)), self._agent_drafts("diff")))
            return [text for text, _ in rows], [hit for _, hit in rows]
        if budget >= 16:
            rows.append((paint("\u2500" * self.cols, "2", plain), None))
        state = self._state_lines(pr, detail, plain)
        summary = self._summary_lines(detail, plain) + self._stale_lines(detail, plain)
        if budget < 16:
            state = _flow([line.strip() for line in state + summary], self.cols)
            summary = []
        rows.extend((line, None) for line in state + summary)
        rows.extend(self._section_tabs(detail, plain))
        avail = max(1, budget - len(rows))
        rows.extend(self._section_window(pr, detail, plain, avail))
        return [text for text, _ in rows], [hit for _, hit in rows]

    def _pr_list_body(self, plain, budget, lines, hits):
        summary = self._pr_summary()
        if summary:
            lines.append(" " + summary)
            hits.append(None)
            budget -= 1
        return self._tree_lines(self._rows(), max(1, budget), lines, hits, plain)

    def _selector_row(self, pr, plain):
        """`[ #n · title ▾ ]` (opens the PR list), then `[ GitHub ↗ ]  [ PR actions ▾ ]`."""
        detail = self._pr_detail(pr)
        title = ((detail or {}).get("view") or {}).get("title") or pr["title"]
        project = self._project(pr["project_key"])
        number = f"#{pr['number']}"
        label = f"{number} \u00b7 {title}"
        if project and project["key"] != (self.target or {}).get("project_key"):
            label = f"{project['label']} \u00b7 " + label
        mark = "\u25b4" if self.pr_picker else "\u25be"
        wide = self.cols >= 72
        github = "[ GitHub \u2197 ]" if wide else "[ \u2197 ]"
        actions = "[ PR actions \u25be ]" if wide else "[ \u22ef ]"
        right = github + "  " + actions + " "
        room = max(4, self.cols - width(right) - 8)
        shown = label if width(label) <= room else fit(label, room - 1) + "\u2026"
        left_width = 3 + width(shown) + 4
        gap = " " * max(1, self.cols - left_width - width(right))
        start = left_width + len(gap)
        spans = [
            (1, left_width, "selector"),
            (start, start + width(github), "github"),
            (start + width(github) + 2, start + width(github) + 2 + width(actions), "actions"),
        ]
        self._pr_menu_at = max(3, min(spans[2][0], self.cols - 24))
        left = "[ " + shown + " " + mark + " ]"
        text = " " + paint(left, CHIP, plain) + gap + paint(github, CHIP, plain) + "  " + paint(actions, CHIP, plain) + " "
        return fit(text, self.cols), {"kind": "pr-head", "spans": spans}

    def _pr_menu_rows(self, plain):
        rows = []
        for index, (action, label, key) in enumerate(self._pr_menu_items(self._current_pr())):
            selected = index == self.pr_menu
            lead = paint("\u2503", ACCENT_SGR, plain) if selected else paint("\u2502", MUTED_SGR, plain)
            text = " " * self._pr_menu_at + f"{lead} " + (paint(label, "1;" + INK_SGR, plain) if selected else label)
            if key:
                text += "  " + paint(key, MUTED_SGR, plain)
            rows.append((text, {"kind": "pr-menu", "action": action}))
        return rows

    def _state_lines(self, pr, detail, plain):
        view = (detail or {}).get("view")
        if view is None:
            error = (detail or {}).get("view_error")
            return [" " + paint(error or "loading pull request\u2026", PINK_SGR if error else MUTED_SGR, plain)]
        if view["state"] == "OPEN":
            badge = ("\u25cc Draft", MUTED_SGR) if view["draft"] else ("\u25cb Open", GREEN_SGR + ";" + ADD_BG)
        else:
            badge = {"CLOSED": ("\u2717 Closed", PINK_SGR + ";" + DEL_BG), "MERGED": ("\u2713 Merged", ACCENT_SGR)}.get(
                view["state"], ("? state unknown", PINK_SGR)
            )
        parts = [paint("[ " + badge[0] + " ]", badge[1], plain)]
        parts.append(paint(f"{view['head'] or '?'} \u2192 {view['base'] or '?'}", INK_SGR, plain))
        files = view["changed"] if view["changed"] is not None else None
        counts = [paint(_plural(files, "file") if files is not None else "? files", ACCENT_SGR, plain)]
        if view["additions"] is not None:
            counts.append(paint(f"+{view['additions']}", GREEN_SGR, plain))
        if view["deletions"] is not None:
            counts.append(paint(f"-{view['deletions']}", PINK_SGR, plain))
        parts.append(counts[0] + (" \u00b7 " + " ".join(counts[1:]) if counts[1:] else ""))
        return _flow(parts, self.cols)

    def _stale_lines(self, detail, plain):
        """One dim line when a refresh failed and the page still shows the last good read."""
        detail = detail or {}
        kept = [part for part in ("view", "threads") if detail.get(part) is not None and detail.get(part + "_error")]
        if not kept:
            return []
        since = min(detail[part + "_at"] for part in kept)
        return [" " + paint(f"! refresh failed \u00b7 showing data from {elapsed_label(self.wall() - since)}", PINK_SGR, plain)]

    def _summary_lines(self, detail, plain):
        view = (detail or {}).get("view")
        parts = []
        checks = view["checks"] if view else None
        if view is None:
            pass
        else:
            if view.get("review_decision"):
                parts.append(paint(f"review {view['review_decision']}", MUTED_SGR, plain))
            if view.get("commits") is not None:
                parts.append(paint(f"{len(view['commits'])} commits", MUTED_SGR, plain))
            if view.get("issue_comments") is not None:
                parts.append(paint(f"{len(view['issue_comments'])} comments", MUTED_SGR, plain))
        if view is None:
            pass
        elif checks is None:
            parts.append(paint("? checks unknown", PINK_SGR, plain))
        elif not checks:
            parts.append(paint("- no checks", MUTED_SGR, plain))
        else:
            states = [check["state"] for check in checks]
            overall = next((word for word in ("unknown", "failing", "pending", "passing") if word in states), "skipped")
            icon, sgr = CHECK_ICONS[overall]
            text = f"{icon} Checks {states.count('passing')}/{len(states)} passing"
            extra = [f"{states.count(word)} {word}" for word in ("failing", "pending", "skipped", "unknown") if states.count(word)]
            parts.append(paint(" \u00b7 ".join([text] + extra), sgr, plain))
        threads_error = (detail or {}).get("threads_error")
        if (detail or {}).get("threads") is None and not threads_error:
            parts.append(paint("comments loading\u2026", MUTED_SGR, plain))
        elif detail.get("threads") is None:
            parts.append(paint(threads_error, PINK_SGR, plain))
        else:
            open_, _ = self._threads(detail)
            more = "+" if detail["threads"]["truncated"] else ""
            word = f"{len(open_)}{more} open " + ("comment" if len(open_) == 1 and not more else "comments")
            parts.append(paint(f"{BUBBLE} {word}" if open_ or more else f"{BUBBLE} no open comments", YELLOW_SGR if open_ else MUTED_SGR, plain))
        return _flow(parts, self.cols, gap="  \u00b7  ") if parts else []

    def _section_tabs(self, detail, plain):
        view = (detail or {}).get("view") or {}
        data = (detail or {}).get("threads")
        counts = {
            "files": view.get("changed") if view.get("changed") is not None else (len(view["files"]) if view.get("files") is not None else None),
            "comments": (f"{data['total']}+" if data["truncated"] else data["total"]) if data else None,
            "checks": None,
        }
        labels = []
        for name in SECTIONS:
            label = SECTION_LABELS[name] + (f" {counts[name]}" if counts[name] is not None else "")
            shown = paint(label, "1;" + ACCENT_SGR, plain) if name == self.pr_section else paint(label, MUTED_SGR, plain)
            labels.append((name, shown, label))
        text, spans = _spans("  ", labels)
        active = next((span for span in spans if span[2] == self.pr_section), None)
        low, high = (active[0] - 1, active[1] + 1) if active else (0, 0)
        rule = (
            paint("\u2500" * low, MUTED_SGR, plain)
            + paint("\u2501" * (high - low), ACCENT_SGR, plain)
            + paint("\u2500" * max(0, self.cols - high), MUTED_SGR, plain)
        )
        hit = {"kind": "pr-sections", "spans": [(start - 1, end + 1, name) for start, end, name in spans]}
        return [(text, hit), (fit(rule, self.cols), hit)]

    # ---- the selected sub-tab --------------------------------------------------------
    def _section_window(self, pr, detail, plain, avail):
        if self.pr_section == "comments":
            rows, pinned, need = self._comment_rows(pr, detail, plain)
        elif self.pr_section == "files":
            rows, pinned, need = self._file_rows_pr(detail, plain)
        else:
            rows, pinned, need = self._check_rows(detail, plain)
        head, rest = rows[:pinned], rows[pinned:]
        avail = max(1, avail - len(head))
        self._pr_avail = avail
        need = [index - pinned for index in need if index >= pinned]
        top = self._pr_top
        if need and self._pr_follow:
            low, high = min(need), max(need)
            if high - low + 1 > avail:
                top = low
            elif low < top:
                top = low
            elif high >= top + avail:
                top = high - avail + 1
        top = max(0, min(top, max(0, len(rest) - avail)))
        self._pr_top = top
        return head + rest[top : top + avail]

    def _comment_rows(self, pr, detail, plain):
        """(rows, pinned heading count, indexes that must stay on screen)."""
        if detail is None or detail.get("threads") is None:
            error = (detail or {}).get("threads_error")
            return [(" " + paint(error or "loading comments\u2026", "35" if error else "2", plain), None)], 1, []
        open_, resolved = self._threads(detail)
        chosen = self._selected_comment(detail)
        position = next((index for index, thread in enumerate(open_, 1) if thread["id"] == chosen), None)
        index_text = f"{position or DASH} / {len(open_)}" if open_ else ""
        heading = paint("Open comments", "1;" + ACCENT_SGR, plain)
        rows = [(spread(" " + heading, paint(index_text + " ", "2", plain), self.cols), None)]
        need = []
        drafts = {}
        for draft in self._agent_drafts("pr-thread"):
            thread_id = (draft.get("anchor") or {}).get("thread_id")
            drafts.setdefault(thread_id, []).append(draft)
        if not open_:
            rows.append((" " + paint("no open comments", "2", plain), None))
        for thread in open_:
            self._thread_rows(pr, thread, thread["id"] == chosen, drafts.get(thread["id"], []), rows, need, plain)
        if resolved:
            selected = chosen == "resolved-group"
            mark = "\u25be" if self.pr_resolved_open else "\u25b8"
            lead = paint("\u2503", ACCENT_SGR, plain) if selected else " "
            text = f" {lead} {mark} " + paint(f"Resolved comments ({len(resolved)})", "1" if selected else "2", plain)
            if selected:
                need.append(len(rows))
            rows.append((text, {"kind": "pr-resolved"}))
            if self.pr_resolved_open:
                for thread in resolved:
                    self._thread_rows(pr, thread, thread["id"] == chosen, drafts.get(thread["id"], []), rows, need, plain)
        if detail["threads"]["truncated"]:
            rows.append((" " + paint(f"only the first {THREAD_LIMIT} comment threads are shown; o opens the PR on GitHub", "2", plain), None))
        return rows, 1, need

    def _thread_rows(self, pr, thread, selected, drafts, rows, need, plain):
        now = self.wall()
        first = thread["comments"][0] if thread["comments"] else None
        author = (first["author"] if first else "") or "unknown author"
        age = age_label(first["created"], now) if first else ""
        hit = {"kind": "pr-thread", "id": thread["id"]}
        if not selected:
            lead = " " + paint("\u2502", "2", plain) + " "
            summary = (first["body"].strip().split("\n")[0] if first else "") or ""
            marks = paint(f"\u270e{len(drafts)}", "36", plain) + "  " if drafts else ""
            right = marks + paint(age, "2", plain) + " "
            left = lead + paint(author, "1", plain) + "  " + paint(thread_location(thread), ACCENT_SGR, plain) + "  " + summary
            left = _cut(left, self.cols - width(right) - 2)
            rows.append((left + " " * max(1, self.cols - width(left) - width(right)) + right, hit))
            return
        start = len(rows)
        bar = paint("\u2503", ACCENT_SGR, plain)
        lead = f" {bar} "
        inner = max(8, self.cols - 4)
        tag = paint("[selected]", CHIP, plain)
        right = (paint(age, "2", plain) + "  " if age else "") + tag + " "
        rows.append((spread(lead + paint(author, "1;" + ACCENT_SGR, plain), right, self.cols), hit))
        location = paint(thread_location(thread), ACCENT_SGR, plain)
        if thread["outdated"]:
            location += "  " + paint("outdated", "35", plain)
        if thread["resolved"]:
            location += "  " + paint("resolved" + (f" by @{thread['resolved_by']}" if thread["resolved_by"] else ""), "32", plain)
        rows.append((lead + location, hit))
        excerpt, note = thread_excerpt(thread)
        if excerpt:
            digits = max(len(str(number or "")) for _, number, _ in excerpt)
            for kind, number, text in excerpt:
                rows.append((lead + self._code_row(kind, number, text, digits, inner, thread["path"], plain), hit))
        if note:
            rows.append((lead + paint(note, "2", plain), hit))
        expanded = thread["id"] in self.pr_expanded
        for position, comment in enumerate(thread["comments"]):
            if position and not expanded:
                break
            if position:
                rows.append((lead + paint(f"{comment['author'] or 'unknown author'}  {age_label(comment['created'], now)}".rstrip(), "1", plain), hit))
            body = word_wrap(comment["body"].strip("\n").expandtabs(4), inner)
            limit = len(body) if expanded else BODY_ROWS
            rows.extend((lead + line, hit) for line in body[:limit])
            if len(body) > limit:
                rows.append((lead + paint(f"\u2026 {_plural(len(body) - limit, 'more line')} \u00b7 Enter shows all", "2", plain), hit))
            if position == 0 and comment["review"] in REVIEW_STATES:
                word, sgr = REVIEW_STATES[comment["review"]]
                rows.append((lead + paint(word, sgr, plain), hit))
        replies = len(thread["comments"]) - 1
        if replies > 0 and not expanded:
            last = thread["comments"][-1]
            text = f"{_plural(replies, 'reply', 'replies')} \u00b7 latest {last['author'] or 'unknown author'} {age_label(last['created'], now)}".rstrip()
            rows.append((_cut(lead + paint(text + " \u00b7 Enter shows all", "2", plain), self.cols - 1), hit))
        hidden = thread["comments_total"] - len(thread["comments"])
        if hidden > 0 and (expanded or replies <= 0):
            rows.append((lead + paint(f"{_plural(hidden, 'more comment')} on GitHub", "2", plain), hit))
        rows.extend(self._controls_rows(thread, lead, plain))
        self._draft_rows(thread, drafts, rows, need, plain, start)

    def _draft_rows(self, thread, drafts, rows, need, plain, start):
        active = self.draft if self.draft and (self.draft.get("anchor") or {}).get("thread_id") == thread["id"] else None
        before = len(rows)
        for draft in drafts:
            if draft is not active and not self._footer_holds(draft):
                rows.extend(self._inline_rows(draft, plain, 3))
        if active and not self._footer_holds(active):
            mark = len(rows)
            rows.extend(self._inline_rows(active, plain, 3))
            if self.focus == "composer":
                need.extend(range(mark, len(rows)))
                return
        need.extend(range(start, max(before, len(rows))))

    def _controls_rows(self, thread, lead, plain):
        """`[ Add to FirstMate ]   Resolve conversation`, on two rows when one is too narrow."""
        add = "[ Add to FirstMate ]"
        labels = [("add", paint(add, ACCENT_SGR, plain), add)]
        if not thread["resolved"]:
            if thread["can_resolve"]:
                labels.append(("resolve", "Resolve conversation", "Resolve conversation"))
            else:
                word = "Resolve conversation (not permitted)"
                labels.append(("resolve", paint(word, "2", plain), word))
        text, spans = _spans(lead, labels)
        if spans[-1][1] < self.cols:
            return [(text, {"kind": "pr-controls", "id": thread["id"], "spans": spans})]
        rows = []
        for label in labels:
            text, spans = _spans(lead, [label])
            rows.append((text, {"kind": "pr-controls", "id": thread["id"], "spans": spans}))
        return rows

    def _code_row(self, kind, number, text, digits, inner, path, plain):
        bg = {"+": ADD_BG, "-": DEL_BG}.get(kind, "")
        gutter = f" {str(number or ''):>{digits}} "
        marker = f" {kind if kind in '+-' else ' '}  "
        code = text.expandtabs(4)
        room = max(0, inner - width(gutter) - width(marker))
        parts, used = [], 0
        segments = tokens(code, path)
        cut = width(code) > room
        room -= 1 if cut else 0
        for segment, role in segments:
            segment = fit(segment, room - used)
            if not segment:
                break
            used += width(segment)
            parts.append(paint(segment, ";".join(value for value in (ROLE_SGR.get(role, ""), bg) if value), plain))
        if cut:
            parts.append(paint("\u2026", ";".join(value for value in (MUTED_SGR, bg) if value), plain))
            room += 1
            used += 1
        pad = " " * max(0, room - used)
        sign = {"+": GREEN_SGR, "-": PINK_SGR}.get(kind, MUTED_SGR)
        return (
            paint(gutter, ";".join(value for value in (MUTED_SGR, bg) if value), plain)
            + paint(marker, ";".join(value for value in (sign, bg) if value), plain)
            + "".join(parts)
            + (paint(pad, bg, plain) if bg else "")
        )

    def _file_rows_pr(self, detail, plain):
        view = (detail or {}).get("view")
        if view is None:
            error = (detail or {}).get("view_error")
            return [(" " + paint(error or "loading files\u2026", "35" if error else "2", plain), None)], 1, []
        files = view["files"]
        if files is None:
            return [(" " + paint("files unknown: gh did not list them", "35", plain), None)], 1, []
        if not files:
            return [(" " + paint("no changed files", "2", plain), None)], 1, []
        open_, _ = self._threads(detail)
        per_path = {}
        for thread in open_:
            per_path[thread["path"]] = per_path.get(thread["path"], 0) + 1
        self.pr_file_index = min(self.pr_file_index, len(files) - 1)
        rows = []
        for index, item in enumerate(files):
            selected = index == self.pr_file_index
            lead = paint("\u2503", ACCENT_SGR, plain) if selected else " "
            counts = []
            if item["additions"] is not None:
                counts.append(paint(f"+{item['additions']}", "32", plain))
            if item["deletions"] is not None:
                counts.append(paint(f"-{item['deletions']}", "31", plain))
            right = " ".join(counts) + " "
            if per_path.get(item["path"]):
                right = paint(f"{BUBBLE}{per_path[item['path']]}", "33", plain) + "  " + right
            left = f" {lead} {item['change']}  " + (paint(item["path"], "1", plain) if selected else item["path"])
            left = _cut(left, self.cols - width(right) - 2)
            rows.append((left + " " * max(1, self.cols - width(left) - width(right)) + right, {"kind": "pr-file", "index": index}))
        if view["changed"] is not None and view["changed"] > len(files):
            rows.append((" " + paint(f"{view['changed'] - len(files)} more files not listed by gh; d shows the full diff", "2", plain), None))
        rows.append((" " + paint("Enter or a second click shows the file's diff", "2", plain), None))
        return rows, 0, [self.pr_file_index]

    def _check_rows(self, detail, plain):
        view = (detail or {}).get("view")
        if view is None:
            error = (detail or {}).get("view_error")
            return [(" " + paint(error or "loading checks\u2026", "35" if error else "2", plain), None)], 1, []
        checks = view["checks"]
        if checks is None:
            return [(" " + paint("checks unknown: gh did not report them", "35", plain), None)], 1, []
        if not checks:
            return [(" " + paint("no checks reported", "2", plain), None)], 1, []
        self.pr_check_index = min(self.pr_check_index, len(checks) - 1)
        rows = []
        for index, check in enumerate(checks):
            selected = index == self.pr_check_index
            lead = paint("\u2503", ACCENT_SGR, plain) if selected else " "
            icon, sgr = CHECK_ICONS[check["state"]]
            text = f" {lead} " + paint(f"{icon} {check['state']:<8}", sgr, plain) + " " + (paint(check["name"], "1", plain) if selected else check["name"])
            if check["workflow"]:
                text += "  " + paint(check["workflow"], "2", plain)
            rows.append((text, {"kind": "pr-check", "index": index}))
        return rows, 0, [self.pr_check_index]
