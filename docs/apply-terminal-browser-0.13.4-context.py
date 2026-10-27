"""Apply the Firstpane-owned 0.13.4 tooltip-note substitutions to a copy.

Does not read or write the installed browser or the configured runtime.
Pass the unpacked 0.13.4 root. Each old string must occur once.
"""

import json
import sys
from pathlib import Path


def apply_root(root, substitutions):
    root = Path(root)
    for name, pairs in substitutions["files"].items():
        path = root / name
        text = path.read_text()
        for pair in pairs:
            old, new = pair["old"], pair["new"]
            count = text.count(old)
            if count != 1:
                raise SystemExit(f"{name}: expected {old!r} once, found {count}")
            text = text.replace(old, new, 1)
        path.write_text(text)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply-terminal-browser-0.13.4-context.py <unpacked-0.13.4-copy>")
    here = Path(__file__).with_name("terminal-browser-0.13.4-context-substitutions.json")
    apply_root(sys.argv[1], json.loads(here.read_text()))


if __name__ == "__main__":
    main()
