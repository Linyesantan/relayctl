"""阶段 5 分发库测试（dist/keys.yaml 管理与导出编排）。

网络访问一律用 mock transport（relayctl.exporters 支持注入），单测不打网络。
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from relayctl import config, distribution as rdist, importers
from relayctl.errors import EXIT_OK, EXIT_USAGE, ConfigError, UsageError

_HERE = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.join(os.path.dirname(_HERE), "bin", "relayctl")


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("relayctl_cli_dist", CLI_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CLI = _load_cli()

K1 = "sk-AAAAAAAAAAAAAAAAAAAA"
K2 = "sk-BBBBBBBBBBBBBBBBBBBB"
K3 = "sk-CCCCCCCCCCCCCCCCCCCC"
TOKEN = "sk-admin-token-0123456789"
BASE = "https://api.example.com/v1"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (
            json.dumps(payload) if payload is not None else "")

    def json(self):
        if not isinstance(self._payload, dict):
            raise ValueError("not a json object")
        return self._payload


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()
        config.init_layout()

    def tearDown(self):
        self._env.stop()

    # -- 辅助 ---------------------------------------------------------------
    def seed(self, *keys, site="site-a", base=BASE, tags=None, models=None):
        cands = []
        for k in keys:
            cands.append({"key": k, "site": site, "base": base,
                          "tags": list(tags or []), "models": list(models or ["gpt-4"])})
        importers.apply_keys_import(cands, dry_run=False, cfg=self.cfg())
        return [importers.fingerprint(k) for k in keys]

    def cfg(self):
        return config.load_config()

    def raw(self):
        return importers.load_keys_dist()["keys"]

    def add_site(self, name="site-a", base=BASE, token=TOKEN, uid=7):
        auth = dict(config.DEFAULT_AUTH)
        auth["access_token"] = token
        auth["user_id"] = uid
        config.save_site(name, {
            "name": name, "base": base, "console_url": "", "panel": "newapi",
            "adapter": "newapi", "script": None, "auth": auth,
            "docs": dict(config.DEFAULT_DOCS), "proxy": "", "refresh_interval": "",
            "tags": [], "status": config.STATUS_ACTIVE, "parked_reason": "",
        })

    def run_cli(self, *argv):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = CLI.main(list(argv))
        return rc, buf.getvalue(), err.getvalue()

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# list / tag / assign / rm
# ---------------------------------------------------------------------------
class ListTests(Base):
    def test_list_returns_masked_only(self):
        self.seed(K1, K2)
        items = rdist.list_keys()
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertNotIn("key", item)
            self.assertIn("key_masked", item)
        lines = rdist.list_lines(items)
        self.assertTrue(all(importers.mask_key(k) in line for k, line in zip([K1, K2], lines)))

    def test_list_filter_by_tag_and_site(self):
        ids = self.seed(K1, K2, tags=["vip"])
        rdist.tag_key(ids[0], add=["team"])
        self.assertEqual(len(rdist.list_keys(tag="team")), 1)
        self.assertEqual(len(rdist.list_keys(tag="vip")), 2)
        self.assertEqual(len(rdist.list_keys(site="site-a")), 2)
        self.assertEqual(len(rdist.list_keys(site="other")), 0)

    def test_list_empty(self):
        self.assertEqual(rdist.list_keys(), [])
        self.assertIn("暂无", rdist.list_lines([])[0])


class TagTests(Base):
    def test_add_tags_persists_and_dedupes(self):
        ids = self.seed(K1)
        rdist.tag_key(ids[0], add=["vip", "team"])
        rdist.tag_key(ids[0], add=["vip"])
        stored = self.raw()[0]
        self.assertEqual(stored["tags"], ["vip", "team"])

    def test_remove_tags(self):
        ids = self.seed(K1)
        rdist.tag_key(ids[0], add=["vip", "team", "old"])
        rdist.tag_key(ids[0], remove=["old"])
        self.assertEqual(self.raw()[0]["tags"], ["vip", "team"])

    def test_add_and_remove_together(self):
        ids = self.seed(K1, tags=["old"])
        rdist.tag_key(ids[0], add=["new"], remove=["old"])
        self.assertEqual(self.raw()[0]["tags"], ["new"])

    def test_dry_run_does_not_write(self):
        ids = self.seed(K1)
        result = rdist.tag_key(ids[0], add=["vip"], dry_run=True)
        self.assertTrue(result["changed"])
        self.assertEqual(self.raw()[0]["tags"], [])

    def test_no_change_reports_unchanged(self):
        ids = self.seed(K1, tags=["vip"])
        result = rdist.tag_key(ids[0], add=["vip"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.raw()[0]["tags"], ["vip"])

    def test_requires_add_or_rm(self):
        ids = self.seed(K1)
        with self.assertRaises(UsageError):
            rdist.tag_key(ids[0])

    def test_unknown_id_and_empty_id(self):
        self.seed(K1)
        with self.assertRaises(ConfigError):
            rdist.tag_key("nosuchid", add=["x"])
        with self.assertRaises(UsageError):
            rdist.tag_key("", add=["x"])

    def test_ambiguous_prefix_is_rejected(self):
        # 前缀匹配到多条时绝不猜测（极端情况下指纹可能撞前缀）
        ids = self.seed(K1)
        self.assertEqual(len(ids), 1)
        keys = self.raw()
        keys.append(importers.key_item({"id": ids[0][:4] + "zzzzzzzzzzzz", "key": K2}))
        rdist.save_keys({"keys": keys})
        with self.assertRaises(ConfigError):
            rdist.tag_key(ids[0][:4], add=["x"])


class AssignTests(Base):
    def test_assign_persists(self):
        ids = self.seed(K1)
        result = rdist.assign_key(ids[0], "张三的 Claude Code", note="笔记本")
        self.assertTrue(result["changed"])
        stored = self.raw()[0]
        self.assertEqual(stored["assigned_to"], "张三的 Claude Code")
        self.assertEqual(stored["notes"], "笔记本")

    def test_assign_requires_to(self):
        ids = self.seed(K1)
        with self.assertRaises(UsageError):
            rdist.assign_key(ids[0], "   ")

    def test_assign_dry_run(self):
        ids = self.seed(K1)
        rdist.assign_key(ids[0], "client-x", note="n", dry_run=True)
        self.assertEqual(self.raw()[0]["assigned_to"], "")

    def test_assign_no_change(self):
        ids = self.seed(K1)
        rdist.assign_key(ids[0], "client-x")
        result = rdist.assign_key(ids[0], "client-x")
        self.assertFalse(result["changed"])


class RemoveTests(Base):
    def test_remove_requires_confirmation(self):
        ids = self.seed(K1)
        with self.assertRaises(UsageError):
            rdist.remove_key(ids[0])
        self.assertEqual(len(self.raw()), 1)

    def test_remove_with_yes(self):
        ids = self.seed(K1, K2)
        result = rdist.remove_key(ids[0], yes=True)
        self.assertEqual(len(self.raw()), 1)
        self.assertEqual(result["removed"]["id"], ids[0])
        self.assertFalse(result["dry_run"])

    def test_remove_dry_run_keeps_record(self):
        ids = self.seed(K1)
        result = rdist.remove_key(ids[0], yes=True, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(self.raw()), 1)


# ---------------------------------------------------------------------------
# 导出编排
# ---------------------------------------------------------------------------
class ExportTests(Base):
    def test_ccswitch_stdout_has_no_plaintext(self):
        self.seed(K1)
        result = rdist.export(self.raw(), "ccswitch", app="claude")
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(K1, text)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["items"][0]["link"].startswith("ccswitch://v1/import?"))

    def test_ccswitch_out_file_is_0600_and_contains_plaintext(self):
        self.seed(K1)
        out = os.path.join(self.tmp, "ccswitch.json")
        result = rdist.export(self.raw(), "ccswitch", app="codex", out=out)
        self.assertEqual(result["path"], out)
        self.assertEqual(self.mode(out), 0o600)
        self.assertIsNone(result["text"], "写盘后不把明文回显到 stdout")
        body = open(out, encoding="utf-8").read()
        self.assertIn(K1, body)
        self.assertIn("wire_api", body)

    def test_reveal_flag_puts_plaintext_on_stdout(self):
        self.seed(K1)
        result = rdist.export(self.raw(), "ccswitch", app="claude", reveal=True)
        self.assertIn(K1, result["text"])

    def test_dry_run_writes_nothing(self):
        self.seed(K1)
        out = os.path.join(self.tmp, "out.json")
        result = rdist.export(self.raw(), "json", out=out, dry_run=True)
        self.assertFalse(os.path.exists(out))
        self.assertIsNone(result["path"])
        self.assertTrue(result["dry_run"])

    def test_env_json_csv_files_are_0600(self):
        self.seed(K1, K2)
        for target in ("env", "json", "csv"):
            out = os.path.join(self.tmp, f"out.{target}")
            rdist.export(self.raw(), target, out=out)
            self.assertEqual(self.mode(out), 0o600, f"{target} 文件必须 0600")
            content = open(out, encoding="utf-8").read()
            self.assertIn(K1, content)

    def test_json_stdout_masked(self):
        self.seed(K1)
        result = rdist.export(self.raw(), "json")
        payload = json.loads(result["text"])
        self.assertNotIn("key", payload["items"][0])
        self.assertNotIn(K1, result["text"])

    def test_tag_filter_limits_export(self):
        ids = self.seed(K1, K2)
        rdist.tag_key(ids[0], add=["vip"])
        selected = rdist.select_keys(tag="vip")
        self.assertEqual(len(selected), 1)
        result = rdist.export(selected, "json")
        self.assertEqual(result["count"], 1)

    def test_unknown_target(self):
        self.seed(K1)
        with self.assertRaises(ConfigError):
            rdist.export(self.raw(), "toml")

    def test_base_falls_back_to_registered_site(self):
        self.add_site()
        importers.apply_keys_import([{"key": K1, "site": "site-a"}],
                                    dry_run=False, cfg=self.cfg())
        keys = rdist.select_keys()
        self.assertEqual(keys[0]["base"], "")
        result = rdist.export(keys, "ccswitch", app="claude", cfg=self.cfg())
        self.assertIn("api.example.com", result["items"][0]["endpoint"])
        # 补 base 只在内存里做，不回写分发库
        self.assertEqual(self.raw()[0]["base"], "")


class NewApiExportTests(Base):
    def test_requires_site(self):
        self.seed(K1)
        with self.assertRaises(ConfigError):
            rdist.export(self.raw(), "newapi", dry_run=True)

    def test_requires_token(self):
        self.seed(K1)
        self.add_site(token="")
        with self.assertRaises(ConfigError):
            rdist.export(self.raw(), "newapi", site="site-a", dry_run=True)

    def test_dry_run_does_not_touch_network(self):
        self.seed(K1)
        self.add_site()
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(method)
            return FakeResponse(200, {"success": True})

        result = rdist.export(self.raw(), "newapi", site="site-a", yes=True,
                              dry_run=True, transport=transport, cfg=self.cfg())
        self.assertEqual(calls, [])
        self.assertTrue(result["dry_run"])
        self.assertTrue(all(i["ok"] for i in result["items"]))

    def test_real_write_needs_yes(self):
        self.seed(K1)
        self.add_site()
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(method)
            return FakeResponse(200, {"success": True, "message": "",
                                      "data": {"items": []}})

        result = rdist.export(self.raw(), "newapi", site="site-a", transport=transport,
                              cfg=self.cfg())
        self.assertTrue(result["dry_run"], "没有 --yes 时必须是试运行")
        self.assertEqual(calls, [])

    def test_write_creates_channel_and_looks_up_id(self):
        self.seed(K1)
        self.add_site()
        state = {"created": False}

        name = "relayctl-" + self.raw()[0]["id"]

        def transport(method, url, headers, body, timeout):
            if method == "POST":
                state["created"] = True
                return FakeResponse(200, {"success": True, "message": ""})
            # 创建前查不到 -> create；创建后回查拿到 id
            items = [{"id": 77, "name": name}] if state["created"] else []
            return FakeResponse(200, {"success": True, "message": "",
                                      "data": {"items": items}})

        result = rdist.export(self.raw(), "newapi", site="site-a", yes=True,
                              transport=transport, cfg=self.cfg())
        item = result["items"][0]
        self.assertTrue(item["ok"])
        self.assertEqual(item["action"], "create")
        self.assertEqual(item["channel_id"], 77)
        # 结果里绝不含明文 Key / 令牌
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(K1, blob)
        self.assertNotIn(TOKEN, blob)

    def test_business_failure_is_reported_not_swallowed(self):
        self.seed(K1)
        self.add_site()

        def transport(method, url, headers, body, timeout):
            if method == "GET":
                return FakeResponse(200, {"success": True, "message": "",
                                          "data": {"items": []}})
            return FakeResponse(200, {"success": False, "message": "渠道已存在"})

        result = rdist.export(self.raw(), "newapi", site="site-a", yes=True,
                              transport=transport, cfg=self.cfg())
        self.assertEqual(result["failed"], 1)
        self.assertIn("渠道已存在", result["items"][0]["message"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class CliTests(Base):
    def test_dist_list_and_json(self):
        self.seed(K1)
        rc, out, _ = self.run_cli("dist", "list")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn(importers.mask_key(K1), out)
        self.assertNotIn(K1, out)
        rc, out, _ = self.run_cli("dist", "list", "--json")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(json.loads(out)["data"]["count"], 1)

    def test_dist_tag_and_assign_persist(self):
        ids = self.seed(K1)
        rc, out, _ = self.run_cli("dist", "tag", ids[0], "--add", "vip,team")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(self.raw()[0]["tags"], ["vip", "team"])
        rc, out, _ = self.run_cli("dist", "assign", ids[0], "--to", "小王", "--note", "备用")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(self.raw()[0]["assigned_to"], "小王")
        self.assertEqual(self.raw()[0]["notes"], "备用")

    def test_dist_rm_requires_yes(self):
        ids = self.seed(K1)
        rc, out, _ = self.run_cli("dist", "rm", ids[0])
        self.assertEqual(rc, EXIT_USAGE)
        self.assertEqual(len(self.raw()), 1)
        rc, out, _ = self.run_cli("dist", "rm", ids[0], "--yes")
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(len(self.raw()), 0)

    def test_dist_export_ccswitch_to_file(self):
        self.seed(K1)
        out = os.path.join(self.tmp, "cc.json")
        rc, stdout, _ = self.run_cli("dist", "export", "--target", "ccswitch",
                                     "--app", "all", "--out", out)
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(self.mode(out), 0o600)
        self.assertNotIn(K1, stdout, "stdout 绝不出现明文 Key")
        self.assertIn(K1, open(out, encoding="utf-8").read())

    def test_dist_export_dry_run_writes_nothing(self):
        self.seed(K1)
        out = os.path.join(self.tmp, "out.json")
        rc, stdout, _ = self.run_cli("dist", "export", "--target", "json",
                                     "--out", out, "--dry-run")
        self.assertEqual(rc, EXIT_OK)
        self.assertFalse(os.path.exists(out))

    def test_dist_export_newapi_dry_run_offline(self):
        self.seed(K1)
        self.add_site()
        rc, stdout, _ = self.run_cli("dist", "export", "--target", "newapi",
                                     "--site", "site-a")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("试运行", stdout)
        self.assertNotIn(K1, stdout)
        self.assertNotIn(TOKEN, stdout)

    def test_dist_export_newapi_missing_site(self):
        self.seed(K1)
        rc, stdout, _ = self.run_cli("dist", "export", "--target", "newapi")
        self.assertEqual(rc, EXIT_USAGE)

    def test_dist_export_failures_return_exit_1(self):
        self.seed(K1)
        self.add_site()
        from relayctl import distribution as mod

        def transport(method, url, headers, body, timeout):
            if method == "GET":
                return FakeResponse(200, {"success": True, "message": "",
                                          "data": {"items": []}})
            return FakeResponse(200, {"success": False, "message": "渠道已存在"})

        with mock.patch.object(mod.exporters, "newapi_export",
                               wraps=mod.exporters.newapi_export):
            result = mod.export(self.raw(), "newapi", site="site-a", yes=True,
                                transport=transport, cfg=self.cfg())
        self.assertEqual(result["failed"], 1)
        lines = mod.export_lines(result)
        self.assertTrue(any("[!]" in line for line in lines))

    def test_dist_without_subcommand_prints_help(self):
        rc, out, _ = self.run_cli("dist")
        self.assertEqual(rc, EXIT_USAGE)
        self.assertIn("usage", out.lower())

    def test_dist_export_csv_to_stdout_masked(self):
        self.seed(K1, K2)
        rc, out, _ = self.run_cli("dist", "export", "--target", "csv")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("id,key_masked", out)
        self.assertNotIn(K1, out)
