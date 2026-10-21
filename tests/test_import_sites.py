import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import yaml

from relayctl import config, importers, paths, probe as rprobe
from relayctl.adapters import newapi as newapi_mod
from relayctl.errors import EXIT_FAIL, EXIT_OK, EXIT_USAGE

_HERE = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.join(os.path.dirname(_HERE), "bin", "relayctl")


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("relayctl_cli_import_sites", CLI_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CLI = _load_cli()

TOKEN = "sk-IMPORT-TOKEN-0123456789"


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


class FakeProbe:
    """假的公开探测：按 base 返回预设结果，并记录调用。"""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default or {"ok": False, "panel": "custom", "adapter": "newapi",
                                   "version": "", "system_name": "", "error": "未识别"}
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        item = self.mapping.get(url, self.default)
        return dict(item)


def ok_probe(panel="newapi", version="v0.9.0", system_name="New API"):
    return {"ok": True, "panel": panel, "adapter": panel if panel in ("newapi", "oneapi") else "newapi",
            "version": version, "system_name": system_name, "error": None}


class ParseTxtTests(Base):
    def test_txt_parses_name_base_token_uid(self):
        text = (
            "# 注释行\n"
            "\n"
            f"site-a https://a.example.com/v1 {TOKEN} 42\n"
            "site-b https://b.example.com sk-BBB\n"
        )
        cands, errors = importers.parse_sites(text, fmt="txt")
        self.assertEqual(errors, [])
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[0]["name"], "site-a")
        self.assertEqual(cands[0]["base"], "https://a.example.com")  # 去掉末尾 /v1
        self.assertEqual(cands[0]["token"], TOKEN)
        self.assertEqual(cands[0]["user_id"], "42")
        self.assertEqual(cands[1]["user_id"], None)

    def test_txt_bad_lines_are_errors(self):
        cands, errors = importers.parse_sites("orphan\n", fmt="txt")
        self.assertEqual(cands, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("字段不足", errors[0]["error"])

    def test_bom_is_stripped(self):
        text = "\ufeff" + f"site-a https://a.example.com {TOKEN}\n"
        cands, errors = importers.parse_sites(text)
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["name"], "site-a")

    def test_missing_base_is_failed(self):
        cands, errors = importers.parse_sites("site-a\n", fmt="txt")
        self.assertEqual(cands, [])
        self.assertEqual(len(errors), 1)

    def test_non_url_base_is_failed(self):
        cands, errors = importers.parse_sites("site-a ftp://x.example.com\n", fmt="txt")
        self.assertEqual(cands, [])
        self.assertIn("http", errors[0]["error"])


