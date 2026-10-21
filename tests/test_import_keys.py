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

import yaml

from relayctl import config, importers, paths
from relayctl.errors import EXIT_FAIL, EXIT_OK

_HERE = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.join(os.path.dirname(_HERE), "bin", "relayctl")


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("relayctl_cli_import_keys", CLI_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CLI = _load_cli()

K1 = "sk-AAAAAAAAAAAAAAAAAAAA"
K2 = "sk-BBBBBBBBBBBBBBBBBBBB"
K3 = "sk-CCCCCCCCCCCCCCCCCCCC"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()
        config.init_layout()

    def tearDown(self):
        self._env.stop()

    def run_cli(self, *argv):
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = CLI.main(list(argv))
        return rc, buf.getvalue(), err.getvalue()

    def add_site(self, name, base):
        config.save_site(name, {
            "name": name, "base": base, "console_url": base, "panel": "newapi",
            "adapter": "newapi", "script": None, "auth": dict(config.DEFAULT_AUTH),
            "docs": dict(config.DEFAULT_DOCS), "proxy": "", "refresh_interval": "",
            "tags": [], "status": config.STATUS_ACTIVE, "parked_reason": "",
        })

    def dist(self):
        return paths.read_yaml(paths.keys_dist_path(), default=None) or {}


class ParseKeysTxtTests(Base):
    def test_one_key_per_line(self):
        cands, errors = importers.parse_keys(f"{K1}\n{K2}\n", fmt="txt")
        self.assertEqual(errors, [])
        self.assertEqual([c["key"] for c in cands], [K1, K2])

    def test_comments_and_blank_lines(self):
        cands, errors = importers.parse_keys(f"# 注释\n\n{K1}\n", fmt="txt")
        self.assertEqual(errors, [])
        self.assertEqual(len(cands), 1)

    def test_optional_inline_fields(self):
        cands, _ = importers.parse_keys(f"{K1} site-a 主力 备注内容", fmt="txt")
        self.assertEqual(cands[0]["site"], "site-a")
        self.assertEqual(cands[0]["tags"], ["主力"])
        self.assertEqual(cands[0]["notes"], "备注内容")

    def test_line_without_key_is_error(self):
        cands, errors = importers.parse_keys("not-a-key\n", fmt="txt")
        self.assertEqual(cands, [])
        self.assertEqual(len(errors), 1)

    def test_bom(self):
        cands, errors = importers.parse_keys("\ufeff" + K1, fmt="txt")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["key"], K1)


class ParseKeysCsvTests(Base):
    def test_chinese_headers(self):
        text = f"Key,站点,分类,备注\n{K1},site-a,主力,中文备注\n"
        cands, errors = importers.parse_keys(text, fmt="csv")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["key"], K1)
        self.assertEqual(cands[0]["site"], "site-a")
        self.assertEqual(cands[0]["tags"], ["主力"])
        self.assertEqual(cands[0]["notes"], "中文备注")

    def test_english_headers(self):
        text = f"key,site_name,category,notes,base\n{K2},site-b,vip,note,https://b.example.com/v1\n"
        cands, _ = importers.parse_keys(text, fmt="csv")
        self.assertEqual(cands[0]["site"], "site-b")
        self.assertEqual(cands[0]["tags"], ["vip"])
        self.assertEqual(cands[0]["base"], "https://b.example.com")

    def test_missing_key_row_is_error(self):
        cands, errors = importers.parse_keys("key,站点\n,site-a\n", fmt="csv")
        self.assertEqual(cands, [])
        self.assertEqual(len(errors), 1)


