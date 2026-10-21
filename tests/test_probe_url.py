import json
import os
import tempfile
import unittest
from unittest import mock

from relayctl import probe as rprobe
from relayctl.errors import ConfigError


class Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("响应不是 JSON")
        return self._payload


class FakeClient:
    """只记录调用路径的假客户端（任何 pricing 调用都会在断言中被抓出来）。"""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append(path)
        item = self.mapping.get(path, self.default)
        if item is None:
            return Resp({}, status=404)
        return item if isinstance(item, Resp) else Resp(item)


def status_payload(system_name="New API", version="v0.9.0", **extra):
    return {"success": True, "message": "", "data": dict(
        {"system_name": system_name, "version": version}, **extra)}


class PublicEndpointGuardTests(unittest.TestCase):
    def test_pricing_is_forbidden(self):
        with self.assertRaises(ConfigError):
            rprobe.ensure_public_endpoint("/api/pricing")
        with self.assertRaises(ConfigError):
            rprobe.ensure_public_endpoint("/api/v1/pricing")
        with self.assertRaises(ConfigError):
            rprobe.ensure_public_endpoint("/api/pricing?all=1")

    def test_only_public_endpoints_allowed(self):
        self.assertEqual(rprobe.ensure_public_endpoint("/api/status"), "/api/status")
        for bad in ("/api/user/self", "/api/token", "", "/api/status/../admin"):
            with self.assertRaises(ConfigError):
                rprobe.ensure_public_endpoint(bad)


class ProbeUrlTests(unittest.TestCase):
    def test_identifies_newapi(self):
        client = FakeClient({"/api/status": status_payload("New API", "v0.9.0")})
        result = rprobe.probe_url("https://api.example.com/v1", client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["panel"], "newapi")
        self.assertEqual(result["adapter"], "newapi")
        self.assertEqual(result["version"], "v0.9.0")
        self.assertEqual(result["system_name"], "New API")
        self.assertEqual(result["base"], "https://api.example.com")
        self.assertIsNone(result["error"])
        for path in client.calls:
            self.assertNotIn("pricing", path.lower())

    def test_identifies_oneapi_from_system_name(self):
        client = FakeClient({"/api/status": status_payload("One API", "0.6.10")})
        result = rprobe.probe_url("https://one.example.com", client=client)
        self.assertEqual(result["panel"], "oneapi")
        self.assertEqual(result["adapter"], "oneapi")
        self.assertEqual(result["system_name"], "One API")

    def test_prefers_status_and_falls_back_to_v1_status(self):
        client = FakeClient({"/api/v1/status": status_payload("New-API", "v1.2.3")},
                            default=Resp({}, status=404))
        result = rprobe.probe_url("https://x.example.com", client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["panel"], "newapi")
        self.assertEqual(client.calls[0], "/api/status")
        self.assertEqual(client.calls[1], "/api/v1/status")
        self.assertTrue(all(p in rprobe.PUBLIC_ENDPOINTS for p in client.calls))

    def test_unrecognized_returns_custom(self):
        client = FakeClient({"/api/status": {"success": True, "data": {"version": "1.0"}}},
                            default=Resp({}, status=404))
        result = rprobe.probe_url("https://unknown.example.com", client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["panel"], "custom")
        self.assertEqual(result["adapter"], "newapi")
        self.assertIsNotNone(result["error"])

    def test_network_failure_is_reported_not_raised(self):
        class Boom:
            calls = []

            def get(self, path, **kwargs):
                Boom.calls.append(path)
                raise OSError("connection refused")

        result = rprobe.probe_url("https://down.example.com", client=Boom())
        self.assertFalse(result["ok"])
        self.assertEqual(result["panel"], "custom")
        self.assertIn("connection refused", result["error"])
        for path in Boom.calls:
            self.assertNotIn("pricing", path.lower())

    def test_non_json_response_is_not_fabricated(self):
        client = FakeClient(default=Resp("<html>login</html>"))
        result = rprobe.probe_url("https://web.example.com", client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["panel"], "custom")

    def test_default_client_uses_http_client_without_credentials(self):
        fake = FakeClient({"/api/status": status_payload()})

        def factory(base_url, proxy=None, auth=None):
            fake.base_url = base_url
            fake.proxy = proxy
            fake.auth = auth
            return fake

        with mock.patch.object(rprobe, "HttpClient", factory):
            result = rprobe.probe_url("https://default.example.com", proxy="none")
        self.assertTrue(result["ok"])
        self.assertEqual(fake.base_url, "https://default.example.com")
        self.assertEqual(fake.auth, {})
        for path in fake.calls:
            self.assertNotIn("pricing", path.lower())

    def test_result_is_json_serializable_and_stable(self):
        client = FakeClient({"/api/status": status_payload()})
        result = rprobe.probe_url("https://s.example.com", client=client)
        self.assertEqual(sorted(result), sorted(
            ["base", "url", "ok", "panel", "adapter", "version", "system_name",
             "endpoints", "error"]))
        json.dumps(result, ensure_ascii=False)

    def test_empty_address(self):
        result = rprobe.probe_url("   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "地址为空")


class IdentifyPanelTests(unittest.TestCase):
    def test_alias_forms(self):
        for name in ("New API", "new-api", "NEWAPI", "New-API 面板"):
            self.assertEqual(rprobe.identify_panel({"system_name": name}), "newapi")
        for name in ("One API", "one-api", "ONEAPI", "One-API 中转"):
            self.assertEqual(rprobe.identify_panel({"system_name": name}), "oneapi")

    def test_newapi_hint_keys(self):
        self.assertEqual(rprobe.identify_panel({"version": "v1", "quota_per_unit": 500000}), "newapi")

    def test_unknown_and_non_dict(self):
        self.assertIsNone(rprobe.identify_panel({"version": "1.0"}))
        self.assertIsNone(rprobe.identify_panel("not a dict"))
        self.assertIsNone(rprobe.identify_panel(None))


class ReportTests(unittest.TestCase):
    def test_report_lines_have_no_credentials(self):
        lines = rprobe.probe_report_lines(rprobe.probe_url("https://x.example.com"))
        joined = "\n".join(lines)
        for marker in ("pricing", "sk-", "Authorization"):
            self.assertNotIn(marker, joined)


if __name__ == "__main__":
    unittest.main()
