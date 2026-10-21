"""阶段 5 导出器测试（外部协议依据见 relayctl/exporters.py 顶部注释）。

CC Switch 依据：v4.0.17 发布页 + 深链协议文档 5.3 +
站长接入指南 https://ccswitch.io/zh/tutorials/deeplink-site-integration-guide（适用 v4.0.10+）
New API 依据：QuantumNous/new-api-docs 渠道管理模块（v1.0.0-rc.44 时期）

所有网络访问都用 mock transport，单测绝不打网络。
"""

import json
import os
import stat
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

from relayctl import config, exporters, importers
from relayctl.errors import EXIT_AUTH, EXIT_FAIL, AuthError, ConfigError

# Base64 里同时含 '+' 与 '=' 的样例（用于断言第二层 URL 编码）
B64_WITH_PLUS_TEXT = "cfg-\u4f80"  # -> Y2ZnLeS+gA==

KEY = "sk-TESTKEYaaaaaaaaaaaa"
KEY2 = "sk-OTHERbbbbbbbbbbbbbb"
TOKEN = "sk-admin-token-0123456789"
BASE = "https://api.example.com/v1"


class FakeResponse:
    """够用的响应替身：status_code / text / json()。"""

    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (
            json.dumps(payload) if payload is not None else "")

    def json(self):
        if not isinstance(self._payload, dict):
            raise ValueError("not a json object")
        return self._payload


class Recorder:
    """mock transport：记录调用并交给 handler 返回响应。"""

    def __init__(self, handler):
        self.calls = []
        self.handler = handler

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "body": body})
        return self.handler(method, url, headers, body)

    def by_method(self, method):
        return [c for c in self.calls if c["method"] == method]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()
        config.init_layout()

    def tearDown(self):
        self._env.stop()

    def item(self, key=KEY, base=BASE, models=None, groups=None, ident=None,
             site="site-a"):
        return importers.key_item({
            "id": ident, "key": key, "source_site": site, "base": base,
            "tags": ["vip"], "models": models or ["gpt-4", "gpt-5"],
            "groups": groups or ["default"], "assigned_to": "", "notes": "",
        })

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# Base64 + URL 编码（CC Switch 两层编码规则）
# ---------------------------------------------------------------------------
class EncodingTests(Base):
    def test_b64_round_trip(self):
        for text in ("hello", "中文配置 /wit+h 特殊字符", "{}", ""):
            self.assertEqual(exporters.b64_decode(exporters.b64_encode(text)), text)

    def test_b64_decode_tolerates_urlsafe_and_missing_padding(self):
        raw = "配置 config with pad"
        std = exporters.b64_encode(raw)
        self.assertEqual(exporters.b64_decode(std.rstrip("=")), raw)
        urlsafe = std.replace("+", "-").replace("/", "_")
        self.assertEqual(exporters.b64_decode(urlsafe), raw)

    def test_b64_decode_rejects_garbage(self):
        with self.assertRaises(ConfigError):
            exporters.b64_decode("!!!!not-base64!!!!")

    def test_plus_and_equal_are_url_encoded(self):
        # 标准 Base64 里的 '+' 必须变成 %2B、'=' 必须变成 %3D（否则 '+' 会被解成空格）
        encoded = exporters.b64_encode(B64_WITH_PLUS_TEXT)
        self.assertIn("+", encoded)
        self.assertIn("=", encoded)
        link = exporters.ccswitch_link([("resource", "provider"), ("config", encoded),
                                        ("configFormat", "toml")])
        self.assertIn("%2B", link)
        self.assertIn("%3D", link)
        self.assertNotIn("=%s&" % encoded, link)
        parsed = exporters.parse_ccswitch_link(link)
        self.assertEqual(parsed["config_text"], B64_WITH_PLUS_TEXT)

    def test_b64_decode_rejects_non_utf8_payload(self):
        # 解出来不是合法 UTF-8 时要报配置错误，而不是返回乱码
        with self.assertRaises(ConfigError):
            exporters.b64_decode("+/w=")

    def test_b64_payload_round_trip_through_link(self):
        text = "model_provider = \"custom\"\n[model_providers.custom]\n"
        link = exporters.build_ccswitch_link(app="codex", name="n",
                                             endpoint="https://a.example.com/v1",
                                             config=text, config_format="toml")
        parsed = exporters.parse_ccswitch_link(link)
        self.assertEqual(parsed["config_text"], text)

    def test_space_becomes_percent20_not_plus(self):
        link = exporters.build_ccswitch_link(app="claude", name="My Provider",
                                             endpoint="https://a.example.com")
        self.assertIn("name=My%20Provider", link)
        self.assertNotIn("+", urlparse(link).query)


