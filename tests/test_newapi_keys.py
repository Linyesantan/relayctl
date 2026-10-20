import json
import unittest
from unittest.mock import Mock

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
