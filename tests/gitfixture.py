"""Real, isolated git repositories for tests. Never touches a user repository or config."""

import os
import subprocess
import tempfile
from pathlib import Path


def git_env(home):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(Path(home) / "gitconfig"),
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return env


class GitFixture:
    """A temporary primary repo with a local bare remote and helpers to shape its state."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fp-git-")
        self.base = Path(os.path.realpath(self.tmp.name))
        self.home = self.base / "home"
        self.home.mkdir()
        (self.home / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
        self.env = git_env(self.home)
        self.remote = self.base / "remote.git"
        self.primary = self.base / "primary"
        self.git(self.base, "init", "-q", "--bare", str(self.remote))
        self.git(self.base, "init", "-q", str(self.primary))
        self.write("README.md", "one\ntwo\nthree\n")
        self.git(self.primary, "add", "README.md")
        self.git(self.primary, "commit", "-q", "-m", "initial")
        self.git(self.primary, "remote", "add", "origin", str(self.remote))
        self.git(self.primary, "push", "-q", "-u", "origin", "main")

    def cleanup(self):
        self.tmp.cleanup()

    def git(self, cwd, *args, check=True):
        completed = subprocess.run(
            ["git", *args], cwd=str(cwd), env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        if check and completed.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
        return completed.stdout.strip()

    def write(self, rel, text, root=None):
        path = Path(root or self.primary) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def worktree(self, name, branch):
        path = self.base / name
        self.git(self.primary, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def head(self, root=None):
        return self.git(root or self.primary, "rev-parse", "HEAD")

    def clone(self, name):
        path = self.base / name
        self.git(self.base, "clone", "-q", str(self.remote), str(path))
        return path