class ParseCsvTests(Base):
    def test_english_headers(self):
        text = "name,base,token,user_id,tags,notes\n" \
               f"site-a,https://a.example.com,{TOKEN},7,vip,备注A\n"
        cands, errors = importers.parse_sites(text, fmt="csv")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["name"], "site-a")
        self.assertEqual(cands[0]["token"], TOKEN)
        self.assertEqual(cands[0]["user_id"], "7")
        self.assertEqual(cands[0]["tags"], ["vip"])
        self.assertEqual(cands[0]["docs"]["notes"], "备注A")

    def test_chinese_headers(self):
        text = "站点名称,地址,令牌,用户ID,分类,登录方式,令牌获取,脚本说明,备注\n" \
               f"cn-site,https://cn.example.com,{TOKEN},3,主力,控制台登录,个人设置,无,中文备注\n"
        cands, errors = importers.parse_sites(text, fmt="csv")
        self.assertEqual(errors, [])
        cand = cands[0]
        self.assertEqual(cand["name"], "cn-site")
        self.assertEqual(cand["base"], "https://cn.example.com")
        self.assertEqual(cand["token"], TOKEN)
        self.assertEqual(cand["user_id"], "3")
        self.assertEqual(cand["tags"], ["主力"])
        self.assertEqual(cand["docs"]["login_method"], "控制台登录")
        self.assertEqual(cand["docs"]["token_howto"], "个人设置")
        self.assertEqual(cand["docs"]["script_howto"], "无")
        self.assertEqual(cand["docs"]["notes"], "中文备注")

    def test_non_ascii_name_falls_back_to_host_slug(self):
        text = "站点名称,地址,令牌\n" \
               f"中文站,https://cn.example.com,{TOKEN}\n"
        cands, errors = importers.parse_sites(text, fmt="csv")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["name"], "cn-example-com")

    def test_headerless_csv_is_positional(self):
        text = f"site-a,https://a.example.com,{TOKEN},7\n"
        cands, errors = importers.parse_sites(text, fmt="csv")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["name"], "site-a")
        self.assertEqual(cands[0]["token"], TOKEN)

    def test_bad_row_keeps_others(self):
        text = "name,base,token\n" \
               f"good,https://g.example.com,{TOKEN}\n" \
               ",https://nobody.example.com,sk-x\n"
        cands, errors = importers.parse_sites(text, fmt="csv")
        self.assertEqual(len(cands), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("站点名", errors[0]["error"])


class ParseStructuredTests(Base):
    def test_json_list(self):
        data = [{"name": "j1", "base": "https://j1.example.com", "token": TOKEN,
                 "user_id": 5, "panel": "newapi", "tags": ["a", "b"]}]
        cands, errors = importers.parse_sites(json.dumps(data), fmt="json")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["name"], "j1")
        self.assertEqual(cands[0]["tags"], ["a", "b"])

    def test_json_single_object(self):
        data = {"name": "solo", "base": "https://solo.example.com", "token": TOKEN}
        cands, _ = importers.parse_sites(json.dumps(data), fmt="json")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["name"], "solo")

    def test_json_sites_mapping(self):
        data = {"sites": {"m1": {"base": "https://m1.example.com", "token": TOKEN},
                          "m2": {"base": "https://m2.example.com"}}}
        cands, _ = importers.parse_sites(json.dumps(data), fmt="json")
        self.assertEqual(sorted(c["name"] for c in cands), ["m1", "m2"])
        by_name = {c["name"]: c for c in cands}
        self.assertEqual(by_name["m1"]["token"], TOKEN)

    def test_yaml_list(self):
        text = (
            "- name: y1\n"
            "  base: https://y1.example.com\n"
            f"  token: {TOKEN}\n"
            "  notes: 通过 YAML 导入\n"
        )
        cands, errors = importers.parse_sites(text, fmt="yaml")
        self.assertEqual(errors, [])
        self.assertEqual(cands[0]["docs"]["notes"], "通过 YAML 导入")

    def test_auto_detects_json_yaml_csv_txt(self):
        self.assertEqual(importers.detect_format(json.dumps([{"name": "a"}])), "json")
        self.assertEqual(importers.detect_format("- name: a\n  base: https://a\n"), "yaml")
        self.assertEqual(importers.detect_format("name,base,token\na,https://a,sk-1"), "csv")
        self.assertEqual(importers.detect_format("a https://a.example.com sk-1"), "txt")

    def test_unknown_format_is_usage_error(self):
        with self.assertRaises(Exception):
            importers.parse_sites("x", fmt="toml")