# ---------------------------------------------------------------------------
# 深链 round-trip
# ---------------------------------------------------------------------------
class DeepLinkTests(Base):
    def test_round_trip_all_params(self):
        name = "我的 供应商 & Co=1 +2"
        notes = "备注/特殊?&=# 中文"
        link = exporters.build_ccswitch_link(
            app="claude", name=name, endpoint="https://api.example.com",
            api_key=KEY, model="claude-sonnet-4-5",
            api_format="anthropic", notes=notes, homepage="https://home.example.com",
            config=json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.example.com"}}),
            config_format="json", enabled=False)
        self.assertTrue(link.startswith("ccswitch://v1/import?"))
        parsed = exporters.parse_ccswitch_link(link)
        self.assertEqual(parsed["resource"], "provider")
        self.assertEqual(parsed["app"], "claude")
        self.assertEqual(parsed["name"], name)
        self.assertEqual(parsed["endpoint"], "https://api.example.com")
        self.assertEqual(parsed["apiKey"], KEY)
        self.assertEqual(parsed["apiFormat"], "anthropic")
        self.assertEqual(parsed["model"], "claude-sonnet-4-5")
        self.assertEqual(parsed["notes"], notes)
        self.assertEqual(parsed["enabled"], "false")
        self.assertEqual(parsed["configFormat"], "json")
        self.assertEqual(json.loads(parsed["config_text"])["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.example.com")

    def test_special_characters_in_name_and_notes(self):
        # URL 里的 &=?/# 等字符必须被编码且能原样解析回来
        name = "a&b=c?d#e/f g"
        notes = "key=value;备注\n第二行"
        link = exporters.build_ccswitch_link(app="codex", name=name,
                                             endpoint="https://api.example.com/v1",
                                             api_key=KEY, notes=notes)
        parsed = exporters.parse_ccswitch_link(link)
        self.assertEqual(parsed["name"], name)
        self.assertEqual(parsed["notes"], notes)

    def test_parse_rejects_non_ccswitch(self):
        with self.assertRaises(ConfigError):
            exporters.parse_ccswitch_link("https://example.com/v1/import?resource=provider")

    def test_parse_rejects_wrong_path(self):
        with self.assertRaises(ConfigError):
            exporters.parse_ccswitch_link("ccswitch://v1/other?resource=provider")

    def test_build_requires_name_and_known_app(self):
        with self.assertRaises(ConfigError):
            exporters.build_ccswitch_link(app="claude", name="")
        with self.assertRaises(ConfigError):
            exporters.build_ccswitch_link(app="notepad", name="x")

    def test_build_rejects_bad_api_format(self):
        with self.assertRaises(ConfigError):
            exporters.build_ccswitch_link(app="claude", name="x", api_format="chat")

    def test_configurl_is_never_generated(self):
        # configUrl 尚未实现（带它的链接会被 CC Switch 直接拒绝），本模块不生成
        link = exporters.build_ccswitch_link(app="claude", name="x",
                                             endpoint="https://api.example.com", api_key=KEY)
        self.assertNotIn("configUrl", link)
        self.assertEqual(exporters.parse_ccswitch_link(link)["configUrl"], "")


# ---------------------------------------------------------------------------
# 供应商配置：Claude Code / Codex
# ---------------------------------------------------------------------------
class ProviderConfigTests(Base):
    def test_claude_env_keys(self):
        payload = exporters.claude_provider_config(
            "https://api.example.com", key=KEY, model="claude-sonnet-4-5",
            small_model="claude-haiku-4-5")
        env = payload["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.example.com")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], KEY)
        self.assertEqual(env["ANTHROPIC_API_KEY"], KEY)
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-sonnet-4-5")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "claude-haiku-4-5")

    def test_claude_endpoint_has_no_v1_codex_has_v1(self):
        self.assertEqual(exporters.claude_endpoint(BASE), "https://api.example.com")
        self.assertEqual(exporters.codex_endpoint(BASE), "https://api.example.com/v1")
        self.assertEqual(exporters.codex_endpoint("https://api.example.com"),
                         "https://api.example.com/v1")

    def test_codex_toml_uses_responses_only(self):
        toml_text = exporters.codex_config_toml(
            "https://api.example.com/v1", key=KEY, model="gpt-5-codex")
        self.assertIn("[model_providers.custom]", toml_text)
        self.assertIn('wire_api = "responses"', toml_text)
        self.assertIn('experimental_bearer_token = "%s"' % KEY, toml_text)
        self.assertIn('model_provider = "custom"', toml_text)
        # 已移除的 chat wire_api 绝不出现
        self.assertNotIn("chat", toml_text.replace("chatgpt", ""))

    def test_codex_provider_config_json_form(self):
        payload = exporters.codex_provider_config(
            "https://api.example.com/v1", key=KEY, model="gpt-5-codex")
        self.assertEqual(payload["auth"]["OPENAI_API_KEY"], KEY)
        self.assertIn('wire_api = "responses"', payload["config"])

    def test_codex_toml_escapes_quotes(self):
        text = exporters.codex_config_toml(BASE, key='sk-a"b\\c', model="m\nx")
        self.assertIn('\\"', text)
        self.assertIn("\\\\", text)
        self.assertIn("\\n", text)

    def test_ccswitch_entry_masked_by_default(self):
        entry = exporters.ccswitch_entry(self.item(), "claude")
        self.assertNotIn(KEY, entry["link"])
        self.assertNotIn(KEY, entry["config"])
        self.assertFalse(entry["reveal"])
        self.assertEqual(entry["api_format"], "anthropic")
        self.assertEqual(entry["endpoint"], "https://api.example.com")

    def test_ccswitch_entry_reveal_contains_key(self):
        entry = exporters.ccswitch_entry(self.item(), "codex", reveal=True)
        self.assertIn(KEY, entry["link"])
        self.assertTrue(entry["reveal"])
        parsed = exporters.parse_ccswitch_link(entry["link"])
        self.assertEqual(parsed["apiKey"], KEY)
        self.assertEqual(parsed["configFormat"], "toml")
        self.assertIn("wire_api", parsed["config_text"])

    def test_ccswitch_entry_codex_config_is_toml_with_responses(self):
        entry = exporters.ccswitch_entry(self.item(), "codex")
        self.assertEqual(entry["config_format"], "toml")
        self.assertIn('wire_api = "responses"', entry["config"])
        # 脱敏预览里不能出现明文 Key
        self.assertNotIn(KEY, entry["config"])

    def test_ccswitch_export_all_apps(self):
        result = exporters.ccswitch_export([self.item()], app="all")
        self.assertEqual(result["count"], 2)
        self.assertEqual([i["app"] for i in result["items"]], ["claude", "codex"])
        single = exporters.ccswitch_export([self.item()], app="claude")
        self.assertEqual(single["count"], 1)

    def test_ccswitch_entry_requires_base(self):
        with self.assertRaises(ConfigError):
            exporters.ccswitch_entry(self.item(base=""), "claude")


