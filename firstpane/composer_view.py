"""Messages to FirstMate: the Agents tab's message field about the selected agent, and inline
messages bound to the diff lines, page selection or PR conversation they are about.

The Agents field's draft is keyed by the agent alone. An inline message is opened on a diff
range (Changes tab), a captured browser selection (Browser tab) or a pull request conversation
(Pull Requests tab) and its editor is drawn right under it. Its draft is keyed by the selected
agent and the selection itself, so it never moves to another agent, file, range or
conversation; sending is one exact preview, then the message is put, unsent, into the FirstMate
primary's own message box (firstpane.handoff) for the captain to send there.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from firstpane.cells import fit, tail, width
from firstpane.handoff import Handoff
from firstpane.message import build_message
from firstpane.review import patch_line_paths
from firstpane.sanitize import sanitize
from firstpane.style import ACCENT, FIELD_BG, INK, MUTED, PINK, SEND_BG, YELLOW, paint, rgb_bg, rgb_fg
from firstpane.workspace import FileEntry, parse_diff, read_commit_diff, read_diff

ATTEMPTED = ("unknown", "saved-unannounced")
EDITOR_ROWS = 3
EXCERPT_ROWS = 3
BAR = "\u2503"


def note_outcome(result):
    """fm-inbox.sh note exit mapping: (outcome, note id)."""
    payload = {}
    if result.stdout:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    note_id = payload.get("id") or None
    if result.timed_out:
        return "unknown", note_id
    if result.code == 1:
        return "not-saved", note_id
    if result.code == 3:
        return "saved-unannounced", note_id
    if result.code == 0:
        return ("already-sent" if payload.get("outcome") == "replay" else "queued"), note_id
    return "unknown", note_id


def anchor_key(record):
    """Identity of a selection: the same lines (or page selection) reopen the same draft."""
    if record.get("kind") == "diff":
        basis = ["diff", record.get("workspace"), record.get("group"), record.get("path"), record.get("rows")]
        if record.get("group") in ("commit", "pr"):
            basis.append(record.get("revision"))
        if record.get("group") == "pr":
            basis.append(record.get("pr_url"))
    elif record.get("kind") == "pr-thread":
        basis = ["pr-thread", record.get("slug"), record.get("number"), record.get("thread_id")]
    else:
        basis = ["browser", record.get("browser_key"), record.get("tab_id"), record.get("url"), record.get("selection")]
        if record.get("element"):
            basis.append(record["element"])
    return basis[0] + ":" + hashlib.sha256(json.dumps(basis, ensure_ascii=False).encode()).hexdigest()[:20]


def range_label(record):
    """`new 2`, `old 2-3 new 2-3`: the lines a diff selection covers on each side."""
    parts = []
    for side in ("old", "new"):
        start, end = record.get(side + "_start"), record.get(side + "_end")
        if start is not None:
            parts.append(f"{side} {start}" + (f"-{end}" if end != start else ""))
    if not parts and record.get("start") is not None:
        parts.append(f"{record.get('side')} {record.get('start')}-{record.get('end')}")
    return " ".join(parts)


def _row(line):
    return (line.kind, line.old, line.new, line.text)


def locate(anchor, lines):
    """(first, last) index in `lines` of the anchor's selected rows, unchanged and in order,
    or None when any of them changed. Hunk headers between selected rows are skipped."""
    rows = [tuple(row) for row in anchor.get("rows") or []]
    if not rows:
        return None
    eligible = [index for index, line in enumerate(lines) if line.kind in "+- "]
    for position, index in enumerate(eligible):
        if _row(lines[index]) != rows[0]:
            continue
        span = eligible[position : position + len(rows)]
        if len(span) == len(rows) and all(_row(lines[i]) == row for i, row in zip(span, rows)):
            return span[0], span[-1]
    return None


class ComposerMixin:
    # ---- identity --------------------------------------------------------------------
    def _agent_target(self, row=None):
        target = row or self.target
        if not target:
            return "general"
        if target.get("task_id"):
            return f"fm:{target['task_id']}:{target.get('terminal_id')}"
        return "agent:" + "|".join(str(part) for part in target["key"][1:])

    def _binding(self):
        target = self.target or {}
        return {
            name: target.get(name)
            for name in ("project_label", "project_key", "label", "task_id", "pane_id", "terminal_id", "harness")
        }

    def _agent_drafts(self, kind):
        """The selected agent's open `kind` (diff or browser) drafts that have text, plus the
        one being edited."""
        prefix = self._agent_target() + "|"
        active = self.draft.get("target") if self.draft else None
        found = [
            draft
            for draft in self.drafts.open_drafts()
            if (draft.get("target") or "").startswith(prefix)
            and (draft.get("anchor") or {}).get("kind") == kind
            and draft.get("target") != active
            and (draft.get("body") or "").strip()
        ]
        if self.draft and active.startswith(prefix) and (self.draft.get("anchor") or {}).get("kind") == kind:
            found.append(self.draft)
        return found

    def _earlier_draft(self):
        """An unsent draft of this agent with no selection: the Agents message field's, or one the removed bottom composer left."""
        if self.draft is not None and not self.draft.get("anchor"):
            return self.draft
        target = self._agent_target()
        return next(
            (
                draft
                for draft in self.drafts.open_drafts()
                if draft.get("target") == target and ((draft.get("body") or "").strip() or draft.get("attachments"))
            ),
            None,
        )

    # ---- draft lifecycle -------------------------------------------------------------
    def _stamp_direct(self, draft, record):
        if not record.get("direct"):
            return
        anchor = dict(draft.get("anchor") or record)
        anchor["direct"] = True
        draft["anchor"] = anchor
        attachments = []
        stamped = False
        for item in draft.get("attachments") or []:
            if item.get("kind") == "browser" and not stamped:
                item = dict(item)
                item["direct"] = True
                stamped = True
            attachments.append(item)
        if not stamped:
            attachments.insert(0, dict(record))
        draft["attachments"] = attachments

    def _open_inline(self, record):
        """Open the editor for a selection: its existing draft, or a new one bound to it."""
        target = f"{self._agent_target()}|{anchor_key(record)}"
        if self.draft is not None and self.draft.get("target") == target:
            self._stamp_direct(self.draft, record)
            self.focus = "composer"
            return
        draft = self.drafts.load(target, self._binding())
        if not draft.get("anchor"):
            draft["anchor"] = record
            draft["attachments"] = [record]
        else:
            self._stamp_direct(draft, record)
        self._open_draft(draft)

    def _open_target(self, target):
        """Reopen an open draft by its target (a draft row, or the earlier composer draft)."""
        if self.draft is not None and self.draft.get("target") == target:
            self.focus = "composer"
            return
        draft = self.drafts.find(target)
        if draft is None:
            self.status = "that draft is no longer open"
            return
        self._open_draft(draft)

    def _open_draft(self, draft):
        # A comment needs the message area, so focus mode returns to the normal view.
        if self.tab == "review":
            self.review_focus = False
        self._close_inline()
        self.board_offer = None
        self.draft = draft
        self.draft_conflict = False
        self.composer.set(draft.get("body") or "")
        self.focus = "composer"

    def _close_inline(self):
        """Leave the editor; the draft stays on disk under its selection."""
        if self.draft is None:
            return
        self._leave_draft()
        self._drop_inline()

    def _drop_inline(self):
        self.draft = None
        self.board_offer = None
        self.draft_conflict = False
        self.composer.set("")
        if self.focus == "composer":
            self.focus = "list"

    def _save_draft(self):
        if self.draft_conflict:
            self.status = "draft conflict: not saved; Ctrl-R loads the other pane's draft"
            return False
        self.draft["body"] = self.composer.text
        if str(self.draft.get("target") or "").startswith("board:") and hasattr(self, "_stamp_board_request"):
            self._stamp_board_request(self.draft)
        if self.draft.get("revision", 0) == 0 and not self.draft["body"]:
            return True
        if self.drafts.save(self.draft):
            return True
        self.draft_conflict = True
        self.status = "draft conflict: another Firstpane pane saved this draft; nothing overwritten (Ctrl-R loads it)"
        return False

    def _sync_draft(self):
        """Pick up a save made by another pane when this pane has no unsaved edit."""
        if self.draft is None or self.draft_conflict or self.draft.get("revision", 0) == 0:
            return
        if self.composer.text != (self.draft.get("body") or ""):
            return
        current = self.drafts.reload(self.draft)
        if current and current.get("revision", 0) > self.draft.get("revision", 0):
            self.draft = current
            self.draft.setdefault("attachments", [])
            self.composer.set(current.get("body") or "")

    def _before_edit(self):
        """An attempted draft keeps its request id for replay; editing starts a new message."""
        if self.draft.get("outcome") not in ATTEMPTED:
            return
        old = self.draft
        self.drafts.archive(old)
        fresh = self.drafts.load(old["target"], old.get("binding"))
        fresh["body"] = self.composer.text
        fresh["attachments"] = list(old.get("attachments") or [])
        if old.get("anchor"):
            fresh["anchor"] = old["anchor"]
        self.draft = fresh
        self.status = f"new message started; the previous one stays {old['outcome']}"

    def _write_conflict_copy(self):
        mine = self.state_dir / "drafts" / f"conflict-{self.draft['id']}-{os.getpid()}.txt"
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text(self.composer.text)
        return mine

    def _reload_after_conflict(self):
        if not self.draft_conflict:
            return
        mine = self._write_conflict_copy()
        current = self.drafts.find(self.draft["target"])
        self.draft_conflict = False
        if current is None:
            self._drop_inline()
        else:
            self.draft = current
            self.composer.set(current.get("body") or "")
        self.status = f"loaded the other pane's draft; your text is in {mine}"

    def _leave_draft(self):
        """Before closing the editor: save, or keep conflicting text in a visible copy."""
        if self.draft_conflict:
            mine = self._write_conflict_copy()
            self.status = f"draft conflict: your text is in {mine}"
            return
        self._save_draft()

    # ---- keys ------------------------------------------------------------------------
    def _composer_key(self, key):
        if key == "ctrl-y" and hasattr(self, "_board_offer_is") and self._board_offer_is("rebind"):
            self._rebind_board_request()
            return
        if key == "ctrl-o" and hasattr(self, "_board_offer_is") and self._board_offer_is("plain"):
            self._send_board_plain()
            return
        if key in ("esc", "tab", "backtab"):
            self._close_inline()
            return
        if key in ("enter", "ctrl-enter"):
            self._arm_send()
            return
        if key == "ctrl-r":
            self._reload_after_conflict()
            return
        if key == "ctrl-x":
            if self.draft.get("attachments") and not self.draft.get("anchor"):
                self._before_edit()
                removed = self.draft["attachments"].pop()
                self._save_draft()
                self.status = f"removed attachment {len(self.draft['attachments']) + 1} ({removed.get('kind')})"
            return
        if key == "ctrl-e":
            self._composer_editor()
            return
        before = self.composer.text
        if not self.composer.key(key):
            if len(key) == 1 and key.isprintable():
                self._before_edit()
                self.composer.insert(key)
        if self.composer.text != before:
            self._before_edit()
            self._save_draft()

    def _composer_paste(self, text):
        self._before_edit()
        self.composer.insert(text, paste=True)
        self._save_draft()

    def _composer_editor(self):
        handle, name = tempfile.mkstemp(prefix="message-", suffix=".md", dir=str(self.state_dir))
        path = Path(name)
        with os.fdopen(handle, "w") as out:
            out.write(self.composer.text)
        body = self._edit(path)
        path.unlink(missing_ok=True)
        if body is None:
            return
        self._before_edit()
        self.composer.set(body.rstrip("\n"))
        self._save_draft()

    # ---- context, preview, send ------------------------------------------------------
    def _message_context(self):
        draft = self.draft or {}
        if str(draft.get("target") or "").startswith("board:"):
            record = next((item for item in draft.get("attachments") or [] if item.get("kind") == "board"), {})
            request = next((item for item in draft.get("attachments") or [] if item.get("kind") == "board-request"), None)
            context = {
                "board_task": record.get("id") or draft.get("target")[6:],
                "project": record.get("project"),
                "board_state": record.get("state"),
            }
            if request:
                context["board_request"] = request
                context["reply_id"] = draft.get("request_id")
            return context
        target = self.target
        if not target:
            return {}
        info = self._ws_info(target)
        ws = info.get("workspace") if info else None
        status = info.get("status") if info else None
        return {
            "project": target.get("project_label"),
            "project_key": target.get("project_key"),
            "agent": target.get("label"),
            "harness": target.get("harness"),
            "pane_id": target.get("pane_id"),
            "terminal_id": target.get("terminal_id"),
            "workspace": ws.path if ws and ws.path else self._ws_word(info),
            "provenance": ws.provenance if ws and ws.path else "",
            "branch": status.branch_label() if status else self._ws_word(info),
            "head": status.oid if status else None,
        }

    def _lines_changed(self, item):
        """True when the selected rows are no longer in the file's current diff."""
        entry = FileEntry(item.get("group"), item.get("code") or "M", item.get("path"), item.get("orig"))
        if item.get("group") == "commit":
            text, error = read_commit_diff(self.transport.run, item.get("workspace"), item.get("revision"), self.env)
            mine = [line for line, path in zip(parse_diff(text), patch_line_paths(text)) if path == item.get("path")]
            return bool(error) or locate(item, mine) is None
        text, error = read_diff(self.transport.run, item.get("workspace"), entry, self.env)
        return bool(error) or locate(item, parse_diff(text)) is None

    def _drift_warnings(self):
        warnings = []
        target = self.target
        if target and not self._target_live():
            warnings.append(f"agent closed: {target.get('label')} is no longer open")
        info = self._ws_info(target) if target else None
        ws = info.get("workspace") if info else None
        status = info.get("status") if info else None
        for index, item in enumerate(self.draft.get("attachments") or [], 1):
            if item.get("kind") == "pr-thread":
                warnings.extend(self._pr_thread_drift(index, item))
                continue
            if item.get("kind") != "diff":
                continue
            if item.get("group") == "pr":
                warnings.extend(self._pr_diff_drift(index, item))
                continue
            if ws is None or item.get("workspace") != ws.path:
                warnings.append(f"attachment {index} was captured in {item.get('workspace')}; workspace is now {ws.path if ws and ws.path else 'unknown'}")
                continue
            now_branch = status.branch_label() if status else "unknown"
            now_head = status.oid if status else None
            if (item.get("branch"), item.get("head")) != (now_branch, now_head):
                warnings.append(
                    f"attachment {index} was captured on {item.get('branch')} @ {(item.get('head') or '-')[:7]}; "
                    f"now {now_branch} @ {(now_head or '-')[:7]}"
                )
            if item.get("rows") and self._lines_changed(item):
                warnings.append(
                    f"attachment {index}: {item.get('path')} {range_label(item)} changed since they were selected; "
                    "the message carries the lines as selected"
                )
        return warnings

    def _pr_diff_drift(self, index, item):
        """Warnings for lines taken from a pull request's diff: the PR left the list or its head moved."""
        pr = next((row for row in self._all_prs() if row["number"] == item.get("number") and item.get("pr_url", "").endswith(f"/{row['owner']}/{row['repo']}/pull/{row['number']}")), None)
        if pr is None:
            return [f"attachment {index}: pull request #{item.get('number')} is not in the open PR list now"]
        head = (((self._pr_detail(pr) or {}).get("view")) or {}).get("head_oid")
        if head and item.get("head") and head != item["head"]:
            return [f"attachment {index} was captured at PR head {item['head'][:7]}; the head is now {head[:7]}"]
        return []

    def _arm_send(self):
        if self.draft_conflict:
            self.status = "draft conflict: Ctrl-R loads the other pane's draft"
            return
        if not self.composer.text.strip() and (self.draft.get("anchor") or not self.draft.get("attachments")):
            self.status = "nothing to send: write the message first"
            return
        self._save_draft()
        refusal = self._board_request_refusal() if hasattr(self, "_board_request_refusal") else None
        if refusal:
            self.status = refusal
            return
        handoff = Handoff(self.transport, self.config.firstmate_home)
        target, refusal = handoff.locate()
        refusal = refusal or handoff.check(target)
        if refusal:
            self.status = f"not transferred: {refusal}; the draft stays here"
            return
        self._refresh_target_now()
        body, warnings = build_message(self._message_context(), self.composer.text, self.draft.get("attachments"))
        self.pending = {"draft_id": self.draft["id"], "body": body, "warnings": warnings + self._drift_warnings(),
                        "target": target}
        self.screen = "confirm-send"

    def _confirm_send(self, plain):
        pending = self.pending or {}
        target = pending.get("target") or {}
        lines = [f"Put in FirstMate's message box (pane {target.get('pane_id')}, {target.get('agent')}); it is not sent"]
        lines.extend(paint("! " + warning, "1;33", plain) for warning in pending.get("warnings") or [])
        lines.extend(["---", pending.get("body", "").rstrip("\n"), "---",
                      "y puts it there (you press Enter in FirstMate), any other key keeps the draft"])
        return "\n".join(lines)

    def _send_message(self):
        pending = self.pending or {}
        if self.draft is None or pending.get("draft_id") != self.draft.get("id"):
            self.screen = "tree"
            self.pending = None
            self.status = "message not transferred: the draft changed"
            return
        refusal = self._board_request_refusal() if hasattr(self, "_board_request_refusal") else None
        if refusal:
            self.screen = "tree"
            self.pending = None
            self.status = refusal
            return
        self._refresh_target_now()
        body, warnings = build_message(self._message_context(), self.composer.text, self.draft.get("attachments"))
        warnings = warnings + self._drift_warnings()
        if body != pending.get("body"):
            self.pending = dict(pending, body=body,
                                warnings=["the message changed since the preview; this is the updated text"] + warnings)
            return
        self.screen = "tree"
        self.pending = None
        outcome, detail = Handoff(self.transport, self.config.firstmate_home).transfer(pending["target"], body)
        if outcome == "refused":
            self.status = f"not transferred: {detail}; the draft stays here"
            return
        self.draft["outcome"] = outcome
        self.draft["sent_body"] = body
        self.draft["finished_at"] = self.clock()
        if not self.drafts.save(self.draft):
            self.drafts.archive(self.draft)
        if outcome == "transferred":
            self.status = detail
            self._drop_inline()
        else:
            self.status = f"transfer unconfirmed: {detail}"

    # ---- drawing ---------------------------------------------------------------------
    def _inline_label(self, draft):
        anchor = draft.get("anchor")
        if not anchor:
            count = len(draft.get("attachments") or [])
            noun = "attachment" if count == 1 else "attachments"
            return "earlier message" + (f" · {count} {noun}" if count else "")
        if anchor.get("kind") == "diff":
            return f"{sanitize(anchor.get('path'))} {range_label(anchor)}"
        if anchor.get("kind") == "pr-thread":
            where = sanitize(anchor.get("path") or "")
            span = anchor.get("range")
            return f"#{anchor.get('number')} " + (f"{where}:{span}" if span else where or "conversation")
        title = sanitize(anchor.get("title") or anchor.get("url") or "").replace("\n", " ")
        return f'browser "{title}"'

    def _context_footer_active(self):
        """Changes and Pull Requests pin their context composer when the pane can hold it."""
        return self.screen == "tree" and (self._on("changes") or self._on("prs")) and self.rows >= 16

    def _open_context_draft(self):
        draft = self.draft
        kind = (draft.get("anchor") or {}).get("kind") if draft else None
        if self._on("changes") and kind == "diff":
            return draft
        if self._on("prs") and kind in ("pr-thread", "diff"):
            return draft
        return None

    def _footer_holds(self, draft):
        return self._context_footer_active() and draft is self._open_context_draft()

    def _context_footer(self, plain):
        """Bottom composer for the open selection or conversation. Empty until one is added."""
        if not self._context_footer_active():
            return []
        if self._review_focused():
            bar = self._mode_bar(plain)
            return [(bar, None)] if bar else []
        draft = self._open_context_draft()
        kind = (draft.get("anchor") or {}).get("kind") if draft else None
        title = "Write to FirstMate"
        if draft:
            count = "1 comment added" if kind == "pr-thread" else "1 selection added"
        else:
            count = ""
        label = " " + paint(title, "1;" + rgb_fg(ACCENT), plain)
        right = paint("To: FirstMate ", rgb_fg(MUTED), plain)
        if count:
            right = paint(count + "  ", rgb_fg(MUTED), plain) + right
        rows = [(self._footer_spread(label, right), {"kind": "context-footer", "part": "label"})]
        if draft:
            chip = self._inline_label(draft) + " \u00d7"
            shown = chip if width(chip) <= self.cols - 2 else tail(chip, self.cols - 2)
            text = " " + paint(shown, rgb_fg(INK) + ";" + rgb_bg(FIELD_BG), plain)
            rows.append((text, {"kind": "context-footer", "part": "chip", "dismiss": 1 + width(shown)}))
        placeholder = "Select lines to write to FirstMate" if self._on("changes") else "Add a conversation or select diff lines to write to FirstMate"
        rows.append(self._footer_field(draft, placeholder, plain))
        hint = "Enter previews for FirstMate's message box; it is not sent" if draft else "Enter on a selection adds it here"
        if draft and draft.get("outcome"):
            hint = f"last send: {draft['outcome']} \u00b7 " + hint
        rows.append((" " + paint(hint, rgb_fg(MUTED), plain), {"kind": "context-footer", "part": "hint"}))
        bar = self._mode_bar(plain)
        if bar:
            rows.append((bar, None))
        return rows

    def _mode_bar(self, plain):
        """`NORMAL` or `VISUAL · n lines` while a diff is shown, then the diff keys."""
        view = self.diff_view
        shown = self._on("changes") or (self._on("prs") and self.pr_diff_open)
        if not shown or not view or not view.get("lines"):
            return ""
        if self.focus == "composer":
            mode, keys = "MESSAGE", "keys type text \u00b7 Esc leaves the message"
            send = "Ctrl+Enter Preview, then y"
        else:
            if view.get("mark") is not None:
                low, high = self._selection()
                count = sum(1 for line in view["lines"][low : high + 1] if line.kind in "+- ")
                mode = f"VISUAL \u00b7 {count} line" + ("" if count == 1 else "s")
            else:
                mode = "NORMAL"
            keys = "h j k l Move \u00b7 w b Word \u00b7 v/V Select \u00b7 gd gI no symbol support \u00b7 Ctrl-O Back"
            send = "Enter Write"
        left = " " + paint(mode, "1;" + rgb_fg(ACCENT), plain) + "   " + paint(keys, rgb_fg(MUTED), plain)
        return self._footer_spread(fit(left, max(1, self.cols - len(send) - 3)), paint(send + " ", rgb_fg(INK), plain))

    def _footer_spread(self, left, right):
        from firstpane.cells import spread
        return spread(left, right, self.cols)

    def _footer_field(self, draft, placeholder, plain):
        focused = draft is not None and self.focus == "composer"
        text = self.composer.text if draft is not None else ""
        button = " Send "
        box = max(8, self.cols - width(button) - 3)
        limit = max(1, box - 2)
        if not text and not focused:
            field = paint(fit(placeholder, limit), rgb_fg(MUTED), plain)
        elif focused:
            cursor = self.composer.cursor
            line_start = text.rfind("\n", 0, cursor) + 1
            line_end = text.find("\n", cursor)
            line = text[line_start: len(text) if line_end < 0 else line_end]
            column = cursor - line_start
            before, after = line[:column], line[column:]
            while width(before) > limit - 1:
                before = before[1:]
            rest = fit(after[1:], max(0, limit - 1 - width(before)))
            caret = after[:1] or " "
            field = paint(before, rgb_fg(INK), plain) + paint(caret, "7", plain) + paint(rest, rgb_fg(INK), plain)
        else:
            field = paint(fit(text.split("\n")[0], limit), rgb_fg(INK), plain)
        inner = " " + field
        pad = " " * max(0, box - width(inner))
        send = paint(button, "1;" + rgb_fg((254, 255, 255)) + ";" + rgb_bg(SEND_BG), plain)
        line = " " + paint(inner + pad, rgb_bg(FIELD_BG), plain) + send
        return line, {"kind": "context-footer", "part": "field", "send_at": 2 + box}

    def _context_footer_click(self, hit, column):
        if hit.get("part") == "chip" and column >= hit.get("dismiss", 0):
            self._close_inline()
            return
        draft = self._open_context_draft()
        if hit.get("part") == "field" and column >= hit.get("send_at", self.cols):
            if draft and self.composer.text.strip():
                self._arm_send()
            else:
                self.status = "nothing to send: write the message first" if draft else self._footer_need()
            return
        if draft:
            self.focus = "composer"
            return
        self.status = self._footer_need()

    def _footer_need(self):
        if self._on("changes"):
            return "select diff lines to write about"
        return "add a conversation to write about"

    def _inline_rows(self, draft, plain, indent, stale=False):
        """Rows for one draft: the open editor under its selection, or one collapsed line."""
        indent = max(0, min(indent, self.cols - 12))
        lead = " " * indent + paint(BAR, rgb_fg(YELLOW if stale else ACCENT), plain) + " "
        hit = {"kind": "draft", "target": draft.get("target")}
        label = self._inline_label(draft)
        if self.draft is None or draft.get("target") != self.draft.get("target"):
            first = sanitize((draft.get("body") or "").strip().split("\n")[0])
            word = paint("stale draft", "1;" + rgb_fg(YELLOW), plain) if stale else "draft"
            return [(f"{lead}{word} · {first}  " + paint(f"({label})", "2", plain), hit)]
        head = "to FirstMate · " + label
        if stale:
            head += " · " + paint("stale: these lines changed", "1;" + rgb_fg(YELLOW), plain)
        if self.draft_conflict:
            head += " · " + paint("draft conflict", "1;" + rgb_fg(PINK), plain)
        elif draft.get("outcome"):
            head += f" · last send: {draft['outcome']}"
        rows = [(lead + head, hit)]
        anchor = draft.get("anchor") or {}
        if anchor.get("kind") == "browser":
            excerpt = sanitize(anchor.get("element") or anchor.get("selection") or "").split("\n")
            rows.extend((lead + paint("> " + line, rgb_fg(MUTED), plain), hit) for line in excerpt[:EXCERPT_ROWS] if line)
        avail = max(4, self.cols - indent - 4)
        rows.extend((lead + line, hit) for line in self._editor_lines(plain, avail))
        rows.append((lead + paint("Enter previews for FirstMate's message box · Esc keeps the draft", rgb_fg(MUTED), plain), hit))
        return rows

    def _editor_lines(self, plain, avail):
        focused = self.focus == "composer"
        prompt = paint("\u203a", "1;" + rgb_fg(ACCENT) if focused else rgb_fg(MUTED), plain)
        text, cursor = self.composer.text, self.composer.cursor
        if not text and not focused:
            return [f"{prompt} " + paint("write the message", rgb_fg(MUTED), plain)]
        lines = text.split("\n")
        line_no = text.count("\n", 0, cursor)
        column = cursor - (text.rfind("\n", 0, cursor) + 1)
        first = max(0, line_no - EDITOR_ROWS + 1)
        out = []
        for number in range(first, min(len(lines), first + EDITOR_ROWS)):
            line = lines[number]
            if number == line_no and focused:
                before, after = line[:column], line[column:]
                shown_before = tail(before, max(1, avail - 1))
                if plain:
                    body = shown_before + "\u258f" + after
                else:
                    rest = fit(after[1:], max(0, avail - 1 - width(shown_before)))
                    body = shown_before + paint(after[:1] or " ", "7", plain) + rest
            else:
                body = line
            out.append(f"{prompt if number == first else ' '} {body}")
        return out
