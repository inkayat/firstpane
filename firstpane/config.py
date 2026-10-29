"""Plugin config. Paths come only from the config file or Herdr's environment."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from firstpane.review import parse_review_config


@dataclass
class Config:
    firstmate_home: str | None = None
    firstmate_root: str | None = None
    editor: list | None = None
    github_hosts: tuple = ("github.com",)
    remotes: dict = field(default_factory=dict)
    project_root: str | None = None
    project_allowlist: tuple = ()
    socket_path: str | None = None
    herdr_pane_id: str | None = None
    terminal_browser_root: str | None = None
    review: object | None = None


def load_config(path, environ=None):
    environ = environ or {}
    data = {}
    config_path = Path(path) if path else None
    if config_path is not None and config_path.is_file():
        loaded = json.loads(config_path.read_text())
        if isinstance(loaded, dict):
            data = loaded
    editor = data.get("editor")
    if isinstance(editor, str) and editor.strip():
        editor = [editor.strip()]
    elif isinstance(editor, list):
        editor = [str(part) for part in editor if str(part).strip()]
    else:
        editor = None
    hosts = data.get("github_hosts")
    if not isinstance(hosts, list) or not hosts:
        hosts = ["github.com"]
    remotes = data.get("remotes") if isinstance(data.get("remotes"), dict) else {}
    allow = data.get("project_allowlist")
    if not isinstance(allow, list):
        allow = []
    home = data.get("firstmate_home") or None
    root = data.get("firstmate_root") or None
    project_root = data.get("project_root") or None
    browser_root = data.get("terminal_browser_root") or None
    return Config(
        firstmate_home=str(home) if home else None,
        firstmate_root=str(root) if root else None,
        editor=editor,
        github_hosts=tuple(str(host) for host in hosts),
        remotes={str(key): str(value) for key, value in remotes.items()},
        project_root=str(project_root) if project_root else None,
        project_allowlist=tuple(str(item) for item in allow if str(item).strip()),
        socket_path=environ.get("HERDR_SOCKET_PATH"),
        herdr_pane_id=environ.get("HERDR_PANE_ID"),
        terminal_browser_root=str(browser_root) if browser_root else None,
        review=parse_review_config(data),
    )