# ---------------------------------------------------------------------------
# New API 渠道
# ---------------------------------------------------------------------------
class ChannelPayloadTests(Base):
    def test_groups_is_array_models_is_csv(self):
        payload = exporters.channel_payload("relayctl-1", KEY, base_url=BASE,
                                            models=["gpt-4", "gpt-5"],
                                            groups=["default", "vip"])
        self.assertIsInstance(payload["groups"], list)
        self.assertEqual(payload["groups"], ["default", "vip"])
        self.assertEqual(payload["models"], "gpt-4,gpt-5")
        self.assertEqual(payload["type"], 1)
        self.assertEqual(payload["key"], KEY)

    def test_type_mapping(self):
        self.assertEqual(exporters.channel_payload("n", KEY, channel_type="anthropic")["type"], 14)
        self.assertEqual(exporters.channel_payload("n", KEY, channel_type="codex")["type"], 57)
        self.assertEqual(exporters.channel_payload("n", KEY, channel_type="24")["type"], 24)
        self.assertEqual(exporters.channel_payload("n", KEY)["groups"], ["default"])

    def test_rejects_empty_name_or_key(self):
        with self.assertRaises(ConfigError):
            exporters.channel_payload("", KEY)
        with self.assertRaises(ConfigError):
            exporters.channel_payload("n", "")


