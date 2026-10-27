"""Frame transport negotiation: asked of the pane's terminal, answered through Firstpane's input."""

import base64
import os
import re
import unittest

import _posixshmem

from firstpane.frames import FILE_QUERY_ID, SHM_QUERY_ID, TIMEOUT, FrameMedium
from tests.support import Clock


def names(queries):
    """{medium: object name} named by the a=q queries."""
    return {m: base64.b64decode(p).decode() for m, p in re.findall(r"\x1b_G[^;]*t=([sf])[^;]*;([^\x1b]+)\x1b\\", queries)}


def exists(medium, name):
    if medium == "f":
        return os.path.exists(name)
    try:
        os.close(_posixshmem.shm_open(name, os.O_RDONLY, mode=0o600))
        return True
    except FileNotFoundError:
        return False


class FrameMediumTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.medium = FrameMedium(self.clock)
        self.addCleanup(self.medium.cleanup)

    def test_queries_name_a_readable_shared_memory_object_and_file_like_terminal_browser(self):
        # Break: the query names an object the terminal cannot read (so a terminal that takes
        # shared memory is answered "no"), or uses another format than terminal-browser's probe.
        queries = self.medium.queries()
        objects = names(queries)
        self.assertEqual(set(objects), {"s", "f"})
        self.assertIn(f"i={SHM_QUERY_ID},s=1,v=1,a=q,t=s,f=32;", queries)
        self.assertIn(f"i={FILE_QUERY_ID},s=1,v=1,a=q,t=f,f=32;", queries)
        self.assertTrue(objects["s"].startswith("/"))
        for medium, name in objects.items():
            self.assertTrue(exists(medium, name), medium)
        with open(objects["f"], "rb") as handle:
            self.assertEqual(handle.read(), bytes([0, 0, 0, 255]))

    def test_shared_memory_wins_and_the_probe_objects_are_removed(self):
        # Break: a later file answer overrides shared memory, or probe objects leak.
        objects = names(self.medium.queries())
        self.medium.answer(SHM_QUERY_ID, "OK")
        self.assertEqual(self.medium.result, "shm")
        self.medium.answer(FILE_QUERY_ID, "OK")
        self.assertEqual(self.medium.result, "shm")
        for medium, name in objects.items():
            self.assertFalse(exists(medium, name), medium)

    def test_a_file_is_used_only_when_shared_memory_is_refused(self):
        # Break: the slow inline path is chosen although the terminal takes files, or a file is
        # chosen before the shared-memory answer arrived.
        self.medium.queries()
        self.medium.answer(FILE_QUERY_ID, "OK")
        self.assertIsNone(self.medium.result)
        self.medium.answer(SHM_QUERY_ID, "EINVAL:not supported")
        self.assertEqual(self.medium.result, "file")

    def test_refusals_or_silence_mean_inline(self):
        # Break: a terminal that refuses both (or never answers) leaves the browser waiting.
        self.medium.queries()
        self.medium.answer(SHM_QUERY_ID, "EINVAL")
        self.medium.answer(FILE_QUERY_ID, "ENOENT")
        self.assertEqual(self.medium.result, "inline")
        silent = FrameMedium(self.clock)
        self.addCleanup(silent.cleanup)
        silent.queries()
        self.assertIsNone(silent.poll())
        self.clock.t += TIMEOUT
        self.assertEqual(silent.poll(), "inline")
        self.assertIn("slow", silent.label)

    def test_answers_to_other_images_are_ignored(self):
        # Break: an unrelated kitty answer (another image id) settles the transport.
        self.medium.queries()
        self.medium.answer(31, "OK")
        self.assertIsNone(self.medium.result)


if __name__ == "__main__":
    unittest.main()
