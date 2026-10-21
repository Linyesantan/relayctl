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

    def test_failed_pull_keeps_old_cache_and_marks_stale(self):
        old, old_meta = rcache.fetch("s", "balance", self._fn({"balance": 1}), rcache.MODE_AUTO)

        def boom():
            raise RuntimeError("down")

        # 强刷失败但有旧缓存：返回旧 data，保留 fetched_at，并标记 stale / last_error
        data, meta = rcache.fetch("s", "balance", boom, rcache.MODE_REFRESH)
        self.assertEqual(data, {"balance": 1})
        self.assertTrue(meta["from_cache"])
        self.assertTrue(meta["stale"])
        self.assertIn("RuntimeError: down", meta["last_error"])
        self.assertEqual(meta["fetched_at"], old_meta["fetched_at"])

        # stale 标记必须落盘
        ent = rcache.read("s", "balance")
        self.assertTrue(ent["stale"])
        self.assertTrue(ent["last_error"])
        self.assertEqual(ent["data"], {"balance": 1})
        self.assertEqual(ent["fetched_at"], old_meta["fetched_at"])

        # 没有旧缓存时才允许抛错
        rcache.invalidate("s")
        with self.assertRaises(RuntimeError):
            rcache.fetch("s", "balance", boom, rcache.MODE_REFRESH)

        # 一次成功刷新必须清掉 stale / last_error
        d2, meta2 = rcache.fetch("s", "balance", self._fn({"balance": 2}), rcache.MODE_REFRESH)
        self.assertEqual(d2, {"balance": 2})
        self.assertFalse(meta2["from_cache"])
        self.assertFalse(meta2["stale"])
        self.assertIsNone(meta2["last_error"])
        self.assertFalse(rcache.read("s", "balance")["stale"])

    def test_serve_stale_can_be_disabled(self):
        rcache.fetch("s", "balance", self._fn({"balance": 1}), rcache.MODE_AUTO)

        def boom():
            raise RuntimeError("down")

        with self.assertRaises(RuntimeError):
            rcache.fetch("s", "balance", boom, rcache.MODE_REFRESH, serve_stale=False)
        self.assertEqual(rcache.read("s", "balance")["data"], {"balance": 1})
        self.assertFalse(rcache.read("s", "balance")["stale"])

    def test_last_error_is_redacted(self):
        rcache.fetch("s", "balance", self._fn({"balance": 1}), rcache.MODE_AUTO)

        def boom():
            raise RuntimeError("HTTP 401 from https://x.example.com: sk-SECRET123456789 token=abc")

        self.assertEqual(rcache.fetch("s", "balance", boom, rcache.MODE_REFRESH)[0],
                         {"balance": 1})
        ent = rcache.read("s", "balance")
        self.assertNotIn("SECRET123456789", json.dumps(ent, ensure_ascii=False))

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
