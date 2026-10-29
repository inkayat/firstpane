"""The exact FirstMate message: context header, user text, numbered marked attachments."""

import re

from firstpane.sanitize import sanitize

LINE_LIMIT = 200
ATTACHMENT_BYTES = 16 * 1024
BODY_BYTES = 64 * 1024
SECRETS = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("ghp_", re.compile(r"\bghp_[A-Za-z0-9]{20,}")),
    ("github_pat_", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("sk-", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AKIA", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("xox", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
)


def _short(sha):
    return (sha or "-")[:12]


def context_lines(context):
    if context and context.get("board_task"):
        request = context.get("board_request")
        lines = [f"board task: {context['board_task']}"]
        if context.get("project"):
            lines.append(f"project: {context['project']}")
        if context.get("board_state"):
            lines.append(f"state: {context['board_state']}")
        if request:
            lines.append(f"firstmate home: {request.get('home') or ''}")
            lines.append(
                f"request: open captain call {request.get('id')} lifecycle {request.get('lifecycle')}"
            )
            asked = sanitize(request.get("question") or "")[:600]
            asked_lines = asked.split("\n") or [""]
            lines.append("asked: " + asked_lines[0])
            lines.extend(asked_lines[1:])
            lines.append(f"reply id: {context.get('reply_id') or ''}")
            lines.append("this is the captain's reply to that request; it is recorded only when FirstMate records it")
        else:
            lines.append("not a dispatch; the task is context for this unsent message")
        return [sanitize(line) for line in lines]
    if not context or not context.get("agent"):
        return ["agent: none selected"]
    lines = []
    if context.get("project"):
        lines.append(f"project: {context['project']}  {context.get('project_key', '')}".rstrip())
    agent = [context["agent"], context.get("harness") or ""]
    if context.get("pane_id"):
        agent.append(f"pane {context['pane_id']}")
    if context.get("terminal_id"):
        agent.append(f"terminal {context['terminal_id']}")
    lines.append("agent: " + "  ".join(part for part in agent if part))
    workspace = context.get("workspace") or "unknown"
    provenance = context.get("provenance")
    lines.append(f"workspace: {workspace}" + (f"  ({provenance})" if provenance else ""))
    branch = context.get("branch") or "unknown"
    head = context.get("head")
    lines.append(f"branch: {branch}" + (f" @ {_short(head)}" if head else ""))
    return [sanitize(line) for line in lines]


def _fence(content, info):
    ticks = 3
    while "`" * ticks in content:
        ticks += 1
    fence = "`" * ticks
    return f"{fence}{info}\n{content}\n{fence}\n" if content else f"{fence}{info}\n{fence}\n"


def _clip(lines, total):
    shown = []
    size = 0
    for line in lines[:LINE_LIMIT]:
        size += len(line.encode()) + 1
        if size > ATTACHMENT_BYTES:
            break
        shown.append(line)
    total = max(total, len(lines))
    note = f"[truncated: shown {len(shown)} of {total} lines]\n" if len(shown) < total else ""
    return shown, note


def _ranges(attachment):
    """`old lines a-b  new lines c-d` for the sides the selection has lines on."""
    parts = [
        f"{side} lines {attachment[side + '_start']}-{attachment[side + '_end']}"
        for side in ("old", "new")
        if attachment.get(side + "_start") is not None
    ]
    if not parts:
        parts.append(f"{attachment.get('side')} lines {attachment.get('start')}-{attachment.get('end')}")
    return "  ".join(parts)


def attachment_text(index, attachment):
    kind = attachment.get("kind")
    if kind == "diff":
        lines = [sanitize(line) for line in attachment.get("lines") or []]
        shown, note = _clip(lines, attachment.get("lines_total") or 0)
        head = (
            f"[{index}] diff {sanitize(attachment.get('path'))}  {attachment.get('group')}  {_ranges(attachment)}  "
            f"HEAD {_short(attachment.get('head'))}  {attachment.get('side')} blob {_short(attachment.get('blob'))}\n"
        )
        if attachment.get("source") or attachment.get("revision"):
            head += f"source {attachment.get('source') or 'workspace'}  revision {attachment.get('revision') or 'uncommitted'}"
            head += f"  {sanitize(attachment['pr_url'])}\n" if attachment.get("pr_url") else "\n"
        found = ""
        for finding in attachment.get("findings") or []:
            skill = f" ({sanitize(finding.get('skill'))})" if finding.get("skill") else ""
            stale = f" [{sanitize(finding.get('stale'))}]" if finding.get("stale") else ""
            found += f"review finding {sanitize(finding.get('severity'))}{skill}{stale}: {sanitize(finding.get('title'))}\n"
            if finding.get("body"):
                found += sanitize(finding["body"]) + "\n"
        return head + found + _fence("\n".join(shown), "diff") + note
    if kind == "browser":
        title = sanitize(attachment.get("title") or "").replace("\n", " ")
        head = (
            f'[{index}] browser "{title}"  {sanitize(attachment.get("url"))}  '
            f"instance {sanitize(attachment.get('browser_key'))} tab {sanitize(attachment.get('tab_id'))}  "
            f"captured {sanitize(attachment.get('captured_at'))}\n"
        )
        parts = [head]
        element = sanitize(attachment.get("element") or "")
        if element:
            lines = element.split("\n")
            shown, note = _clip(lines, len(lines))
            parts += ["element picked with the browser's select tool:\n", _fence("\n".join(shown), "text"), note]
        selection = sanitize(attachment.get("selection") or "")
        if selection:
            lines = selection.split("\n")
            shown, note = _clip(lines, len(lines))
            parts += [_fence("\n".join(shown), "text"), note]
        elif not element:
            parts.append("(no text selected)\n")
        return "".join(parts)
    if kind == "pr-thread":
        return _pr_thread_text(index, attachment)
    if kind == "board":
        reason = sanitize(attachment.get("hold_reason") or "")
        text = (
            f"[{index}] board task {sanitize(attachment.get('id'))}  "
            f"{sanitize(attachment.get('project'))}  {sanitize(attachment.get('state'))}\n"
            f"{sanitize(attachment.get('title'))}\n"
        )
        if reason:
            text += f"reason: {reason}\n"
        return text
    return f"[{index}] unsupported attachment\n"


REVIEW_WORDS = {
    "CHANGES_REQUESTED": "changes requested",
    "APPROVED": "approved",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
    "PENDING": "pending review",
}


def _pr_thread_text(index, attachment):
    """A pull request conversation as it was added: where, its code, every captured comment."""
    state = "resolved" if attachment.get("resolved") else "unresolved"
    if attachment.get("outdated"):
        state += ", outdated"
    where = sanitize(attachment.get("path") or "") or "(no file)"
    if attachment.get("range"):
        where += ":" + sanitize(attachment["range"])
        if attachment.get("side") == "LEFT":
            where += " (old side)"
    head = (
        f"[{index}] pr conversation {sanitize(attachment.get('slug'))}#{attachment.get('number')}  {where}  {state}  "
        f"thread {sanitize(attachment.get('thread_id'))}  HEAD {_short(attachment.get('head'))}  "
        f"captured {sanitize(attachment.get('captured_at'))}\n"
        f"{sanitize(attachment.get('url'))}\n"
    )
    parts = [head]
    excerpt = [sanitize(str(row[0]) + str(row[2])) for row in attachment.get("excerpt") or [] if len(row) == 3]
    if excerpt:
        shown, note = _clip(excerpt, len(excerpt))
        parts.append(_fence("\n".join(shown), "diff") + note)
    if attachment.get("excerpt_note"):
        parts.append(f"({sanitize(attachment['excerpt_note'])})\n")
    conversation = []
    for comment in attachment.get("comments") or []:
        review = REVIEW_WORDS.get(comment.get("review"))
        author = sanitize(comment.get("author") or "") or "unknown author"
        line = f"@{author} {sanitize(comment.get('created') or 'time unknown')}" + (f" ({review})" if review else "") + ":"
        if conversation:
            conversation.append("")
        conversation.append(line)
        conversation.extend(sanitize(comment.get("body") or "").split("\n"))
    shown, note = _clip(conversation, len(conversation))
    parts.append(_fence("\n".join(shown), "text") + note)
    missing = (attachment.get("comments_total") or 0) - len(attachment.get("comments") or [])
    if missing > 0:
        parts.append(f"[{missing} more comments on GitHub were not captured]\n")
    return "".join(parts)


def scan_secrets(index, text):
    found = [name for name, pattern in SECRETS if pattern.search(text)]
    if not found:
        return []
    return [f"attachment {index} looks like it contains a secret ({', '.join(found)})"]


def build_message(context, user_text, attachments):
    """(body, warnings). The preview shows exactly `body`; the same string is sent."""
    parts = ["Firstpane message\n" + "\n".join(context_lines(context)) + "\n"]
    text = sanitize(user_text or "").strip("\n")
    if text:
        parts.append("\n" + text + "\n")
    warnings = []
    size = sum(len(part.encode()) for part in parts)
    for index, attachment in enumerate(attachments or [], 1):
        block = attachment_text(index, attachment)
        warnings.extend(scan_secrets(index, block))
        if size + len(block.encode()) + 1 > BODY_BYTES:
            parts.append(f"\n[truncated: attachments {index}-{len(attachments)} omitted, message limit]\n")
            break
        parts.append("\n" + block)
        size += len(block.encode()) + 1
    return "".join(parts), warnings