class NewApiClientTests(Base):
    def client(self, handler):
        rec = Recorder(handler)
        return exporters.NewApiChannelClient(BASE, TOKEN, user_id=7, transport=rec), rec

    def test_create_channel_headers_and_body(self):
        state = {"created": False}

        def handler(method, url, headers, body):
            if method == "POST":
                state["created"] = True
                return FakeResponse(200, {"success": True, "message": ""})
            # 创建前查不到 -> create；创建后回查拿到 id
            items = [{"id": 42, "name": "relayctl-abc"}] if state["created"] else []
            return FakeResponse(200, {"success": True, "message": "",
                                      "data": {"items": items}})
        client, rec = self.client(handler)
        outcome = client.push_channel("relayctl-abc",
                                      exporters.channel_payload("relayctl-abc", KEY),
                                      dry_run=False)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "create")
        self.assertEqual(outcome["channel_id"], 42)
        post = rec.by_method("POST")[0]
        self.assertTrue(post["url"].endswith("/api/channel/"))
        self.assertEqual(post["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(post["headers"]["New-Api-User"], "7")
        self.assertEqual(post["headers"]["Content-Type"], "application/json")
        self.assertEqual(post["body"]["mode"], "single")
        self.assertEqual(post["body"]["channel"]["name"], "relayctl-abc")
        self.assertIsInstance(post["body"]["channel"]["groups"], list)
        get = rec.by_method("GET")[0]
        self.assertIn("/api/channel/search", get["url"])
        self.assertIn("keyword=relayctl-abc", get["url"])

    def test_update_path_when_channel_exists(self):
        def handler(method, url, headers, body):
            if method == "GET":
                return FakeResponse(200, {"success": True, "message": "",
                                          "data": {"items": [{"id": 9, "name": "relayctl-abc"}]}})
            return FakeResponse(200, {"success": True, "message": ""})
        client, rec = self.client(handler)
        outcome = client.push_channel("relayctl-abc",
                                      exporters.channel_payload("relayctl-abc", KEY),
                                      dry_run=False)
        self.assertEqual(outcome["action"], "update")
        self.assertEqual(outcome["channel_id"], 9)
        put = rec.by_method("PUT")[0]
        self.assertEqual(put["body"]["id"], 9)
        self.assertEqual(put["body"]["name"], "relayctl-abc")
        self.assertEqual(rec.by_method("POST"), [])

    def test_http_200_with_success_false_is_failure(self):
        def handler(method, url, headers, body):
            if method == "GET":
                return FakeResponse(200, {"success": True, "message": "",
                                          "data": {"items": []}})
            return FakeResponse(200, {"success": False, "message": "API密钥无效"})
        client, rec = self.client(handler)
        with self.assertRaises(exporters.ApiError) as ctx:
            client.push_channel("relayctl-abc",
                                exporters.channel_payload("relayctl-abc", KEY),
                                dry_run=False)
        self.assertIn("API密钥无效", str(ctx.exception))
        self.assertEqual(ctx.exception.exit_code, EXIT_FAIL)

    def test_http_401_is_auth_error(self):
        client, rec = self.client(lambda *a: FakeResponse(401, text="unauthorized"))
        with self.assertRaises(AuthError) as ctx:
            client.create_channel(exporters.channel_payload("n", KEY))
        self.assertEqual(ctx.exception.exit_code, EXIT_AUTH)

    def test_token_never_leaks_into_error_message(self):
        def handler(method, url, headers, body):
            return FakeResponse(200, {"success": False,
                                      "message": f"令牌 {TOKEN} 无效"})
        client, rec = self.client(handler)
        with self.assertRaises(exporters.ApiError) as ctx:
            client.create_channel(exporters.channel_payload("n", KEY))
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("...", str(ctx.exception))

    def test_ambiguous_name_is_reported_not_guessed(self):
        def handler(method, url, headers, body):
            return FakeResponse(200, {"success": True, "message": "", "data": {"items": [
                {"id": 1, "name": "relayctl-abc"},
                {"id": 2, "name": "relayctl-abc"},
            ]}})
        client, rec = self.client(handler)
        with self.assertRaises(exporters.AmbiguousChannel):
            client.find_channel_id("relayctl-abc")

    def test_dry_run_makes_no_request(self):
        client, rec = self.client(lambda *a: FakeResponse(200, {"success": True}))
        outcome = client.push_channel("relayctl-abc",
                                      exporters.channel_payload("relayctl-abc", KEY),
                                      dry_run=True)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["action"], "create")
        self.assertEqual(rec.calls, [])

    def test_find_channel_id_returns_none_when_missing(self):
        def handler(method, url, headers, body):
            return FakeResponse(200, {"success": True, "message": "", "data": {
                "items": [{"id": 1, "name": "other"}]}})
        client, rec = self.client(handler)
        self.assertIsNone(client.find_channel_id("relayctl-abc"))

    def test_client_requires_base_and_token(self):
        with self.assertRaises(ConfigError):
            exporters.NewApiChannelClient("", TOKEN)
        with self.assertRaises(ConfigError):
            exporters.NewApiChannelClient(BASE, "")

    def test_newapi_export_marks_failures_and_masks_keys(self):
        def handler(method, url, headers, body):
            if method == "GET":
                return FakeResponse(200, {"success": True, "message": "", "data": {"items": []}})
            return FakeResponse(200, {"success": False, "message": "渠道已存在"})
        rec = Recorder(handler)
        result = exporters.newapi_export([self.item()], BASE, TOKEN, user_id=7,
                                         dry_run=False, transport=rec)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["items"][0]["ok"])
        self.assertIn("渠道已存在", result["items"][0]["message"])
        # 结果里绝不带明文
        self.assertNotIn(KEY, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(TOKEN, json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["items"][0]["key_masked"], importers.mask_key(KEY))

    def test_newapi_export_dry_run_offline(self):
        rec = Recorder(lambda *a: FakeResponse(200, {"success": True}))
        result = exporters.newapi_export([self.item(), self.item(key=KEY2, ident="x")],
                                         BASE, TOKEN, user_id=7, dry_run=True, transport=rec)
        self.assertEqual(rec.calls, [])
        self.assertTrue(all(i["ok"] for i in result["items"]))
        self.assertTrue(result["dry_run"])


# ---------------------------------------------------------------------------
# env / json / csv
# ---------------------------------------------------------------------------
class GenericExportTests(Base):
    def test_env_masked_and_revealed(self):
        item = self.item()
        ident = item["id"]
        masked = exporters.env_export([item])
        self.assertIn(f"RELAYCTL_{ident.upper()}_BASE=", masked)
        self.assertNotIn(KEY, masked)
        self.assertIn(importers.mask_key(KEY), masked)
        revealed = exporters.env_export([item], reveal=True)
        self.assertIn(KEY, revealed)

    def test_json_export_has_no_plaintext_by_default(self):
        payload = json.loads(exporters.json_export([self.item()]))
        self.assertEqual(payload["count"], 1)
        self.assertNotIn("key", payload["items"][0])
        self.assertEqual(payload["items"][0]["key_masked"], importers.mask_key(KEY))
        revealed = json.loads(exporters.json_export([self.item()], reveal=True))
        self.assertEqual(revealed["items"][0]["key"], KEY)

    def test_csv_export_columns(self):
        text = exporters.csv_export([self.item()])
        lines = text.strip().splitlines()
        self.assertTrue(lines[0].startswith("id,key_masked,source_site"))
        self.assertNotIn("key", lines[0].split(","))
        self.assertNotIn(KEY, text)
        revealed = exporters.csv_export([self.item()], reveal=True)
        self.assertIn("key", revealed.splitlines()[0].split(","))
        self.assertIn(KEY, revealed)
        self.assertEqual(len(revealed.strip().splitlines()), 2)

    def test_export_files_are_0600(self):
        for target in ("env", "json", "csv"):
            path = os.path.join(self.tmp, f"out.{target}")
            exporters.write_export_file(path, exporters.generic_export(target, [self.item()]))
            self.assertEqual(self.mode(path), 0o600, f"{target} 导出文件必须是 0600")

    def test_generic_export_rejects_unknown_target(self):
        with self.assertRaises(ConfigError):
            exporters.generic_export("toml", [self.item()])

    def test_scrub_text_masks_secrets(self):
        self.assertNotIn(KEY, exporters.scrub_text(f"key {KEY} 无效", [KEY]))
