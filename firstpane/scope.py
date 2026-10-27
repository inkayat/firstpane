"""Which project paths belong in the configured FirstMate view."""

from pathlib import Path


def scope_root(config):
    if getattr(config, "project_root", None):
        return _resolve(config.project_root)
    home = getattr(config, "firstmate_home", None)
    if home:
        return _resolve(str(Path(home) / "projects"))
    return None


def path_in_scope(path, root, allowlist=()):
    allowed = [_resolve(item) for item in (allowlist or ())]
    allowed = [item for item in allowed if item]
    if not allowed and not root:
        return True
    resolved = _resolve(path)
    if not resolved:
        return False
    if allowed:
        return any(_within(resolved, item) for item in allowed)
    root_resolved = _resolve(root)
    if not root_resolved or resolved == root_resolved:
        return False
    return _within(resolved, root_resolved)


def _within(path, base):
    try:
        Path(path).relative_to(base)
    except ValueError:
        return False
    return True


def _resolve(path):
    if not path or not isinstance(path, str):
        return None
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return None