class BuildConfigTests(Base):
    def test_docs_always_complete(self):
        cand, _ = importers.parse_sites(f"d1 https://d1.example.com {TOKEN}", fmt="txt")
        site = importers.build_site_config(cand[0], panel="newapi", adapter="newapi")
        self.assertEqual(sorted(site["docs"]), sorted(config.DOCS_FIELDS))
        for field in config.DOCS_FIELDS:
            self.assertIn(field, site["docs"])
        self.assertEqual(site["auth"]["type"], "access_token")
        self.assertEqual(site["auth"]["access_token"], TOKEN)
        self.assertEqual(site["status"], config.STATUS_PARKED)

    def test_no_token_means_auth_none(self):
        cand, _ = importers.parse_sites("d2 https://d2.example.com", fmt="txt")
        site = importers.build_site_config(cand[0])
        self.assertEqual(site["auth"]["type"], "none")

    def test_input_panel_and_adapter_win(self):
        cand, _ = importers.parse_sites(
            '{"name":"p1","base":"https://p1.example.com","panel":"oneapi","adapter":"oneapi"}',
            fmt="json")
        site = importers.build_site_config(cand[0], panel="newapi", adapter="newapi")
        self.assertEqual(site["panel"], "oneapi")
        self.assertEqual(site["adapter"], "oneapi")

    def test_unrecognized_probe_falls_back_to_custom_newapi(self):
        cand, _ = importers.parse_sites(f"c1 https://c1.example.com {TOKEN}", fmt="txt")
        site = importers.build_site_config(cand[0], panel="custom", adapter="newapi")
        self.assertEqual(site["panel"], "custom")
        self.assertEqual(site["adapter"], "newapi")

    def test_console_url_defaults_to_base(self):
        cand, _ = importers.parse_sites(f"c2 https://c2.example.com {TOKEN}", fmt="txt")
        self.assertEqual(importers.build_site_config(cand[0])["console_url"],
                         "https://c2.example.com")


