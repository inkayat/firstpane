"""Browser contexts: who a browser belongs to and which address it reopens at.

The Browser owner runs against a stand-in child (a sleeping process in its own session) and a
fake capture, so only the context and address rules are exercised here; the tab, the embed
protocol and the child lifecycle are covered in test_browser.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from firstpane.browser import Browser


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "tb"
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "terminal-browser").write_text("#!/bin/sh\n")
        (root / "bin" / "terminal-browser").chmod(0o755)
        self.browser = Browser(str(root), Path(self.tmp.name) / "state", {"LANG": "en_US.UTF-8"})

    def alpha(self, source="/src/wt-a"):
        return self.browser.context(("fm", "alpha-task"), "alpha-task", source)

    def show(self, context, page):
        """`context`'s browser is running and its page is at `page`."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(child.kill)
        self.browser.child, self.browser.shown, self.browser.child_env_used = child, context, {"HOME": "/x"}
        self.browser.capture_fn = lambda *args, **kwargs: {"url": page, "title": "T", "selection": ""}

    def test_a_context_is_its_agent_and_source_together(self):
        # Break: two agents of one project share a context (profile, address), an agent that
        # moved to another worktree keeps the old worktree's, or a renamed label forks it.
        ids = {
            self.alpha()["id"],
            self.browser.context(("fm", "beta-task"), "beta-task", "/src/wt-a")["id"],
            self.alpha("/src/wt-c")["id"],
            self.alpha(None)["id"],
        }
        self.assertEqual(len(ids), 4)
        self.assertEqual(self.browser.context(("fm", "alpha-task"), "renamed", "/src/wt-a")["id"], self.alpha()["id"])

    def test_only_a_well_formed_web_or_file_address_is_remembered(self):
        # Break: a blank, browser-internal or malformed address the browser reports replaces
        # the remembered one and is reopened later.
        self.show(self.alpha(), "http://127.0.0.1:8001/")
        self.browser.close()
        for page, want in (
            ("http://localhost:5173/x?y=1", "http://localhost:5173/x?y=1"),
            ("file:///tmp/page.html", "file:///tmp/page.html"),
            ("https://example.test/docs", "https://example.test/docs"),
            ("about:blank", None),
            ("terminal-browser://start", None),
            ("javascript:alert(1)", None),
            ("http://", None),
            ("http://localhost:99999/", None),
            ("http://exa mple.test/", None),
            ("ftp://host/x", None),
            ("file://", None),
        ):
            with self.subTest(page=page):
                before = self.alpha()["url"]
                self.show(self.alpha(), page)
                self.browser.close()
                self.assertEqual(self.alpha()["url"], want or before)

    def test_an_unreadable_page_keeps_the_remembered_address(self):
        # Break: a browser whose page cannot be read wipes the address it had.
        self.show(self.alpha(), "http://127.0.0.1:8001/")
        self.browser.close()
        self.show(self.alpha(), "unused")

        def unreadable(*args, **kwargs):
            raise OSError("no registry")

        self.browser.capture_fn = unreadable
        self.assertIn("closed", self.browser.close())
        self.assertEqual(self.alpha()["url"], "http://127.0.0.1:8001/")

    def test_capture_refuses_another_contexts_browser(self):
        # Break: with beta selected, `a` reads alpha's page into beta's message.
        self.show(self.alpha(), "http://127.0.0.1:8001/")
        beta = self.browser.context(("fm", "beta-task"), "beta-task", "/src/wt-b")
        with self.assertRaises(Exception) as caught:
            self.browser.capture(beta)
        self.assertIn("alpha-task", str(caught.exception))
        self.assertEqual(self.browser.capture(self.alpha())["url"], "http://127.0.0.1:8001/")

    def test_an_unconfigured_or_missing_install_starts_nothing(self):
        # Break: a missing install is looked up on PATH or started anyway.
        self.assertIn("terminal_browser_root", Browser(None, Path(self.tmp.name) / "s").open(self.alpha()))
        missing = Browser(str(Path(self.tmp.name) / "nowhere"), Path(self.tmp.name) / "s")
        self.assertIn("unavailable", missing.open(self.alpha()))
        self.assertIsNone(missing.child)


if __name__ == "__main__":
    unittest.main()
