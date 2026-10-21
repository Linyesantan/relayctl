import json
import os
import tempfile
import unittest
from unittest.mock import Mock

from relayctl import cache as rcache
from relayctl.adapters.base import NotSupported
from relayctl.adapters.newapi import NewAPIAdapter
from relayctl.models import KEY_FIELDS


class Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = ""
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._data


def adapter():
    a = NewAPIAdapter("t", {"base": "http://example"}, {})
    a._probed = True
    a._quota_unit_known = True
    a.quota_per_unit = 500000
    return a


class CreateKeySignatureTests(unittest.TestCase):
    def test_unlimited_flag_creates_unlimited_key(self):
        a = adapter()
        a.client = Mock()
        a.client.post.return_value = Resp({"success": True, "data": {"id": 1, "key": "sk-abc123456789"}})
        res = a.create_key("k", "default", unlimited=True)
        payload = a.client.post.call_args.kwargs["json"]
        self.assertTrue(payload["unlimited_quota"])
        self.assertEqual(payload["remain_quota"], 0)
        self.assertTrue(res["unlimited"])
        self.assertEqual(res["key_masked"], "sk-a...6789")

    def test_quota_none_without_unlimited_still_counts_as_unlimited(self):
        a = adapter()
        a.client = Mock()
        a.client.post.return_value = Resp({"success": True, "data": {"id": 2}})
        a.create_key("k", "default")
        payload = a.client.post.call_args.kwargs["json"]
        self.assertTrue(payload["unlimited_quota"])

    def test_explicit_quota_is_not_unlimited(self):
        a = adapter()
        a.client = Mock()
        a.client.post.return_value = Resp({"success": True, "data": {"id": 3, "key": "sk-xyz"}})
        res = a.create_key("k", "default", quota=2, models=["gpt-4o"])
        payload = a.client.post.call_args.kwargs["json"]
        self.assertFalse(payload["unlimited_quota"])
        self.assertEqual(payload["remain_quota"], 1000000)
        self.assertEqual(payload["model_limits"], "gpt-4o")
        self.assertFalse(res["unlimited"])

    def test_create_after_diff_does_not_need_raw_key(self):
        a = adapter()
        a.client = Mock()
        a.client.post.return_value = Resp({"success": True, "data": {}})  # 无 id 也无 key

        snapshots = [
            [{"id": 1, "name": "old", "status": 1, "key": "sk-old-old-old"}],
            [{"id": 1, "name": "old", "status": 1, "key": "sk-old-old-old"},
             {"id": 2, "name": "fresh", "status": 1, "key": "sk-new-new-new"}],
        ]
        a.list_keys = Mock(side_effect=lambda: snapshots.pop(0))

        res = a.create_key("fresh", "default", quota=1)
        self.assertEqual(res["id"], 2)
        # 明文既不出现在返回里，也不出现在任何输出中
        self.assertNotIn("_raw_key", res)
        self.assertNotIn("sk-new-new-new", json.dumps(res, ensure_ascii=False))


class ListKeysOutputTests(unittest.TestCase):
    def test_list_keys_has_stable_fields_without_plaintext(self):
        a = adapter()
        a.client = Mock()
        a.client.get.return_value = Resp({
            "success": True,
            "data": [{
                "id": 7, "name": "k1", "group": "pro", "status": 1,
                "unlimited_quota": False, "remain_quota": 500000, "used_quota": 250000,
                "model_limits": "gpt-4o,claude", "key": "sk-REALSECRET123456",
            }],
        })
        keys = a.list_keys()
        self.assertEqual(len(keys), 1)
        self.assertEqual(tuple(keys[0].keys()), KEY_FIELDS)
        self.assertTrue(keys[0]["enabled"])
        self.assertEqual(keys[0]["status"], 1)
        self.assertFalse(keys[0]["unlimited"])
        self.assertEqual(keys[0]["key_masked"], "sk-R...3456")
        self.assertEqual(keys[0]["models"], ["gpt-4o", "claude"])
        serialized = json.dumps(keys, ensure_ascii=False)
        self.assertNotIn("REALSECRET", serialized)
        self.assertNotIn("_raw_key", serialized)

    def test_disabled_and_unlimited_keys_are_normalized(self):
        a = adapter()
        a.client = Mock()
        a.client.get.return_value = Resp({
            "success": True,
            "data": [
                {"id": 1, "name": "off", "status": 2, "unlimited_quota": False, "key": "sk-1"},
                {"id": 2, "name": "inf", "status": 1, "unlimited_quota": True, "key": "sk-2"},
            ],
        })
        keys = {k["id"]: k for k in a.list_keys()}
        self.assertFalse(keys[1]["enabled"])
        self.assertEqual(keys[1]["status"], 2)
        self.assertTrue(keys[2]["unlimited"])
        self.assertEqual(keys[2]["remain"], "无限")