class ApplySitesTests(Base):
    def _cands(self, text, fmt="txt"):
        cands, errors = importers.parse_sites(text, fmt=fmt)
        return cands, errors

    def test_active_when_public_and_auth_ok(self):
        cands, errors = self._cands(f"ok-site https://ok.example.com {TOKEN}")
        probe = FakeProbe({"https://ok.example.com": ok_probe()})
        result = importers.apply_sites_import(
            cands, cfg=config.load_config(), parse_errors=errors,
            probe_url_fn=probe, auth_probe_fn=lambda site, cfg, name: {"auth_ok": True})
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["added"][0]["status"], config.STATUS_ACTIVE)
        self.assertEqual(result["added"][0]["panel"], "newapi")
        self.assertEqual(probe.calls, ["https://ok.example.com"])
        site = config.load_site("ok-site")
        self.assertEqual(site["status"], config.STATUS_ACTIVE)
        self.assertEqual(site["parked_reason"], "")
        self.assertEqual(site["auth"]["access_token"], TOKEN)

    def test_parked_when_auth_fails(self):
        cands, errors = self._cands(f"bad-site https://bad.example.com {TOKEN}")
        probe = FakeProbe({"https://bad.example.com": ok_probe()})
        result = importers.apply_sites_import(
            cands, cfg=config.load_config(), parse_errors=errors, probe_url_fn=probe,
            auth_probe_fn=lambda site, cfg, name: {"auth_ok": False, "auth_msg": "HTTP 401"})
        self.assertEqual(result["added"][0]["status"], config.STATUS_PARKED)
        self.assertIn("401", result["added"][0]["parked_reason"])
        self.assertEqual(config.load_site("bad-site")["status"], config.STATUS_PARKED)
        self.assertTrue(config.load_site("bad-site")["parked_reason"])

    def test_parked_when_probe_unrecognized(self):
        cands, errors = self._cands(f"unk-site https://unk.example.com {TOKEN}")
        probe = FakeProbe({})
        result = importers.apply_sites_import(
            cands, cfg=config.load_config(), parse_errors=errors, probe_url_fn=probe,
            auth_probe_fn=lambda site, cfg, name: {"auth_ok": True})
        entry = result["added"][0]
        self.assertEqual(entry["status"], config.STATUS_PARKED)
        self.assertEqual(entry["panel"], "custom")
        self.assertEqual(entry["adapter"], "newapi")
        self.assertIn("公开探测", entry["parked_reason"])

    def test_network_exception_still_parks(self):
        cands, errors = self._cands(f"net-site https://net.example.com {TOKEN}")

        def boom(site, cfg, name):
            raise RuntimeError("connection reset")

        result = importers.apply_sites_import(
            cands, cfg=config.load_config(), parse_errors=errors,
            probe_url_fn=FakeProbe({"https://net.example.com": ok_probe()}),
            auth_probe_fn=boom)
        self.assertEqual(result["failed"], [])
        self.assertIn("connection reset", result["added"][0]["parked_reason"])

    def test_partial_failure_does_not_stop_others(self):
        text = (
            f"s1 https://s1.example.com {TOKEN}\n"
            f"s2 https://s2.example.com {TOKEN}\n"
        )
        cands, errors = self._cands(text)
        probe = FakeProbe({"https://s1.example.com": ok_probe(),
                           "https://s2.example.com": ok_probe()})

        def auth(site, cfg, name):
            if name == "s1":
                raise RuntimeError("boom")
            return {"auth_ok": True}

        result = importers.apply_sites_import(
            cands, cfg=config.load_config(), parse_errors=errors,
            probe_url_fn=probe, auth_probe_fn=auth)
        self.assertEqual(len(result["added"]), 2)
        self.assertEqual(config.load_site("s2")["status"], config.STATUS_ACTIVE)
        self.assertEqual(config.load_site("s1")["status"], config.STATUS_PARKED)

    def test_existing_site_is_skipped_not_overwritten(self):
        config.save_site("dup", {
            "name": "dup", "base": "https://old.example.com", "console_url": "",
            "panel": "custom", "adapter": "newapi", "script": None,
            "auth": dict(config.DEFAULT_AUTH), "docs": dict(config.DEFAULT_DOCS),
            "proxy": "", "refresh_interval": "", "tags": [], "status": config.STATUS_ACTIVE,
            "parked_reason": ""})
        cands, errors = self._cands(f"dup https://new.example.com {TOKEN}")
        result = importers.apply_sites_import(
            cands, cfg=config.load_config(), parse_errors=errors,
            probe_url_fn=FakeProbe(), auth_probe_fn=lambda *a: {"auth_ok": True})
        self.assertEqual(result["added"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(config.load_site("dup")["base"], "https://old.example.com")

    def test_duplicate_names_in_one_batch(self):
        cands, errors = self._cands(
            f"d1 https://d1.example.com {TOKEN}\nd1 https://d1b.example.com {TOKEN}")
        result = importers.apply_sites_import(
            cands, cfg=config.load_config(), parse_errors=errors,
            probe_url_fn=FakeProbe({"https://d1.example.com": ok_probe()}),
            auth_probe_fn=lambda *a: {"auth_ok": True}, dry_run=True)
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(len(result["skipped"]), 1)

    def test_dry_run_never_writes(self):
        cands, errors = self._cands(f"dry-site https://dry.example.com {TOKEN}")
        probe = FakeProbe({"https://dry.example.com": ok_probe()})
        result = importers.apply_sites_import(
            cands, dry_run=True, cfg=config.load_config(), parse_errors=errors,
            probe_url_fn=probe, auth_probe_fn=lambda *a: {"auth_ok": True})
        self.assertEqual(len(result["added"]), 1)
        self.assertTrue(result["dry_run"])
        self.assertEqual(probe.calls, ["https://dry.example.com"])  # 允许探测
        self.assertFalse(os.path.exists(paths.site_path("dry-site")))
        self.assertEqual(config.list_site_names(), [])

    def test_site_files_are_isolated_per_site(self):
        text = f"i1 https://i1.example.com {TOKEN}\ni2 https://i2.example.com {TOKEN}"
        cands, errors = self._cands(text)
        importers.apply_sites_import(
            cands, cfg=config.load_config(), parse_errors=errors,
            probe_url_fn=FakeProbe({"https://i1.example.com": ok_probe(),
                                    "https://i2.example.com": ok_probe()}),
            auth_probe_fn=lambda *a: {"auth_ok": True})
        self.assertEqual(sorted(config.list_site_names()), ["i1", "i2"])
        p1, p2 = paths.site_path("i1"), paths.site_path("i2")
        self.assertNotEqual(p1, p2)
        with open(p2, "w", encoding="utf-8") as f:
            f.write("name: i2\nbase: [broken\n")
        sites, errs = config.load_sites()
        self.assertIn("i1", sites)
        self.assertEqual(len(errs), 1)


    def test_write_error_message_is_scrubbed(self):
        cands, errors = self._cands(f"w-site https://w.example.com {TOKEN}")
        with mock.patch.object(config, "save_site",
                               side_effect=RuntimeError(f"写入失败 {TOKEN}")):
            result = importers.apply_sites_import(
                cands, cfg=config.load_config(), parse_errors=errors,
                probe_url_fn=FakeProbe({"https://w.example.com": ok_probe()}),
                auth_probe_fn=lambda *a: {"auth_ok": True})
        self.assertEqual(len(result["failed"]), 1)
        joined = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(TOKEN, joined)
        self.assertIn("写入失败", joined)


class ImportSitesEndToEndTests(Base):
    """不替换 probe/auth 函数：直接给 New API 适配器喂假 HTTP，验证整条链路。"""

    ALLOWED_PATHS = ("/api/status", "/api/v1/status", "/api/user/self")

    def _install_http(self, status_data, self_status=200, self_payload=None):
        client = FakeProbeClient(status_data, self_status, self_payload or {})

        def factory(base_url, proxy=None, auth=None):
            client.base_url = base_url
            client.auth = auth or {}
            return client

        return client, [
            mock.patch.object(newapi_mod, "HttpClient", factory),
            mock.patch.object(rprobe, "HttpClient", factory),
        ]

    def test_end_to_end_active(self):
        client, patches = self._install_http(
            {"success": True, "data": {"system_name": "New API", "version": "v0.9.0"}},
            self_payload={"success": True, "data": {"username": "u", "group": "default"}})
        cands, errors = importers.parse_sites(f"e2e https://e2e.example.com {TOKEN}", fmt="txt")
        with patches[0], patches[1]:
            result = importers.apply_sites_import(cands, cfg=config.load_config(),
                                                  parse_errors=errors)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["added"][0]["status"], config.STATUS_ACTIVE)
        self.assertEqual(result["added"][0]["panel"], "newapi")
        self.assertEqual(result["added"][0]["system_name"], "New API")
        for path in client.calls:
            self.assertIn(path, self.ALLOWED_PATHS)
            self.assertNotIn("pricing", path.lower())
        # 公开探测不带任何凭据；鉴权探测才带令牌
        public = [c for c in client.calls_meta if c["path"] == "/api/status"]
        authed = [c for c in client.calls_meta if c["path"] == "/api/user/self"]
        self.assertTrue(public)
        self.assertEqual(public[0]["auth"], {})
        self.assertTrue(authed)
        self.assertEqual(authed[0]["auth"].get("access_token"), TOKEN)

    def test_end_to_end_parked_on_401(self):
        client, patches = self._install_http(
            {"success": True, "data": {"system_name": "New API", "version": "v0.9.0"}},
            self_status=401)
        cands, errors = importers.parse_sites(f"e2e2 https://e2e2.example.com {TOKEN}", fmt="txt")
        with patches[0], patches[1]:
            result = importers.apply_sites_import(cands, cfg=config.load_config(),
                                                  parse_errors=errors)
        entry = result["added"][0]
        self.assertEqual(entry["status"], config.STATUS_PARKED)
        self.assertIn("401", entry["parked_reason"])
        self.assertNotIn(TOKEN, entry["parked_reason"])
        for path in client.calls:
            self.assertNotIn("pricing", path.lower())


class FakeProbeClient:
    """喂给 New API 适配器的假 HTTP 客户端：记录路径与是否带凭据。"""

    def __init__(self, status_data, self_status=200, self_payload=None):
        self.status_data = status_data
        self.self_status = self_status
        self.self_payload = self_payload or {}
        self.calls: list = []
        self.calls_meta: list = []
        self.base_url = ""
        self.auth: dict = {}

    def get(self, path, **kwargs):
        self.calls.append(path)
        self.calls_meta.append({"path": path, "auth": dict(self.auth)})
        if path in ("/api/status", "/api/v1/status"):
            return FakeJsonResp(self.status_data)
        if path == "/api/user/self":
            return FakeJsonResp(self.self_payload, status=self.self_status)
        return FakeJsonResp({}, status=404)


class FakeJsonResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class ImportSitesCliTests(Base):
    def _stdin(self, text):
        return mock.patch.object(CLI.importers.sys, "stdin", io.StringIO(text))

    def test_import_from_stdin_dry_run(self):
        with self._stdin(f"cli-site https://cli.example.com {TOKEN}\n"), \
                mock.patch.object(rprobe, "probe_url",
                                  lambda url, **kw: ok_probe()):
            rc, out, _ = self.run_cli("import", "sites", "-", "--dry-run", "--json")
        self.assertEqual(rc, EXIT_OK, out)
        payload = json.loads(out)
        self.assertEqual(payload["dry_run"], True)
        self.assertEqual(payload["added"][0]["name"], "cli-site")
        self.assertNotIn(TOKEN, out)
        self.assertFalse(os.path.exists(paths.site_path("cli-site")))

    def test_import_from_file_writes_site(self):
        path = os.path.join(self.tmp, "sites.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"file-site https://file.example.com {TOKEN} 9\n")
        with mock.patch.object(rprobe, "probe_url", lambda url, **kw: ok_probe()), \
                mock.patch.object(importers, "_auth_probe",
                                  lambda site, cfg, name: {"auth_ok": True}):
            rc, out, _ = self.run_cli("import", "sites", path)
        self.assertEqual(rc, EXIT_OK, out)
        site = config.load_site("file-site")
        self.assertEqual(site["status"], config.STATUS_ACTIVE)
        self.assertEqual(site["auth"]["user_id"], 9)
        self.assertNotIn(TOKEN, out)

    def test_missing_file_is_usage_error(self):
        rc, out, _ = self.run_cli("import", "sites", "/nonexistent/sites.txt")
        self.assertEqual(rc, EXIT_USAGE)
        self.assertIn("不存在", out)

    def test_bad_line_reports_failed_and_exit_fail(self):
        path = os.path.join(self.tmp, "bad.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("orphan-line\n")
        rc, out, _ = self.run_cli("import", "sites", path, "--json")
        self.assertEqual(rc, EXIT_FAIL)
        payload = json.loads(out)
        self.assertEqual(len(payload["failed"]), 1)

    def test_json_output_never_contains_token(self):
        path = os.path.join(self.tmp, "s.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"name": "jsite", "base": "https://j.example.com", "token": TOKEN}], f)
        with mock.patch.object(rprobe, "probe_url", lambda url, **kw: ok_probe()), \
                mock.patch.object(importers, "_auth_probe",
                                  lambda site, cfg, name: {"auth_ok": True}):
            rc, out, _ = self.run_cli("import", "sites", path, "--json")
        self.assertEqual(rc, EXIT_OK, out)
        payload = json.loads(out)
        self.assertIn("added", payload)
        self.assertNotIn(TOKEN, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("token", json.dumps(payload, ensure_ascii=False).lower())

    def test_probe_url_command(self):
        with mock.patch.object(rprobe, "HttpClient",
                               lambda base, proxy=None, auth=None: mock.Mock(**{
                                   "get.return_value": mock.Mock(
                                       status_code=200,
                                       **{"json.return_value": {
                                           "success": True,
                                           "data": {"system_name": "New API",
                                                    "version": "v0.9.5"}}})})):
            rc, out, _ = self.run_cli("probe-url", "https://p.example.com/v1", "--json")
        self.assertEqual(rc, EXIT_OK, out)
        payload = json.loads(out)
        self.assertEqual(payload["panel"], "newapi")
        self.assertEqual(payload["version"], "v0.9.5")
        self.assertEqual(payload["base"], "https://p.example.com")


if __name__ == "__main__":
    unittest.main()
