import unittest
from unittest.mock import Mock

from relayctl.adapters.newapi import NewAPIAdapter
from relayctl.adapters.base import NotSupported


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


class NewAPISafetyTests(unittest.TestCase):
    def test_mixed_status_uses_second_status_only_request(self):
        a = adapter()
        calls = []
        a.client = Mock()
        a.client.get.return_value = Resp({"success": True, "data": {"id": 7, "name": "old", "status": 0}})
        a.client.put.side_effect = lambda url, json: (calls.append((url, json)) or Resp({"success": True}))
        a.update_key(7, name="new", status=1)
        self.assertEqual([c[0] for c in calls], ["/api/token/", "/api/token/?status_only=true"])
        self.assertNotIn("status", calls[0][1])
        self.assertEqual(calls[1][1]["status"], 1)

    def test_status_second_phase_failure_reports_partial_success(self):
        a = adapter()
        a.client = Mock()
        a.client.get.return_value = Resp({"success": True, "data": {"id": 7}})
        a.client.put.side_effect = [Resp({"success": True}), Resp({"success": False, "message": "busy"})]
        with self.assertRaisesRegex(Exception, "已完成其他字段更新"):
            a.update_key(7, name="new", status=1)

    def test_add_quota_then_enable_uses_status_only_after_full_update(self):
        a = adapter()
        calls = []
        a.client = Mock()
        a.client.get.return_value = Resp({"success": True, "data": {"id": 7, "status": 0, "remain_quota": 0}})
        a.client.put.side_effect = lambda url, json: (calls.append((url, json)) or Resp({"success": True}))
        a.update_key(7, quota=2, status=1)
        self.assertEqual(calls[0][1]["remain_quota"], 1000000)
        self.assertNotIn("status", calls[0][1])
        self.assertEqual(calls[1], ("/api/token/?status_only=true", {"id": 7, "status": 1}))

    def test_create_does_not_claim_old_same_name_when_before_snapshot_failed(self):
        a = adapter()
        a.client = Mock()
        a.client.post.return_value = Resp({"success": True, "data": {}})
        calls = [0]
        def list_keys():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("list unavailable")
            return [{"id": 9, "name": "same", "_raw_key": "old"}]
        a.list_keys = Mock(side_effect=list_keys)
        with self.assertRaises(NotSupported):
            a.create_key("same", "default", None)

    def test_unknown_quota_unit_blocks_quota_write(self):
        a = adapter()
        a._quota_unit_known = False
        a.client = Mock()
        with self.assertRaises(NotSupported):
            a.create_key("k", "default", 1)
        a.client.post.assert_not_called()

    def test_known_quota_unit_is_used_for_create_payload(self):
        a = adapter()
        a.client = Mock()
        a.client.post.return_value = Resp({"success": True, "data": {"id": 8, "key": "sk-test"}})
        result = a.create_key("k", "default", 1.5)
        payload = a.client.post.call_args.kwargs["json"]
        self.assertEqual(payload["remain_quota"], 750000)
        self.assertEqual(result["id"], 8)


if __name__ == "__main__":
    unittest.main()
