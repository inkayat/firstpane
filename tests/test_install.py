"""Execute the Firstpane installer against synthetic homes and fake Herdr.

These tests run bin/install-firstpane. They do not link a live plugin, talk to
the default Herdr session, or download the real terminal-browser archive.
"""

import hashlib
import io
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bin" / "install-firstpane"
REMOTE = "https://github.com/example/firstpane.git"
APPLY_SHA = "20dc63714896172aaf6b7579ac8a70e6fd5ca1be02bb79e29126c39fa2f01e13"
SUBS_SHA = "da8adbd143eb501e5a5e5488cbc9d1b30cbea6465ae582ad9441cd5ee0dea795"


def landed_file(path: str) -> bytes:
    """The pinned artifact as it sits in this checkout.

    A shallow CI clone has these files and not the historical commit object,
    so identity is the pinned size and sha256, not ``git show <commit>``.
    """
    expected = {
        "docs/apply-terminal-browser-0.13.4-context.py": (APPLY_SHA, 1062),
        "docs/terminal-browser-0.13.4-context-substitutions.json": (SUBS_SHA, 2415),
    }[path]
    data = (ROOT / path).read_bytes()
    digest, size = expected
    if sha256(data) != digest or len(data) != size:
        raise AssertionError(f"{path} is not the checksum-pinned artifact")
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.xdg_data = self.home / ".local" / "share"
        self.xdg_config = self.home / ".config"
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.state = self.root / "herdr-state.json"
        self.log = self.root / "herdr.log"
        self.stamp = self.root / "stamp"
        self.write_fakes()
        self.repo = self.root / "repo"
        self.make_repo()
        self.payload_before = b"original-sender"
        self.payload_after = b"patched-sender"
        self.archive = self.root / "browser.tar.gz"
        self.apply_script = self.root / "apply-terminal-browser-0.13.4-context.py"
        self.apply_script.write_bytes(
            landed_file("docs/apply-terminal-browser-0.13.4-context.py")
        )
        self.real_subs = self.root / "real-substitutions.json"
        self.real_subs.write_bytes(
            landed_file("docs/terminal-browser-0.13.4-context-substitutions.json")
        )
        self.synthetic_subs = self.root / "synthetic-substitutions.json"
        self.write_good_archives()
        self.pins = self.root / "pins.json"
        self.write_pins(self.good_assets())
        self.fm_home = self.root / "fm-home"
        self.fm_root = self.root / "fm-root"
        self.fm_home.mkdir()
        (self.fm_root / "bin").mkdir(parents=True)
        (self.fm_root / "bin" / "fm-inbox.sh").write_text("#!/bin/sh\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write_fakes(self):
        write_executable(
            self.bindir / "herdr",
            """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
log = Path(os.environ["HERDR_FAKE_LOG"])
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a") as handle:
    handle.write(json.dumps(argv) + "\\n")
if any(arg in {"server", "session", "pane"} for arg in argv):
    sys.stderr.write("forbidden herdr command\\n")
    raise SystemExit(9)
if argv == ["--version"]:
    print(os.environ.get("HERDR_FAKE_VERSION", "herdr 0.9.5"))
    raise SystemExit(0)
state_path = Path(os.environ["HERDR_FAKE_STATE"])
state = json.loads(state_path.read_text()) if state_path.is_file() else {"plugins": []}

def save():
    state_path.write_text(json.dumps(state))

if argv[:2] == ["plugin", "list"] and "--json" in argv:
    print(json.dumps({"id": "cli:plugin", "result": {"type": "plugin_list", "plugins": state["plugins"]}}))
    raise SystemExit(0)
if argv[:2] == ["plugin", "link"] and len(argv) == 3:
    state["plugins"] = [item for item in state["plugins"] if item.get("plugin_id") != "firstpane"]
    state["plugins"].append({
        "plugin_id": "firstpane",
        "name": "Firstpane",
        "plugin_root": argv[2],
        "enabled": True,
        "source": {"kind": "local"},
    })
    save()
    print("linked")
    raise SystemExit(0)
if argv[:2] == ["plugin", "enable"] and argv[2:] == ["firstpane"]:
    for item in state["plugins"]:
        if item.get("plugin_id") == "firstpane":
            item["enabled"] = True
    save()
    print("enabled")
    raise SystemExit(0)
if argv[:2] == ["plugin", "config-dir"]:
    override = os.environ.get("HERDR_FAKE_CONFIG_DIR")
    if override:
        print(override)
        raise SystemExit(0)
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "herdr" / "plugins" / "config" / "firstpane"
    path.mkdir(parents=True, exist_ok=True)
    print(path.resolve())
    raise SystemExit(0)
sys.stderr.write("unexpected herdr args: " + " ".join(argv) + "\\n")
raise SystemExit(2)
""",
        )
        for name in ("sudo", "open", "osascript", "terminal-browser"):
            write_executable(
                self.bindir / name,
                f"""#!/bin/sh
echo {name} >> "{self.stamp}"
exit 0
""",
            )

    def make_repo(self):
        self.repo.mkdir()
        (self.repo / "herdr-plugin.toml").write_text(
            'id = "firstpane"\nname = "Firstpane"\nversion = "0.1.0"\n'
        )
        (self.repo / "bin").mkdir()
        shutil.copy(INSTALLER, self.repo / "bin" / "install-firstpane")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True, env=env)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True, env=env)
        subprocess.run(
            ["git", "commit", "-m", "fixture"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            env=env,
        )

    def write_good_archives(self):
        script = b"#!/bin/sh\necho terminal-browser v0.13.4\n"
        self.make_tar(
            self.archive,
            [
                ("terminal-browser/VERSION", b"0.13.4\n", 0o644),
                ("terminal-browser/bin/terminal-browser", script, 0o755),
                ("terminal-browser/payload.txt", self.payload_before, 0o644),
                ("terminal-browser/rel", "payload.txt", None),
            ],
        )
    def make_tar(self, path: Path, members: list) -> None:
        with tarfile.open(path, "w:gz") as tar:
            for name, payload, mode in members:
                info = tarfile.TarInfo(name)
                if mode is None:
                    info.type = tarfile.SYMTYPE
                    info.linkname = payload
                else:
                    data = payload if isinstance(payload, bytes) else payload.encode()
                    info.size = len(data)
                    info.mode = mode
                    tar.addfile(info, io.BytesIO(data))
                    continue
                tar.addfile(info)

    def good_assets(self) -> dict:
        digest = sha256(self.archive.read_bytes())
        size = self.archive.stat().st_size
        url = self.archive.as_uri()
        return {
            key: {"url": url, "sha256": digest, "size": size}
            for key in ("darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64")
        }

    def write_pins(
        self,
        assets: dict,
        files=None,
        substitutions_path: Path | None = None,
        substitutions_sha: str | None = None,
        apply_sha: str | None = None,
        **extra,
    ) -> None:
        subs = substitutions_path or self.synthetic_subs
        if substitutions_path is None:
            subs.write_text(
                json.dumps(
                    {
                        "version": "0.13.4",
                        "files": {
                            "payload.txt": [
                                {"old": "original-sender", "new": "patched-sender"}
                            ]
                        },
                    }
                )
            )
        subs_bytes = subs.read_bytes()
        apply_bytes = self.apply_script.read_bytes()
        document = {
            "firstpane_remote": REMOTE,
            "allow_local_source": True,
            "herdr_min": "0.9.0",
            "python_min": "3.11",
            "terminal_browser": {
                "version": "0.13.4",
                "max_uncompressed_bytes": extra.pop("max_uncompressed_bytes", 1_000_000),
                "assets": assets,
            },
            "sender_patch": {
                "status": "bundled",
                "identity": "fixture-context-patch",
                "applies_to": "0.13.4",
                "substitutions": {
                    "sha256": substitutions_sha or sha256(subs_bytes),
                    "size": len(subs_bytes),
                },
                "apply": {
                    "sha256": apply_sha or sha256(apply_bytes),
                    "size": len(apply_bytes),
                },
                "files": files
                or [
                    {
                        "path": "payload.txt",
                        "pre_sha256": sha256(self.payload_before),
                        "pre_size": len(self.payload_before),
                        "post_sha256": sha256(self.payload_after),
                        "post_size": len(self.payload_after),
                    }
                ],
            },
        }
        document.update(extra)
        docs = self.repo / "docs"
        docs.mkdir(exist_ok=True)
        shutil.copy(subs, docs / "terminal-browser-0.13.4-context-substitutions.json")
        shutil.copy(self.apply_script, docs / "apply-terminal-browser-0.13.4-context.py")
        subprocess.run(["git", "-C", str(self.repo), "add", "docs"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=test",
             "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "fixture patch"],
            check=True, capture_output=True,
        )
        self.pins.write_text(json.dumps(document))

    def env(self, **extra):
        base = os.environ.copy()
        for key in (
            "FM_HOME",
            "FIRSTMATE_ROOT",
            "FM_CONFIG_ENV",
            "FIRSTPANE_ASSUME_PYTHON",
            "FIRSTPANE_ASSUME_PLATFORM",
            "FIRSTPANE_INSTALL_FAIL_AFTER",
            "FIRSTPANE_CHECK_LINUX_LIBS",
            "HERDR_FAKE_VERSION",
            "HERDR_FAKE_CONFIG_DIR",
        ):
            base.pop(key, None)
        base.update(
            {
                "HOME": str(self.home),
                "XDG_DATA_HOME": str(self.xdg_data),
                "XDG_CONFIG_HOME": str(self.xdg_config),
                "PATH": f"{self.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
                "FIRSTPANE_INSTALL_PINS": str(self.pins),
                "HERDR_FAKE_STATE": str(self.state),
                "HERDR_FAKE_LOG": str(self.log),
            }
        )
        base.update(extra)
        return base

    def run_install(self, *args, env=None, stdin="y\n", script=None):
        return subprocess.run(
            [sys.executable, str(script or (self.repo / "bin" / "install-firstpane")), *args],
            input=stdin,
            text=True,
            capture_output=True,
            env=env or self.env(),
            check=False,
        )

    def config_path(self):
        return self.xdg_config / "herdr" / "plugins" / "config" / "firstpane" / "config.json"

    def install_root(self):
        return self.xdg_data / "firstpane"

    def herdr_calls(self):
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def test_fresh_install_patches_browser_and_links_once(self):
        result = self.run_install("--yes", "--without-firstmate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("herdr plugin pane open --plugin firstpane --entrypoint tree", result.stdout)
        self.assertNotIn("server", {arg for call in self.herdr_calls() for arg in call})
        config = json.loads(self.config_path().read_text())
        browser = Path(config["terminal_browser_root"])
        self.assertEqual((browser / "payload.txt").read_bytes(), self.payload_after)
        self.assertEqual((browser / "VERSION").read_text().strip(), "0.13.4")
        self.assertTrue((browser / "rel").is_symlink())
        linked = [call for call in self.herdr_calls() if call[:2] == ["plugin", "link"]]
        self.assertEqual(len(linked), 1)
        self.assertIn("home with spaces", linked[0][2])
        self.assertFalse(self.stamp.exists())

    def test_repeat_install_does_not_relink_or_redownload(self):
        first = self.run_install("--yes", "--without-firstmate")
        self.assertEqual(first.returncode, 0, first.stderr)
        broken = json.loads(self.pins.read_text())
        broken["terminal_browser"]["assets"]["darwin-arm64"]["url"] = "http://127.0.0.1:9/nope"
        broken["terminal_browser"]["assets"]["linux-x64"]["url"] = "http://127.0.0.1:9/nope"
        self.pins.write_text(json.dumps(broken))
        before = (self.install_root() / "src" / "herdr-plugin.toml").read_text()
        second = self.run_install("--yes", "--without-firstmate")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.install_root() / "src" / "herdr-plugin.toml").read_text(), before)
        links = [call for call in self.herdr_calls() if call[:2] == ["plugin", "link"]]
        self.assertEqual(len(links), 1)
        self.assertFalse((self.install_root() / "src.previous").exists())

    def test_check_makes_no_changes_and_does_not_download(self):
        broken = json.loads(self.pins.read_text())
        for asset in broken["terminal_browser"]["assets"].values():
            asset["url"] = "http://127.0.0.1:9/closed"
        self.pins.write_text(json.dumps(broken))
        marker = self.home / "untouched"
        marker.write_text("keep")
        result = self.run_install("--check", "--without-firstmate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check only", result.stdout)
        self.assertEqual(marker.read_text(), "keep")
        self.assertFalse(self.install_root().exists())
        self.assertFalse(self.config_path().exists())
        self.assertFalse(any(call[:2] == ["plugin", "link"] for call in self.herdr_calls()))

    def test_decline_changes_nothing(self):
        result = self.run_install("--without-firstmate", stdin="n\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing was changed", result.stderr)
        self.assertFalse(self.install_root().exists())

    def test_missing_and_old_prerequisites(self):
        missing_git = self.run_install(
            "--check",
            env=self.env(PATH=str(self.bindir)),
        )
        self.assertNotEqual(missing_git.returncode, 0)
        self.assertIn("does not install it", missing_git.stderr)
        no_herdr = self.env()
        no_herdr["PATH"] = "/usr/bin:/bin"
        missing_herdr = self.run_install("--check", env=no_herdr)
        self.assertNotEqual(missing_herdr.returncode, 0)
        self.assertIn("does not install or upgrade Herdr", missing_herdr.stderr)
        old = self.run_install("--check", env=self.env(HERDR_FAKE_VERSION="herdr 0.8.9"))
        self.assertNotEqual(old.returncode, 0)
        self.assertIn("does not upgrade Herdr", old.stderr)
        old_python = self.run_install("--check", env=self.env(FIRSTPANE_ASSUME_PYTHON="3.10.0"))
        self.assertNotEqual(old_python.returncode, 0)
        self.assertIn("does not install or upgrade Python", old_python.stderr)

    def test_supported_and_unsupported_platform(self):
        supported = self.run_install(
            "--yes",
            "--without-firstmate",
            env=self.env(FIRSTPANE_ASSUME_PLATFORM="linux-x64"),
        )
        self.assertEqual(supported.returncode, 0, supported.stderr)
        self.assertIn("linux-x64", supported.stdout)
        self.assertNotIn("missing Linux libraries", supported.stderr)
        unsupported = self.run_install(
            "--check",
            env=self.env(FIRSTPANE_ASSUME_PLATFORM="linux-ppc64"),
        )
        self.assertNotEqual(unsupported.returncode, 0)
        self.assertIn("Refusing to download", unsupported.stderr)

    def test_checksum_mismatch_leaves_no_active_tree(self):
        assets = self.good_assets()
        for asset in assets.values():
            asset["sha256"] = "0" * 64
        self.write_pins(assets)
        result = self.run_install("--yes", "--without-firstmate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr)
        self.assertFalse((self.install_root() / "src").exists())
        self.assertFalse((self.install_root() / "terminal-browser-0.13.4").exists())
        self.assertFalse(self.config_path().exists())

    def test_patch_mismatch_does_not_activate(self):
        self.write_pins(
            self.good_assets(),
            files=[
                {
                    "path": "payload.txt",
                    "pre_sha256": "f" * 64,
                    "post_sha256": sha256(self.payload_after),
                }
            ],
        )
        result = self.run_install("--yes", "--without-firstmate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not apply", result.stderr)
        self.assertFalse((self.install_root() / "terminal-browser-0.13.4").exists())
        self.assertFalse(self.config_path().exists())

    def test_malicious_archive_paths_and_symlinks_are_rejected(self):
        outside = self.root / "outside"
        cases = [
            [("../../outside", b"pwned", 0o644)],
            [("/tmp/firstpane-absolute-pwned", b"pwned", 0o644)],
            [
                ("escape", str(outside), None),
                ("escape/pwned", b"pwned", 0o644),
            ],
        ]
        for members in cases:
            with self.subTest(members=members[0][0]):
                archive = self.root / "bad.tar.gz"
                self.make_tar(archive, members)
                assets = {
                    key: {
                        "url": archive.as_uri(),
                        "sha256": sha256(archive.read_bytes()),
                        "size": archive.stat().st_size,
                    }
                    for key in ("darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64")
                }
                self.write_pins(assets)
                result = self.run_install("--yes", "--without-firstmate")
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("refusing", result.stderr)
                self.assertFalse((self.install_root() / "terminal-browser-0.13.4").exists())
                self.assertFalse(outside.exists())
                self.assertFalse(Path("/tmp/firstpane-absolute-pwned").exists())

    def test_config_keys_drafts_and_valid_paths_are_preserved(self):
        config_dir = self.config_path().parent
        config_dir.mkdir(parents=True)
        original_browser = self.root / "existing-browser"
        (original_browser / "bin").mkdir(parents=True)
        write_executable(original_browser / "bin" / "terminal-browser", "#!/bin/sh\n")
        (original_browser / "VERSION").write_text("0.13.4\n")
        (config_dir / "draft.txt").write_text("keep draft")
        state = self.home / "state-data"
        state.mkdir()
        (state / "draft.json").write_text('{"text":"keep"}')
        other_home = self.root / "other-home"
        other_root = self.root / "other-root"
        other_home.mkdir()
        (other_root / "bin").mkdir(parents=True)
        (other_root / "bin" / "fm-inbox.sh").write_text("#!/bin/sh\n")
        self.config_path().write_text(
            json.dumps(
                {
                    "editor": ["vi"],
                    "custom": "keep",
                    "firstmate_home": str(self.fm_home),
                    "firstmate_root": str(self.fm_root),
                    "terminal_browser_root": str(original_browser),
                }
            )
        )
        result = self.run_install(
            "--yes",
            "--firstmate-home",
            str(other_home),
            "--firstmate-root",
            str(other_root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.config_path().read_text())
        self.assertEqual(saved["editor"], ["vi"])
        self.assertEqual(saved["custom"], "keep")
        self.assertEqual(saved["firstmate_home"], str(self.fm_home))
        self.assertEqual(saved["firstmate_root"], str(self.fm_root))
        self.assertEqual(saved["terminal_browser_root"], str(original_browser))
        self.assertEqual((config_dir / "draft.txt").read_text(), "keep draft")
        self.assertEqual((state / "draft.json").read_text(), '{"text":"keep"}')
        self.assertFalse((self.install_root() / "terminal-browser-0.13.4").exists())

    def test_interrupted_install_can_be_rerun(self):
        failed = self.run_install(
            "--yes",
            "--without-firstmate",
            env=self.env(FIRSTPANE_INSTALL_FAIL_AFTER="after-extract"),
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Recovery", failed.stderr)
        self.assertFalse((self.install_root() / "src").exists())
        self.assertFalse(self.config_path().exists())
        retried = self.run_install("--yes", "--without-firstmate")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertTrue((self.install_root() / "src" / "herdr-plugin.toml").is_file())

    def test_failure_after_activate_keeps_previous_and_recovers(self):
        failed = self.run_install(
            "--yes",
            "--without-firstmate",
            env=self.env(FIRSTPANE_INSTALL_FAIL_AFTER="after-activate"),
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Recovery", failed.stderr)
        self.assertTrue((self.install_root() / "src").is_dir())
        self.assertFalse(self.config_path().exists())
        self.assertEqual(
            [call for call in self.herdr_calls() if call[:2] == ["plugin", "link"]],
            [],
        )
        retried = self.run_install("--yes", "--without-firstmate")
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertTrue(self.config_path().is_file())

    def test_no_sudo_restart_message_or_unexpected_network(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://example.com/evil")
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            assets = self.good_assets()
            for asset in assets.values():
                asset["url"] = f"http://127.0.0.1:{port}/browser.tar.gz"
                asset["size"] = 1
                asset["sha256"] = "a" * 64
            self.write_pins(assets)
            result = self.run_install("--yes", "--without-firstmate")
        finally:
            server.shutdown()
            server.server_close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing", result.stderr)
        self.assertEqual(seen, ["/browser.tar.gz"])
        self.assertFalse(self.stamp.exists())
        joined = " ".join(" ".join(call) for call in self.herdr_calls())
        for word in ("server", "session", "pane"):
            self.assertNotIn(word, joined)

    def test_launcher_env_is_data_not_shell_and_skip_is_standalone(self):
        env_file = self.home / ".config" / "firstmate-config" / "env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(
            f"EVIL=$(touch {self.stamp})\n"
            f"FM_HOME=$(touch {self.stamp})\n"
            f"FIRSTMATE_ROOT='{self.fm_root}'\n"
        )
        refused = self.run_install("--yes")
        self.assertFalse(self.stamp.exists())
        self.assertNotIn("firstmate_home", self._config_or_empty())
        env_file.write_text(
            f"FM_HOME='{self.fm_home}'\nFIRSTMATE_ROOT='{self.fm_root}'\n"
        )
        found = self.run_install("--yes")
        self.assertEqual(found.returncode, 0, found.stderr)
        config = json.loads(self.config_path().read_text())
        self.assertEqual(config["firstmate_home"], str(self.fm_home.resolve()))
        self.assertEqual(config["firstmate_root"], str(self.fm_root.resolve()))

    def test_ambiguous_firstmate_prompts_and_does_not_guess(self):
        decoy = self.home / "Developer" / "tools" / "firstmate"
        decoy.mkdir(parents=True)
        skipped = self.run_install(stdin="skip\ny\n")
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        config = json.loads(self.config_path().read_text())
        self.assertNotIn("firstmate_home", config)
        self.assertNotIn(str(decoy), json.dumps(config))

    def test_symlink_and_foreign_install_roots_are_refused(self):
        self.xdg_data.mkdir(parents=True)
        target = self.root / "foreign"
        target.mkdir()
        (target / "keep").write_text("mine")
        (self.xdg_data / "firstpane").symlink_to(target)
        refused = self.run_install("--yes", "--without-firstmate")
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((target / "keep").read_text(), "mine")
        self.assertFalse((target / "install-owner.json").exists())
        (self.xdg_data / "firstpane").unlink()
        foreign = self.xdg_data / "firstpane"
        foreign.mkdir()
        (foreign / "keep").write_text("mine")
        again = self.run_install("--yes", "--without-firstmate")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("foreign", again.stderr)
        self.assertEqual((foreign / "keep").read_text(), "mine")

    def test_foreign_herdr_link_is_not_replaced(self):
        self.state.write_text(
            json.dumps(
                {
                    "plugins": [
                        {
                            "plugin_id": "firstpane",
                            "plugin_root": str(self.root / "other-plugin"),
                            "enabled": True,
                            "source": {"kind": "local"},
                        }
                    ]
                }
            )
        )
        result = self.run_install("--yes", "--without-firstmate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("will not replace", result.stderr)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertFalse(self.install_root().exists())
        self.assertFalse(self.config_path().exists())
        self.assertFalse(any(call[:2] == ["plugin", "link"] for call in self.herdr_calls()))

    def test_linux_library_gap_does_not_sudo(self):
        write_executable(self.bindir / "ldd", f"""#!/bin/sh
echo "libgtk-3.so.0 => not found" >> "{self.stamp}"
echo "libgtk-3.so.0 => not found"
exit 1
""")
        result = self.run_install(
            "--yes",
            "--without-firstmate",
            env=self.env(FIRSTPANE_CHECK_LINUX_LIBS="1"),
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("does not run sudo", result.stderr)
        self.assertIn("Arch/Omarchy", result.stderr)
        self.assertNotIn("sudo\n", self.stamp.read_text() if self.stamp.exists() else "")
        self.assertTrue(self.config_path().is_file())

    def test_landed_patch_identity_is_applied_by_the_committed_script(self):
        substitutions = json.loads(self.real_subs.read_text())
        originals = {}
        for name, pairs in substitutions["files"].items():
            chunks = []
            for index, pair in enumerate(pairs):
                chunks.append(f"\nMARK-{name}-{index}-START\n{pair['old']}\nMARK-{name}-{index}-END\n")
            originals[name] = "".join(chunks).encode()
        members = [
            ("terminal-browser/VERSION", b"0.13.4\n", 0o644),
            (
                "terminal-browser/bin/terminal-browser",
                b"#!/bin/sh\necho terminal-browser v0.13.4\n",
                0o755,
            ),
        ]
        for name, data in originals.items():
            members.append((f"terminal-browser/{name}", data, 0o644))
        archive = self.root / "identity.tar.gz"
        self.make_tar(archive, members)
        tool = self.root / "landed-tool"
        tool.mkdir()
        script = tool / "apply-terminal-browser-0.13.4-context.py"
        subs = tool / "terminal-browser-0.13.4-context-substitutions.json"
        script.write_bytes(self.apply_script.read_bytes())
        subs.write_bytes(self.real_subs.read_bytes())
        oracle = self.root / "oracle"
        for name, data in originals.items():
            path = oracle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        subprocess.run([sys.executable, str(script), str(oracle)], check=True)
        files = []
        for name, data in originals.items():
            patched = (oracle / name).read_bytes()
            self.assertNotEqual(patched, data)
            files.append(
                {
                    "path": name,
                    "pre_sha256": sha256(data),
                    "pre_size": len(data),
                    "post_sha256": sha256(patched),
                    "post_size": len(patched),
                }
            )
        assets = {
            key: {
                "url": archive.as_uri(),
                "sha256": sha256(archive.read_bytes()),
                "size": archive.stat().st_size,
            }
            for key in ("darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64")
        }
        self.apply_script = script
        self.write_pins(assets, files=files, substitutions_path=subs)
        result = self.run_install("--yes", "--without-firstmate")
        self.assertEqual(result.returncode, 0, result.stderr)
        browser = Path(json.loads(self.config_path().read_text())["terminal_browser_root"])
        for name in originals:
            self.assertEqual((browser / name).read_bytes(), (oracle / name).read_bytes())

    def test_official_remote_without_git_suffix_is_accepted_and_other_remotes_are_not(self):
        # Break: the selected source is rejected, or a foreign source is accepted.
        (self.repo / "install").mkdir()
        shutil.copy(ROOT / "install" / "pins.json", self.repo / "install" / "pins.json")
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", "https://github.com/inkayat/firstpane"],
            check=True, capture_output=True,
        )
        env = self.env()
        env.pop("FIRSTPANE_INSTALL_PINS")
        accepted = self.run_install("--check", "--without-firstmate", env=env)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("check only", accepted.stdout)
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "set-url", "origin", "https://github.com/evil/firstpane.git"],
            check=True, capture_output=True,
        )
        refused = self.run_install("--check", "--without-firstmate", env=env)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("refusing source origin", refused.stderr)
        self.assertFalse(self.install_root().exists())
        self.assertFalse(self.config_path().exists())
        self.assertFalse(any(call[:2] == ["plugin", "link"] for call in self.herdr_calls()))

    def test_unselected_public_target_refuses_before_any_tool_or_file_change(self):
        # Break: an unset destination gets past pins validation and invokes tools.
        installer = runpy.run_path(str(INSTALLER))
        installer["main"].__globals__["REMOTE"] = None
        unset = json.loads((ROOT / "install" / "pins.json").read_text())
        unset["firstpane_remote"] = None
        unset_pins = self.root / "unset-pins.json"
        unset_pins.write_text(json.dumps(unset))
        stderr = io.StringIO()
        env = self.env(FIRSTPANE_INSTALL_PINS=str(unset_pins))
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stderr", stderr):
            result = installer["main"](["--check", "--without-firstmate"])
        self.assertEqual(result, 3, stderr.getvalue())
        self.assertIn("public repository has not been selected", stderr.getvalue())
        self.assertFalse(self.install_root().exists())
        self.assertFalse(self.config_path().exists())
        self.assertEqual(self.herdr_calls(), [])

    def test_bundled_patch_applies_offline_and_rejects_tampered_helpers(self):
        # Break: fetching private historical URLs prevents an offline patch, or
        # unverified bundled code executes before its checksum is checked.
        installer = runpy.run_path(str(INSTALLER))
        docs = self.repo / "docs"
        subs = docs / "terminal-browser-0.13.4-context-substitutions.json"
        patch = json.loads(self.pins.read_text())["sender_patch"]
        installer["apply_sender_patch"].__globals__["repo_root"] = lambda: self.repo
        for tampered in (False, True):
            with self.subTest(tampered=tampered):
                runtime = self.root / f"runtime-{tampered}"
                runtime.mkdir()
                (runtime / "payload.txt").write_bytes(self.payload_before)
                staging = self.root / f"staging-{tampered}"
                staging.mkdir()
                if tampered:
                    substitutions = json.loads(subs.read_text())
                    substitutions["files"]["payload.txt"][0]["new"] = "wrong-sender"
                    subs.write_text(json.dumps(substitutions))
                    with self.assertRaises(installer["InstallError"]):
                        installer["apply_sender_patch"](runtime, staging, patch, 1_000_000)
                    self.assertEqual((runtime / "payload.txt").read_bytes(), self.payload_before)
                else:
                    try:
                        installer["apply_sender_patch"](runtime, staging, patch, 1_000_000)
                    except installer["InstallError"] as exc:
                        self.fail(f"bundled patch must work without a download URL: {exc}")
                    self.assertEqual((runtime / "payload.txt").read_bytes(), b"patched-sender")

    def snapshot(self):
        if not self.home.exists():
            return []
        return sorted(str(path.relative_to(self.home)) for path in self.home.rglob("*"))

    def _config_or_empty(self):
        if not self.config_path().is_file():
            return {}
        return json.loads(self.config_path().read_text())


if __name__ == "__main__":
    unittest.main()