class ParseKeysStructuredTests(Base):
    def test_json_generic_list(self):
        data = [{"key": K1, "base": "https://a.example.com", "site": "site-a",
                 "category": "主力", "notes": "n1"}]
        cands, errors = importers.parse_keys(json.dumps(data), fmt="json")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["key"], K1)
        self.assertEqual(cands[0]["tags"], ["主力"])

    def test_yaml_list(self):
        text = f"- key: {K1}\n  base: https://a.example.com\n  group: default\n"
        cands, errors = importers.parse_keys(text, fmt="yaml")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["key"], K1)
        self.assertEqual(cands[0]["groups"], ["default"])

    def test_cc_switch_export(self):
        data = {
            "version": 2,
            "providers": [
                {"id": "p1", "app": "claude", "provider": "my-relay",
                 "name": "My Relay", "endpoint": "https://cc.example.com/v1",
                 "apiKey": K1,
                 "config": {"env": {"ANTHROPIC_BASE_URL": "https://cc.example.com"}}},
            ],
        }
        self.assertEqual(importers.detect_format(json.dumps(data), kind="keys"), "ccswitch")
        cands, errors = importers.parse_keys(json.dumps(data), fmt="ccswitch")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["key"], K1)
        self.assertEqual(cands[0]["base"], "https://cc.example.com")
        self.assertEqual(cands[0]["site"], "my-relay")
        self.assertIn("claude", cands[0]["tags"])

    def test_cc_switch_top_level_list(self):
        data = [{"app": "codex", "endpoint": "https://cc2.example.com", "apiKey": K2}]
        cands, _ = importers.parse_keys(json.dumps(data))
        self.assertEqual(cands[0]["key"], K2)
        self.assertEqual(cands[0]["tags"], ["codex"])

    def test_new_api_channel_export(self):
        data = {"success": True, "data": {"items": [
            {"id": 11, "name": "渠道A", "key": K1, "base_url": "https://up.example.com/v1",
             "group": "default", "models": "gpt-4o,claude"},
        ]}}
        self.assertEqual(importers.detect_format(json.dumps(data), kind="keys"), "newapi")
        cands, errors = importers.parse_keys(json.dumps(data), fmt="newapi")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["key"], K1)
        self.assertEqual(cands[0]["base"], "https://up.example.com")
        self.assertEqual(cands[0]["groups"], ["default"])
        self.assertEqual(cands[0]["models"], ["gpt-4o", "claude"])

    def test_new_api_items_and_channels_containers(self):
        data = {"items": [{"key": K2, "base_url": "https://x.example.com",
                           "groups": ["pro", "vip"], "models": ["gpt-4o"]}]}
        cands, _ = importers.parse_keys(json.dumps(data))
        self.assertEqual(cands[0]["groups"], ["pro", "vip"])
        self.assertEqual(cands[0]["models"], ["gpt-4o"])

        data2 = {"channels": [{"key": K3, "base_url": "https://y.example.com"}]}
        cands2, _ = importers.parse_keys(json.dumps(data2), fmt="newapi")
        self.assertEqual(cands2[0]["key"], K3)

    def test_alias_variants_not_tied_to_one_version(self):
        data = [{"api_key": K1, "baseUrl": "https://v.example.com/v1", "app": "claude"},
                {"apiKey": K2, "url": "https://w.example.com", "provider": "relay-x"},
                {"secretKey": K3, "endpoint": "https://z.example.com", "category": "备用"}]
        cands, errors = importers.parse_keys(json.dumps(data))
        self.assertEqual(errors, [])
        by_key = {c["key"]: c for c in cands}
        self.assertEqual(by_key[K1]["base"], "https://v.example.com")
        self.assertEqual(by_key[K1]["tags"], ["claude"])
        self.assertEqual(by_key[K2]["base"], "https://w.example.com")
        self.assertEqual(by_key[K2]["site"], "relay-x")
        self.assertEqual(by_key[K3]["tags"], ["备用"])

    def test_list_of_plain_strings(self):
        cands, errors = importers.parse_keys(json.dumps([K1, K2]), fmt="json")
        self.assertEqual(errors, [])
        self.assertEqual([c["key"] for c in cands], [K1, K2])


