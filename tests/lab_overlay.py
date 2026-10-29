"""Test-only snapshot overlay. Does not write Herdr and does not change product binding rules."""

import copy
import os

SYNTHETIC_NAME = "synthetic-lab-agent"
SYNTHETIC_AGENT = "synthetic"


def verify_shell(pane, expected_cwd):
    """Return the owned shell facts, or raise if they are not this directory's pane."""
    required = ("pane_id", "terminal_id", "workspace_id", "tab_id", "cwd")
    missing = [key for key in required if not pane.get(key)]
    if missing:
        raise ValueError("shell missing " + ",".join(missing))
    if os.path.realpath(pane["cwd"]) != os.path.realpath(expected_cwd):
        raise ValueError("cwd mismatch")
    return {key: pane[key] for key in required}


def overlay_snapshot(raw, shell):
    """Copy a native snapshot and add one synthetic agent bound to those exact facts."""
    snap = copy.deepcopy(raw)
    agent = {
        "name": SYNTHETIC_NAME,
        "agent": SYNTHETIC_AGENT,
        "agent_status": "idle",
        "cwd": shell["cwd"],
        "foreground_cwd": shell["cwd"],
        "pane_id": shell["pane_id"],
        "terminal_id": shell["terminal_id"],
        "workspace_id": shell["workspace_id"],
        "tab_id": shell["tab_id"],
        "focused": False,
        "revision": 1,
    }
    for pane in snap.get("panes") or []:
        if isinstance(pane, dict) and pane.get("pane_id") == shell["pane_id"]:
            pane["name"] = SYNTHETIC_NAME
    snap.setdefault("agents", [])
    snap["agents"] = [row for row in snap["agents"] if row.get("pane_id") != shell["pane_id"]]
    snap["agents"].append(agent)
    return snap


def mismatch_snapshot(raw, shell):
    """Same overlay with a stale pane id, so it cannot bind to the owned shell."""
    snap = overlay_snapshot(raw, shell)
    for row in snap["agents"]:
        if row.get("name") == SYNTHETIC_NAME:
            row["pane_id"] = "stale-not-owned"
    return snap
