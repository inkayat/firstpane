"""Runs where the placed browser shows (firstpane/placed.py): in the Herdr pane beside Firstpane
or in a Ghostty tab, as `exec <python> -I -B browser_launch.py <spec.json>`.

Reads the spec Firstpane wrote, records this pid (exec keeps it, so it stays the browser's pid),
and replaces itself with `terminal-browser open [<url>]` in the spec's private environment (no
url: terminal-browser's blank page). terminal-browser then draws straight into this terminal.
Standard library only: `-I` keeps the caller's PYTHONPATH and user site out.
"""

import json
import os
import sys

URL_SCHEMES = ("https://", "http://", "file://")


def _fail(message):
    sys.stderr.write(f"firstpane browser: {message}\nPress Enter to close.\n")
    sys.stderr.flush()
    try:
        sys.stdin.readline()
    except (OSError, ValueError):
        pass
    return 1


def main(argv):
    if len(argv) != 2:
        return _fail("usage: browser_launch.py SPEC")
    spec_path = argv[1]
    try:
        with open(spec_path, encoding="utf-8") as handle:
            spec = json.load(handle)
        binary, url, cwd, nonce = spec["bin"], spec["url"], spec["cwd"], spec["nonce"]
        env = spec["env"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _fail(f"unreadable launch spec ({type(error).__name__})")
    if not all(isinstance(value, str) for value in (binary, cwd, nonce)) or not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        return _fail("malformed launch spec")
    if url is not None and not (isinstance(url, str) and url.startswith(URL_SCHEMES)):
        return _fail("refused: only https://, http:// or file:// URLs")
    launched = os.path.join(os.path.dirname(spec_path), "launched.json")
    temp = f"{launched}.{os.getpid()}.tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "nonce": nonce}, handle)
        os.replace(temp, launched)
        os.chdir(cwd)
        os.execve(binary, [binary, "open"] + ([url] if url else []), env)
    except OSError as error:
        return _fail(f"could not start terminal-browser: {error.strerror or error}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