class ApplyKeysDistTests(Base):
    def _apply(self, text, fmt="txt", dry_run=False):
        cands, errors = importers.parse_keys(text, fmt=fmt)
        return importers.apply_keys_import(cands, dry_run=dry_run,
                                           cfg=config.load_config(), parse_errors=errors)

    def test_dist_schema_and_permissions(self):
        self._apply(K1)
        path = paths.keys_dist_path()
        self.assertTrue(os.path.exists(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(paths.dist_dir()).st_mode), 0o700)
        data = self.dist()
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["keys"]), 1)
        item = data["keys"][0]
        for field in ("id", "key", "source_site", "base", "tags", "models", "groups",
                      "assigned_to", "notes", "status"):
            self.assertIn(field, item)
        self.assertEqual(item["key"], K1)  # 明文按需求保存（分发 Key 库）
        self.assertEqual(item["id"], importers.fingerprint(K1))
        self.assertEqual(len(item["id"]), 16)
        self.assertEqual(item["status"], "active")

    def test_dedupe_by_fingerprint(self):
        self._apply(K1)
        result = self._apply(f"{K1}\n{K2}\n")
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(len(self.dist()["keys"]), 2)

    def test_base_matches_registered_site(self):
        self.add_site("site-a", "https://a.example.com")
        result = self._apply(f"key,base\n{K1},https://a.example.com/v1\n", fmt="csv")
        self.assertEqual(result["added"][0]["source_site"], "site-a")

    def test_explicit_site_wins(self):
        self.add_site("site-a", "https://a.example.com")
        result = self._apply(f"key,site,base\n{K1},site-b,https://a.example.com\n", fmt="csv")
        self.assertEqual(result["added"][0]["source_site"], "site-b")
        self.assertEqual(result["added"][0]["base"], "https://a.example.com")

    def test_unknown_base_keeps_empty_site(self):
        result = self._apply(f"key,base\n{K1},https://unknown.example.com\n", fmt="csv")
        self.assertEqual(result["added"][0]["source_site"], "")

    def test_dry_run_writes_nothing(self):
        result = self._apply(K1, dry_run=True)
        self.assertEqual(len(result["added"]), 1)
        self.assertTrue(result["dry_run"])
        self.assertFalse(os.path.exists(paths.keys_dist_path()))

    def test_output_never_contains_plaintext(self):
        result = self._apply(f"{K1}\n{K2}\n")
        text = json.dumps(result, ensure_ascii=False)
        for key in (K1, K2):
            self.assertNotIn(key, text)
        self.assertIn("key_masked", text)
        for line in importers.keys_report_lines(result):
            self.assertNotIn(K1, line)
            self.assertNotIn(K2, line)

    def test_error_text_is_scrubbed(self):
        secret = "sk-SECRETERROR1234567"
        cands, errors = importers.parse_keys(secret, fmt="txt")
        with mock.patch.object(importers, "match_site_by_base",
                               side_effect=RuntimeError(f"匹配失败 {secret}")):
            result = importers.apply_keys_import(cands, cfg=config.load_config(),
                                                 parse_errors=errors)
        self.assertEqual(len(result["failed"]), 1)
        joined = json.dumps(result["failed"], ensure_ascii=False)
        self.assertNotIn(secret, joined)
        self.assertIn("匹配失败", joined)

    def test_partial_failure_keeps_other_keys(self):
        cands, errors = importers.parse_keys(f"{K1}\n{K2}\n", fmt="txt")
        calls = {"n": 0}

        def flaky(data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("磁盘写入失败")
            return importers.save_keys_dist.__wrapped__(data) if False else None

        orig = importers.save_keys_dist

        def wrapper(data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("磁盘写入失败")
            return orig(data)

        with mock.patch.object(importers, "save_keys_dist", wrapper):
            result = importers.apply_keys_import(cands, cfg=config.load_config(),
                                                 parse_errors=errors)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(len(self.dist()["keys"]), 1)

    def test_read_modify_write_uses_lock(self):
        self._apply(K1)
        with mock.patch.object(paths, "file_lock") as lock:
            lock.return_value.__enter__ = mock.Mock(return_value=None)
            lock.return_value.__exit__ = mock.Mock(return_value=False)
            self._apply(K2)
        lock.assert_called()


class ImportKeysCliTests(Base):
    def _stdin(self, text):
        return mock.patch.object(CLI.importers.sys, "stdin", io.StringIO(text))

    def test_import_keys_from_stdin(self):
        with self._stdin(f"{K1}\n"):
            rc, out, _ = self.run_cli("import", "keys", "-")
        self.assertEqual(rc, EXIT_OK, out)
        self.assertEqual(len(self.dist()["keys"]), 1)
        self.assertNotIn(K1, out)
        self.assertIn(importers.fingerprint(K1), out)

    def test_import_keys_json_has_no_plaintext(self):
        path = os.path.join(self.tmp, "keys.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"providers": [{"app": "claude", "apiKey": K2,
                                      "endpoint": "https://cc.example.com/v1"}]}, f)
        rc, out, _ = self.run_cli("import", "keys", path, "--json")
        self.assertEqual(rc, EXIT_OK, out)
        payload = json.loads(out)
        self.assertEqual(len(payload["data"]["added"]), 1)
        self.assertNotIn(K2, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["data"]["added"][0]["id"], importers.fingerprint(K2))

    def test_dry_run_cli_writes_nothing(self):
        path = os.path.join(self.tmp, "k.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(K1 + "\n")
        rc, out, _ = self.run_cli("import", "keys", path, "--dry-run")
        self.assertEqual(rc, EXIT_OK, out)
        self.assertFalse(os.path.exists(paths.keys_dist_path()))
        self.assertIn("试运行", out)

    def test_bad_line_exit_code(self):
        path = os.path.join(self.tmp, "bad.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("no-key-here\n")
        rc, out, _ = self.run_cli("import", "keys", path, "--json")
        self.assertEqual(rc, EXIT_FAIL)
        self.assertEqual(len(json.loads(out)["data"]["failed"]), 1)

    def test_second_import_is_deduped(self):
        path = os.path.join(self.tmp, "k.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{K1}\n{K2}\n")
        self.run_cli("import", "keys", path)
        rc, out, _ = self.run_cli("import", "keys", path, "--json")
        self.assertEqual(rc, EXIT_OK, out)
        payload = json.loads(out)
        self.assertEqual(len(payload["data"]["skipped"]), 2)
        self.assertEqual(len(self.dist()["keys"]), 2)


if __name__ == "__main__":
    unittest.main()
