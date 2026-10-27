"""Explicit Git writes on one agent workspace: preview from a fresh read, re-check at execute.

Offered: stage, unstage, commit, fetch, pull (fast-forward only), push. Never force,
stash, discard, reset, clean, rebase, checkout, amend or hook bypass. A timeout or signal is
an unknown outcome and is never retried.
"""

import hashlib
import re
from dataclasses import dataclass, field

from firstpane.sanitize import sanitize
from firstpane.workspace import canonical, git_argv, read_env, read_status

TIMEOUTS = {"stage": 30, "unstage": 30, "commit": 120, "fetch": 60, "pull": 60, "push": 120}
QUIET_STATUSES = ("idle", "done")
_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@")


def strip_credentials(url):
    return _USERINFO.sub(r"\1", url or "")


@dataclass
class State:
    workspace: str
    status: object
    index_id: str
    remote: str | None
    remote_url: str | None
    merge_ref: str | None


@dataclass
class Plan:
    action: str
    workspace: str
    argv: list = field(default_factory=list)
    paths: list = field(default_factory=list)
    message: str = ""
    preview: list = field(default_factory=list)
    refusal: str = ""
    state: State | None = None


@dataclass
class Outcome:
    kind: str  # done | failed | refused | unknown
    detail: str = ""


def _git(run, ws, env, *args, timeout=10):
    return run(git_argv(ws, *args), env=read_env(env), timeout=timeout, cwd=ws)


def _config(run, ws, env, key):
    result = _git(run, ws, env, "config", "--get", key)
    value = (result.stdout or "").strip()
    return value if result.code == 0 and value else None


def capture(run, env, ws, preferred_remote):
    status, error = read_status(run, ws, env)
    if status is None:
        return None, error
    listing = _git(run, ws, env, "ls-files", "--stage", "-z")
    if listing.code != 0:
        return None, "unavailable: git ls-files failed"
    index_id = hashlib.sha256((listing.stdout or "").encode()).hexdigest()
    remote = merge_ref = None
    if status.head and not status.detached:
        remote = _config(run, ws, env, f"branch.{status.head}.remote")
        merge_ref = _config(run, ws, env, f"branch.{status.head}.merge")
    if not remote or not status.upstream:
        remote, merge_ref = None, None
    chosen = remote or preferred_remote
    url = None
    if chosen:
        result = _git(run, ws, env, "remote", "get-url", chosen)
        url = (result.stdout or "").strip() if result.code == 0 else None
        if url is None and not remote:
            fallback = "origin" if preferred_remote != "origin" else None
            if fallback:
                again = _git(run, ws, env, "remote", "get-url", fallback)
                if again.code == 0:
                    chosen, url = fallback, (again.stdout or "").strip()
    if url is None:
        chosen = None
    return State(ws, status, index_id, chosen, url, merge_ref), ""


def _paths_listed(status, paths, groups):
    listed = {entry.path for entry in status.entries if entry.group in groups}
    return [path for path in paths if path not in listed]


def prepare(action, run, env, ws, *, paths=(), message_path=None, preferred_remote="origin", busy_agents=()):
    plan = Plan(action, ws, paths=list(paths))
    state, error = capture(run, env, ws, preferred_remote)
    if state is None:
        plan.refusal = error
        plan.preview = [f"{action} refused: {error}"]
        return plan
    plan.state = state
    status = state.status
    plan.refusal = _refusal(action, state, plan, message_path, busy_agents)
    if action == "stage":
        plan.argv = git_argv(ws, "add", "--", *plan.paths)
        shown = f"git add -- {' '.join(plan.paths)}"
    elif action == "unstage":
        plan.argv = git_argv(ws, "restore", "--staged", "--", *plan.paths)
        shown = f"git restore --staged -- {' '.join(plan.paths)}"
    elif action == "commit":
        plan.argv = git_argv(ws, "commit", "-F", message_path or "")
        shown = "git commit -F <message below>"
        plan.message = _read_message(message_path)
    elif action == "fetch":
        plan.argv = git_argv(ws, "fetch", state.remote or "")
        shown = f"git fetch {state.remote}"
    elif action == "pull":
        plan.argv = git_argv(
            ws, "pull", "--ff-only", "--no-rebase", "--no-autostash", state.remote or "", state.merge_ref or ""
        )
        shown = f"git pull --ff-only --no-rebase --no-autostash {state.remote} {state.merge_ref or '<no upstream>'}"
    elif action == "push":
        branch = status.head or ""
        if state.merge_ref:
            plan.argv = git_argv(ws, "push", state.remote or "", f"refs/heads/{branch}:{state.merge_ref}")
            shown = f"git push {state.remote} refs/heads/{branch}:{state.merge_ref}"
        else:
            plan.argv = git_argv(ws, "push", "-u", state.remote or "", f"refs/heads/{branch}:refs/heads/{branch}")
            shown = f"git push -u {state.remote} refs/heads/{branch}:refs/heads/{branch}"
    else:
        plan.refusal = f"unknown action {action}"
        shown = action
    plan.preview = _preview(plan, state, shown)
    return plan


