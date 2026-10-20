import os
import json
import stat
import tempfile
import unittest
from unittest import mock

from relayctl import cache as rcache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = mock.patch.object(rcache, "CACHE_DIR", os.path.join(self.tmp, "cache"))
        self.p.start()
        self.calls = 0

    def tearDown(self):
        self.p.stop()

    def _fn(self, value):
        def f():
            self.calls += 1
            return value
        return f

    def test_live_pull_writes_cache_and_auto_reuses(self):
        d, meta = rcache.fetch("s", "balance", self._fn({"balance": 1}), rcache.MODE_AUTO)
        self.assertFalse(meta["from_cache"])
        self.assertTrue(meta["fetched_at"])
        d2, meta2 = rcache.fetch("s", "balance", self._fn({"balance": 2}), rcache.MODE_AUTO)
        self.assertEqual(d2, {"balance": 1})
        self.assertTrue(meta2["from_cache"])
        self.assertEqual(meta2["fetched_at"], meta["fetched_at"])
        self.assertEqual(self.calls, 1)

    def test_old_cache_never_expires(self):
        rcache.fetch("s", "balance", self._fn({"balance": 1}), rcache.MODE_AUTO)
        import json as _j
        p = rcache.cache_path("s"); d = _j.load(open(p)); d["sections"]["balance"]["ts"] -= 10 * 365 * 86400
        rcache.secure_write_json(p, d)
        data, meta = rcache.fetch("s", "balance", self._fn({"balance": 2}), rcache.MODE_AUTO)
        self.assertEqual(data, {"balance": 1})
        self.assertTrue(meta["from_cache"])
        self.assertEqual(self.calls, 1)

    def test_refresh_always_pulls(self):
        rcache.fetch("s", "groups", self._fn([1]), rcache.MODE_AUTO)
        d, meta = rcache.fetch("s", "groups", self._fn([2]), rcache.MODE_REFRESH)
        self.assertEqual(d, [2])
        self.assertEqual(self.calls, 2)

    def test_cached_mode_never_calls_network(self):
        with self.assertRaises(rcache.CacheMiss):
            rcache.fetch("s", "keys", self._fn([]), rcache.MODE_CACHED)
        self.assertEqual(self.calls, 0)
        rcache.fetch("s", "keys", self._fn([{"id": 1}]), rcache.MODE_AUTO)
        d, meta = rcache.fetch("s", "keys", self._fn([]), rcache.MODE_CACHED)
        self.assertEqual(d, [{"id": 1}])
        self.assertEqual(self.calls, 1)

    def test_failed_pull_keeps_old_cache(self):
        rcache.fetch("s", "balance", self._fn({"balance": 1}), rcache.MODE_AUTO)
        def boom():
            raise RuntimeError("down")
        with self.assertRaises(RuntimeError):
            rcache.fetch("s", "balance", boom, rcache.MODE_REFRESH)
        self.assertEqual(rcache.read("s", "balance")["data"], {"balance": 1})

    def test_raw_keys_never_hit_disk(self):
        keys = [{"id": 1, "key": "sk-REALSECRET123456", "_raw_key": "sk-REALSECRET123456",
                 "key_masked": "sk-R...3456", "nested": {"access_token": "tok", "ok": 1}}]
        rcache.fetch("s", "keys", self._fn(keys), rcache.MODE_AUTO)
        raw = open(rcache.cache_path("s"), encoding="utf-8").read()
        self.assertNotIn("REALSECRET", raw)
        self.assertNotIn("tok\"", raw)
        self.assertIn("sk-R...3456", raw)
        mode = stat.S_IMODE(os.stat(rcache.cache_path("s")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_invalidate_sections_and_site(self):
        rcache.fetch("s", "balance", self._fn(1), rcache.MODE_AUTO)
        rcache.fetch("s", "keys", self._fn(2), rcache.MODE_AUTO)
        rcache.invalidate("s", ["keys"])
        self.assertIsNone(rcache.read("s", "keys"))
        self.assertIsNotNone(rcache.read("s", "balance"))
        rcache.invalidate("s")
        self.assertIsNone(rcache.read("s", "balance"))

    def test_site_name_cannot_escape_dir(self):
        p = rcache.cache_path("../../etc/x")
        self.assertTrue(os.path.dirname(p) == rcache.CACHE_DIR)



if __name__ == "__main__":
    unittest.main()
