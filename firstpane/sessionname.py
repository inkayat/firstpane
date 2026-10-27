"""Derive the Herdr session name from the socket path this process was given."""


def session_from_socket_path(path):
    if not path:
        return None
    parts = str(path).rstrip("/").split("/")
    if len(parts) >= 3 and parts[-1] == "herdr.sock" and parts[-3] == "sessions":
        name = parts[-2]
        if name and name not in (".", ".."):
            return name
        return None
    if parts and parts[-1] == "herdr.sock" and "sessions" not in parts:
        return "default"
    return None
