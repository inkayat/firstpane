"""Subprocess and Herdr socket boundary. No shell, no implicit repo cwd."""

import json
import os
import socket
import subprocess
import uuid


class CmdResult:
    def __init__(self, code, stdout="", stderr="", timed_out=False):
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


class LiveTransport:
    def __init__(self, socket_path):
        self.socket_path = socket_path

    def socket_call(self, method, params, timeout):
        if not self.socket_path:
            raise OSError("Herdr socket unavailable")
        request_id = "firstpane-" + uuid.uuid4().hex
        payload = json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(self.socket_path)
            client.sendall(payload.encode())
            data = b""
            while b"\n" not in data:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 8_000_000:
                    raise ValueError("Herdr response too large")
        line = data.split(b"\n", 1)[0]
        if not line:
            raise OSError("Herdr socket closed")
        return json.loads(line.decode())

    def run(self, argv, stdin=None, env=None, timeout=None, cwd=None, detach=False):
        """Run argv without a shell. `detach` gives the child no controlling terminal and no
        pane stdin, so a credential or hook prompt fails instead of reading the pane."""
        try:
            completed = subprocess.run(
                list(argv),
                input=stdin,
                stdin=subprocess.DEVNULL if detach and stdin is None else None,
                text=True,
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
                shell=False,
                start_new_session=detach,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            return CmdResult(124, stdout, stderr, timed_out=True)
        except OSError as exc:
            return CmdResult(127, "", str(exc))
        return CmdResult(completed.returncode, completed.stdout, completed.stderr)

    def open_editor(self, path, terminal):
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        completed = subprocess.run([editor, path], check=False)
        return completed.returncode

    def browser_open(self, url):
        opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
        return self.run([opener, url], timeout=10, cwd=None)
