import os
import stat
import tempfile
import threading
import unittest
from unittest import mock

from relayctl import cache as rcache
from relayctl import paths


class CacheLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_cache_lives_in_new_cache_dir(self):
        self.assertEqual(rcache.CACHE_DIR, os.path.join(paths.home(), "cache"))
        rcache.write("s", "balance", {"balance": 1})
        path = rcache.cache_path("s")
        self.assertEqual(os.path.dirname(path), paths.cache_dir())
        self.assertFalse(os.path.exists(os.path.join(paths.home(), "state")))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(paths.cache_dir()).st_mode), 0o700)

    def test_cache_dir_follows_relayctl_home(self):
        first = rcache.cache_path("s")
        other = os.path.join(tempfile.gettempdir(), "relayctl-cache-check")
        with mock.patch.dict(os.environ, {"RELAYCTL_HOME": other}):
            self.assertNotEqual(rcache.cache_path("s"), first)
        self.assertEqual(rcache.cache_path("s"), first)

    def test_concurrent_sections_are_not_lost(self):
        sections = ["balance", "groups", "keys", "probe"]
        barrier = threading.Barrier(len(sections))
        errors = []

        def writer(section):
            try:
                barrier.wait(timeout=5)
                rcache.write("s", section, {"value": section, "items": list(range(20))})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(s,)) for s in sections]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)

        self.assertFalse(errors)
        for section in sections:
            entry = rcache.read("s", section)
            self.assertIsNotNone(entry, section)
            self.assertEqual(entry["data"]["value"], section)
            self.assertEqual(entry["data"]["items"], list(range(20)))

    def test_invalidate_accepts_string_and_list(self):
        rcache.write("s", "balance", {"v": 1})
        rcache.write("s", "keys", {"v": 2})
        rcache.invalidate("s", "keys")
        self.assertIsNone(rcache.read("s", "keys"))
        self.assertIsNotNone(rcache.read("s", "balance"))
        rcache.invalidate("s", ["balance"])
        self.assertIsNone(rcache.read("s", "balance"))
        rcache.invalidate("s")
        self.assertFalse(os.path.exists(rcache.cache_path("s")))

    def test_failed_pull_keeps_previous_cache_and_marks_stale(self):
        rcache.write("s", "groups", [{"name": "default"}])
        fetched_at = rcache.read("s", "groups")["fetched_at"]

        def boom():
            raise RuntimeError("upstream down")

        data, meta = rcache.fetch("s", "groups", boom, rcache.MODE_REFRESH)
        self.assertEqual(data, [{"name": "default"}])
        self.assertTrue(meta["from_cache"])
        self.assertTrue(meta["stale"])
        self.assertEqual(meta["fetched_at"], fetched_at)

        ent = rcache.read("s", "groups")
        self.assertTrue(ent["stale"])
        self.assertIn("RuntimeError", ent["last_error"])
        self.assertEqual(ent["fetched_at"], fetched_at)
        # 权限不能被 stale 更新破坏
        self.assertEqual(stat.S_IMODE(os.stat(rcache.cache_path("s")).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
