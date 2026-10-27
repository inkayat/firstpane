"""Toggle Firstpane in the focused tab: a Herdr plugin action, bound to a key by the user.

No Firstpane in the tab: open one split right of the focused pane, without taking focus.
One Firstpane that this toggle opened: close it with `plugin pane close`. Herdr has no
hide/show for a pane, so the pane process ends; drafts and UI state are on disk and the next
Firstpane loads them.

Herdr's pane list does not say which plugin owns a pane, and a pane title is only a label.
A pane is recognised only by the pane id and terminal id that `plugin pane open` returned
when this toggle opened it, recorded under the plugin state directory together with the Herdr
session (its socket path) it was opened in. The state directory is shared by every session, so
a press reads, prunes and rewrites only its own session's records. Anything less certain
is refused and changes nothing: an unreadable layout, two recognised panes in the tab, or a
pane titled Firstpane that this toggle did not open. Presses are serialised with a file lock,
so rapid presses alternate instead of both opening.
"""

import fcntl
import json
import os
import sys
from pathlib import Path

from firstpane.transport import LiveTransport

ENTRYPOINT = "tree"
TITLE = "Firstpane"  # the manifest pane title Herdr shows as the pane label
TIMEOUT = 10


class Refused(Exception):
    pass


def _herdr(env, transport, *args):
    done = transport.run([env.get("HERDR_BIN_PATH") or "herdr", *args], timeout=TIMEOUT)
    try:
        reply = json.loads(done.stdout if done.code == 0 else done.stderr)
    except ValueError:
        reply = None
    if done.code != 0 or not isinstance(reply, dict) or "result" not in reply:
        error = (reply or {}).get("error") if isinstance(reply, dict) else None
        detail = error.get("code") if isinstance(error, dict) else (done.stderr or done.stdout).strip()[:200]
        raise Refused(f"herdr {' '.join(args[:3])} failed: {detail or 'exit ' + str(done.code)}")
    return reply["result"]


def _read_records(path):
    try:
        records = json.loads(path.read_text()).get("panes", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [r for r in records if isinstance(r, dict) and r.get("pane_id") and r.get("terminal_id")]


def _write_records(path, records):
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps({"panes": records}))
    os.replace(temp, path)


def _toggle_locked(env, transport, records_path):
    plugin = env.get("HERDR_PLUGIN_ID")
    workspace, tab, focused = env.get("HERDR_WORKSPACE_ID"), env.get("HERDR_TAB_ID"), env.get("HERDR_PANE_ID")
    if not (plugin and workspace and tab and focused):
        raise Refused("Herdr gave no focused workspace, tab and pane")
    panes = _herdr(env, transport, "pane", "list").get("panes")
    if not isinstance(panes, list):
        raise Refused("Herdr pane list has no panes")
    live = {pane.get("pane_id"): pane for pane in panes if isinstance(pane, dict)}
    # A record survives only while its pane id still runs the terminal it was opened on.
    session = env.get("HERDR_SOCKET_PATH") or ""
    stored = _read_records(records_path)
    elsewhere = [r for r in stored if r.get("session", "") != session]
    records = [r for r in stored if r.get("session", "") == session
               and live.get(r["pane_id"], {}).get("terminal_id") == r["terminal_id"]]
    _write_records(records_path, elsewhere + records)
    ours = {r["pane_id"] for r in records}
    in_tab = [pane for pane in live.values() if pane.get("tab_id") == tab]
    mine = [pane["pane_id"] for pane in in_tab if pane["pane_id"] in ours]
    unknown = [pane["pane_id"] for pane in in_tab if pane["pane_id"] not in ours and pane.get("label") == TITLE]
    if unknown:
        raise Refused(f"{TITLE} pane {', '.join(unknown)} in this tab was not opened by this toggle; "
                      "close it yourself, then press again")
    if len(mine) > 1:
        raise Refused(f"{len(mine)} {TITLE} panes ({', '.join(mine)}) are in this tab; close one yourself")
    if mine:
        _herdr(env, transport, "plugin", "pane", "close", mine[0])
        _write_records(records_path, elsewhere + [r for r in records if r["pane_id"] != mine[0]])
        return f"closed {TITLE} {mine[0]}"
    opened = _herdr(env, transport, "plugin", "pane", "open", "--plugin", plugin, "--entrypoint", ENTRYPOINT,
                    "--placement", "split", "--direction", "right", "--target-pane", focused, "--no-focus")
    info = opened.get("plugin_pane") or {}
    pane = info.get("pane") or {}
    if info.get("plugin_id") != plugin or info.get("entrypoint") != ENTRYPOINT or not pane.get("terminal_id"):
        raise Refused(f"Herdr opened a pane but did not confirm it as {plugin}/{ENTRYPOINT}: {json.dumps(info)[:200]}")
    _write_records(records_path, elsewhere + records
                   + [{"session": session, "pane_id": pane["pane_id"], "terminal_id": pane["terminal_id"]}])
    return f"opened {TITLE} {pane['pane_id']}"


def toggle(env, transport, state_dir):
    """Returns (exit code, message). Refusals change no pane and show a Herdr notification."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "toggle.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return 0, _toggle_locked(env, transport, state_dir / "toggle.json")
        except Refused as refusal:
            reason = str(refusal)
    transport.run([env.get("HERDR_BIN_PATH") or "herdr", "notification", "show", f"{TITLE} toggle refused",
                   "--body", reason], timeout=TIMEOUT)
    return 1, f"{TITLE} toggle refused: {reason}"


def main():
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR") or str(Path.home() / ".local/state/firstpane")
    code, message = toggle(os.environ, LiveTransport(os.environ.get("HERDR_SOCKET_PATH")), state_dir)
    print(message, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