def _read_message(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return sanitize(handle.read())
    except (OSError, TypeError):
        return ""


def _refusal(action, state, plan, message_path, busy_agents):
    status = state.status
    if action in ("stage", "unstage") and not plan.paths:
        return "no file selected"
    if action == "stage":
        missing = _paths_listed(status, plan.paths, ("unstaged", "untracked", "conflicted"))
        if missing:
            return "not changed: " + ", ".join(missing)
    if action == "unstage":
        if status.oid is None:
            return "unstage refused: no commits yet"
        missing = _paths_listed(status, plan.paths, ("staged",))
        if missing:
            return "not staged: " + ", ".join(missing)
    if action in ("commit", "pull", "push") and status.detached:
        return f"{action} refused: detached at {(status.oid or '?')[:7]}"
    if action == "commit":
        if not any(entry.group == "staged" for entry in status.entries):
            return "commit refused: nothing staged"
        if not _read_message(message_path).strip():
            return "commit refused: empty message"
    if action in ("fetch", "pull", "push") and not state.remote:
        return f"{action} refused: no remote"
    if action == "pull":
        if not state.merge_ref:
            return "pull refused: no upstream"
        if not status.clean:
            return "pull refused: worktree not clean"
        if busy_agents:
            return "pull refused: agent busy in this worktree: " + ", ".join(busy_agents)
    if action == "push" and status.oid is None:
        return "push refused: no commits yet"
    return ""


def _preview(plan, state, shown):
    status = state.status
    lines = [
        f"workspace  {state.workspace}",
        f"branch     {status.branch_label()}",
        f"HEAD       {status.oid or '(none)'}",
    ]
    if plan.action in ("fetch", "pull", "push"):
        upstream = status.upstream or "none"
        counts = ""
        if status.ahead is not None:
            counts = f"  ({status.ahead} ahead, {status.behind} behind)"
        lines.append(f"upstream   {upstream}{counts}")
        lines.append(f"remote     {state.remote or 'none'}  {sanitize(strip_credentials(state.remote_url)) if state.remote_url else ''}".rstrip())
    if plan.action == "push" and not state.merge_ref and state.remote:
        lines.append(f"note       sets upstream {state.remote}/{status.head}")
    if plan.action == "commit":
        staged = [entry for entry in status.entries if entry.group == "staged"]
        lines.append(f"staged     {len(staged)} file(s)")
        lines.extend(f"  {entry.code} {sanitize(entry.path)}" for entry in staged)
    lines.append(f"command    {sanitize(shown)}")
    if plan.action == "commit":
        lines.append("message:")
        lines.extend("  " + line for line in (plan.message or "").splitlines())
    if plan.refusal:
        lines.append(f"refused    {plan.refusal}")
    return lines


def _drift(plan, fresh):
    old, new = plan.state.status, fresh.status
    if canonical(fresh.workspace) != canonical(plan.workspace):
        return "workspace changed"
    if (old.head, old.detached) != (new.head, new.detached):
        return f"branch changed: {old.branch_label()} -> {new.branch_label()}"
    if plan.action in ("commit", "pull", "push", "unstage") and old.oid != new.oid:
        return f"HEAD moved: {(old.oid or '-')[:7]} -> {(new.oid or '-')[:7]}"
    if plan.action == "commit" and plan.state.index_id != fresh.index_id:
        return "index changed since the preview"
    if plan.action == "commit" and _read_message(plan.argv[-1]) != plan.message:
        return "commit message changed since the preview"
    if plan.action in ("fetch", "pull", "push"):
        if (plan.state.remote, plan.state.remote_url) != (fresh.remote, fresh.remote_url):
            return "remote changed since the preview"
        if plan.action in ("pull", "push") and (old.upstream, plan.state.merge_ref) != (new.upstream, fresh.merge_ref):
            return "upstream changed since the preview"
    return ""


def execute(plan, run, env, current_ws, *, busy_agents=(), preferred_remote="origin"):
    """Run a confirmed plan after re-reading the target. No retry."""
    if plan.refusal or plan.state is None:
        return Outcome("refused", plan.refusal or "no preview")
    if not current_ws or canonical(current_ws) != canonical(plan.workspace):
        return Outcome("refused", "workspace changed since the preview")
    fresh, error = capture(run, env, plan.workspace, preferred_remote)
    if fresh is None:
        return Outcome("refused", error)
    drift = _drift(plan, fresh)
    if drift:
        return Outcome("refused", drift)
    again = _refusal(plan.action, fresh, plan, plan.argv[-1] if plan.action == "commit" else None, busy_agents)
    if again:
        return Outcome("refused", again)
    write_env = dict(env or {})
    write_env["GIT_TERMINAL_PROMPT"] = "0"
    result = run(plan.argv, env=write_env, timeout=TIMEOUTS[plan.action], cwd=plan.workspace, detach=True)
    if result.timed_out or result.code < 0:
        return Outcome("unknown", f"{plan.action} outcome unknown: check the repository before retrying")
    if result.code != 0:
        first = next((line for line in (result.stderr or result.stdout or "").splitlines() if line.strip()), "")
        return Outcome("failed", f"{plan.action} failed: {sanitize(first)[:200]}")
    return Outcome("done", f"{plan.action} done")