class ProbeReuseTests(unittest.TestCase):
    """probe 结果可缓存复用：同一实例 probe 一次，跨实例从 probe 缓存恢复配额底数。"""

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _adapter(self, base="http://example"):
        return NewAPIAdapter("t", {"base": base, "adapter": "newapi", "auth": {}}, {})

    def _client(self, unit=500000):
        calls = {"n": 0}

        def get(path, **kwargs):
            if path == "/api/status":
                calls["n"] += 1
                return Resp({"data": {"system_name": "New API", "version": "v1.0.0",
                                      "quota_per_unit": unit,
                                      "quota_display_type": "USD"}})
            if path in ("/api/user/self", "/api/v1/user/profile"):
                return Resp({"success": True, "data": {"quota": 1000000, "used_quota": 500000,
                                                       "group": "default"}})
            if path.startswith("/api/token/"):
                return Resp({"success": True, "data": {"items": [], "total": 0}})
            return Resp({}, status=404)

        client = Mock()
        client.get.side_effect = get
        client.calls = calls
        return client

    def test_same_instance_probes_only_once(self):
        a = self._adapter()
        client = self._client()
        a.client = client
        real_probe = a.probe
        a.probe = Mock(side_effect=real_probe)

        a.balance()
        a.list_keys()
        self.assertEqual(a.probe.call_count, 1)
        self.assertTrue(a._probed)

    def test_quota_per_unit_is_restored_from_probe_cache(self):
        rcache.write("t", "probe", {"site": "t", "system_name": "New API",
                                    "version": "v1.0.0", "quota_per_unit": 250000,
                                    "quota_display_type": "CNY", "status_ok": True})
        a = self._adapter()
        a.client = self._client(unit=500000)
        a.probe = Mock(side_effect=RuntimeError("网络被禁用"))

        data = a.balance()
        a.probe.assert_not_called()
        self.assertEqual(data["quota_per_unit"], 250000)
        self.assertEqual(data["display_type"], "CNY")
        self.assertEqual(data["balance"], "4.0000")  # 1000000 / 250000

    def test_failed_probe_cache_never_unlocks_quota_writes(self):
        """探测都没成功的缓存里的 quota_per_unit 不可信，额度写操作必须继续被挡。"""
        a = self._adapter()
        a.client = Mock()                       # 绝不真的打网络
        a.probe = Mock(side_effect=RuntimeError("网络不可用"))
        rcache.write("t", "probe", {"site": "t", "quota_per_unit": 250000,
                                    "status_ok": False, "status_error": "connection refused"})
        a._ensure_probed()
        self.assertFalse(a._quota_unit_known)
        self.assertNotEqual(a.quota_per_unit, 250000)
        with self.assertRaises(NotSupported):
            a._ensure_quota_unit()

    def test_probe_cache_result_is_used_by_cli_flow(self):
        """probe 走缓存：CLI 第二次取 probe 不再打网络。"""
        a = self._adapter()
        a.client = self._client()
        probe_data, meta = rcache.fetch("t", "probe", a.probe, rcache.MODE_REFRESH)
        self.assertFalse(meta["from_cache"])
        self.assertEqual(probe_data["quota_per_unit"], 500000)

        again, meta2 = rcache.fetch("t", "probe", a.probe, rcache.MODE_AUTO)
        self.assertTrue(meta2["from_cache"])
        self.assertEqual(again["quota_per_unit"], 500000)
        self.assertEqual(meta["fetched_at"], meta2["fetched_at"])


class GroupsTests(unittest.TestCase):
    def test_groups_failure_raises_not_supported(self):
        a = adapter()
        a.client = Mock()
        a.client.get.return_value = Resp({}, status=500)
        with self.assertRaises(NotSupported):
            a.groups()
        a.client.get.assert_called_once_with("/api/user/self/groups")

    def test_groups_empty_does_not_fabricate_default(self):
        a = adapter()
        a.client = Mock()
        a.client.get.return_value = Resp({"success": True, "data": {}})
        with self.assertRaises(NotSupported):
            a.groups()
        for call in a.client.get.call_args_list:
            self.assertNotIn("/api/pricing", call.args[0])


if __name__ == "__main__":
    unittest.main()
