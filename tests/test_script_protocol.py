"""阶段 4：站点脚本协议（进程调用 / 退出码 / 环境变量隔离 / CLI new+test）。

测试绝不打真实网络，全部通过写进 $RELAYCTL_HOME/scripts/ 的临时脚本完成。
"""

import ast
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from relayctl import config, paths
from relayctl import cache as rcache
from relayctl import script_adapter as rscript
from relayctl import script_base
from relayctl.adapters import get_adapter, ADAPTER_REGISTRY
from relayctl.adapters.script import ScriptAdapter
from relayctl.errors import (AuthError, ConfigError, NotSupported,
                             EXIT_AUTH, EXIT_NOTSUPPORTED, EXIT_FAIL)

TOKEN = "sk-TESTTOKEN1234567890abcd"


class ScriptHomeMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()
        paths.ensure_dir(paths.scripts_dir())

    def tearDown(self):
        self._env.stop()

    # -- 工具 ------------------------------------------------------------
    def write_script(self, name, body, mode=0o750):
        path = os.path.join(paths.scripts_dir(), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n" + body)
        os.chmod(path, mode)
        return path

    def site_cfg(self, script=None, base="https://demo.example.com", **override):
        cfg = {
            "name": "demo",
            "base": base,
            "adapter": "script",
            "script": script,
            "auth": {**config.DEFAULT_AUTH, "access_token": TOKEN,
                     "user_id": 7, "cookie": "sid=abc"},
            "proxy": "none",
            "status": config.STATUS_ACTIVE,
            "docs": dict(config.DEFAULT_DOCS),
        }
        cfg.update(override)
        return cfg

    def runner(self, script, core_cfg=None, **override):
        return rscript.ScriptRunner("demo", self.site_cfg(script, **override),
                                    core_cfg or {})

    def add_site(self, name="demo", **override):
        site = {
            "name": name,
            "base": f"https://{name}.example.com",
            "console_url": "",
            "panel": "custom",
            "adapter": "newapi",
            "script": None,
            "auth": {**config.DEFAULT_AUTH, "access_token": TOKEN},
            "docs": dict(config.DEFAULT_DOCS),
            "proxy": "",
            "refresh_interval": "",
            "tags": [],
            "status": config.STATUS_ACTIVE,
            "parked_reason": "",
        }
        site.update(override)
        config.save_site(name, site)
        return site


# ---------------------------------------------------------------------------
# 脚本路径解析
# ---------------------------------------------------------------------------
class ScriptPathTests(ScriptHomeMixin, unittest.TestCase):
    def test_relative_path_from_home(self):
        self.write_script("demo.py", "import json;print(json.dumps({'ok':1}))\n")
        path = rscript.resolve_script_path("scripts/demo.py", site="demo")
        self.assertEqual(path, os.path.join(paths.scripts_dir(), "demo.py"))

    def test_relative_path_from_scripts_dir(self):
        self.write_script("demo.py", "import json;print(json.dumps({'ok':1}))\n")
        path = rscript.resolve_script_path("demo.py", site="demo")
        self.assertEqual(path, os.path.join(paths.scripts_dir(), "demo.py"))

    def test_missing_script(self):
        with self.assertRaises(ConfigError) as ctx:
            rscript.resolve_script_path("scripts/nope.py", site="demo")
        self.assertIn("不存在", str(ctx.exception))

    def test_empty_script_config(self):
        for bad in (None, "", "   "):
            with self.assertRaises(ConfigError):
                rscript.resolve_script_path(bad, site="demo")

    def test_not_executable(self):
        self.write_script("demo.py", "print('{}')\n", mode=0o640)
        with self.assertRaises(ConfigError) as ctx:
            rscript.resolve_script_path("scripts/demo.py", site="demo")
        self.assertIn("不可执行", str(ctx.exception))

    def test_escape_via_relative_path(self):
        outside = os.path.join(self.tmp, "evil.py")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\nprint('{}')\n")
        os.chmod(outside, 0o750)
        with self.assertRaises(ConfigError) as ctx:
            rscript.resolve_script_path("../evil.py", site="demo")
        self.assertIn("越界", str(ctx.exception))

    def test_escape_via_absolute_path(self):
        outside = os.path.join(self.tmp, "evil.py")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\nprint('{}')\n")
        os.chmod(outside, 0o750)
        with self.assertRaises(ConfigError) as ctx:
            rscript.resolve_script_path(outside, site="demo")
        self.assertIn("越界", str(ctx.exception))

    def test_timeout_default_and_configurable(self):
        self.assertEqual(rscript.resolve_timeout({}), 15.0)
        self.assertEqual(rscript.resolve_timeout({"script": {"timeout": 3}}), 3.0)
        self.assertEqual(rscript.resolve_timeout({"script": {"timeout": "0.5"}}), 0.5)
        self.assertEqual(rscript.resolve_timeout({"script": {"timeout": 0}}), 15.0)


# ---------------------------------------------------------------------------
# 进程调用 / 协议
# ---------------------------------------------------------------------------
class ScriptRunnerTests(ScriptHomeMixin, unittest.TestCase):
    def test_probe_and_balance_ok(self):
        self.write_script("demo.py", """
import json, sys
action = sys.argv[1]
if action == "probe":
    print(json.dumps({"site": "demo", "type": "custom", "system_name": "Demo",
                      "version": "v1", "auth_ok": True, "quota_per_unit": 500000,
                      "quota_display_type": "USD"}))
else:
    print(json.dumps({"balance": "1.5000", "used": "0.5000", "raw_quota": 750000,
                      "raw_used": 250000, "quota_per_unit": 500000,
                      "display_type": "USD", "user_group": "vip"}))
""")
        runner = self.runner("scripts/demo.py")
        probe = runner.run("probe")
        self.assertTrue(probe["auth_ok"])
        self.assertEqual(probe["quota_per_unit"], 500000)
        balance = runner.run("balance")
        self.assertEqual(balance["balance"], "1.5000")
        self.assertEqual(balance["user_group"], "vip")

    def test_groups_returns_list(self):
        self.write_script("demo.py", """
import json
print(json.dumps([{"name": "default", "desc": "默认", "ratio": 1.0}]))
""")
        self.assertEqual(self.runner("scripts/demo.py").run("groups"),
                         [{"name": "default", "desc": "默认", "ratio": 1.0}])

    def test_json_args_passthrough(self):
        self.write_script("demo.py", """
import json, sys
args = json.loads(sys.argv[sys.argv.index("--json-args") + 1])
print(json.dumps({"echo": args, "id": 11, "name": args.get("name"),
                  "group": args.get("group"), "unlimited": args.get("unlimited"),
                  "key_masked": ""}))
""")
        runner = self.runner("scripts/demo.py")
        res = runner.run("create_key", {"name": "k1", "group": "vip", "quota": 2,
                                        "unlimited": False, "models": ["gpt-4o"]})
        self.assertEqual(res["echo"]["name"], "k1")
        self.assertEqual(res["echo"]["models"], ["gpt-4o"])
        self.assertEqual(res["echo"]["quota"], 2)

    def test_no_json_args_for_read_actions(self):
        self.write_script("demo.py", """
import json, sys
print(json.dumps({"argv": sys.argv[1:]}))
""")
        argv = self.runner("scripts/demo.py").run("probe")["argv"]
        self.assertEqual(argv, ["probe"])

    def test_unsupported_exit_three(self):
        self.write_script("demo.py", """
import sys
sys.exit(3)
""")
        with self.assertRaises(NotSupported) as ctx:
            self.runner("scripts/demo.py").run("list_keys")
        self.assertEqual(ctx.exception.exit_code, EXIT_NOTSUPPORTED)

    def test_auth_exit_two(self):
        self.write_script("demo.py", """
import sys
sys.stderr.write("HTTP 401\\n")
sys.exit(2)
""")
        with self.assertRaises(AuthError) as ctx:
            self.runner("scripts/demo.py").run("balance")
        self.assertEqual(ctx.exception.exit_code, EXIT_AUTH)

    def test_generic_failure_exit_one(self):
        self.write_script("demo.py", """
import sys
sys.exit(1)
""")
        with self.assertRaises(rscript.ScriptError) as ctx:
            self.runner("scripts/demo.py").run("balance")
        self.assertEqual(ctx.exception.exit_code, EXIT_FAIL)

    def test_invalid_json_is_protocol_violation(self):
        self.write_script("demo.py", "print('not json at all')\n")
        with self.assertRaises(rscript.ScriptProtocolError):
            self.runner("scripts/demo.py").run("probe")

    def test_extra_stdout_is_protocol_violation(self):
        self.write_script("demo.py", """
print('{"balance": "1.0"}')
print('{"extra": true}')
""")
        with self.assertRaises(rscript.ScriptProtocolError):
            self.runner("scripts/demo.py").run("balance")

    def test_wrong_type_is_protocol_violation(self):
        self.write_script("demo.py", "print('[1, 2, 3]')\n")
        with self.assertRaises(rscript.ScriptProtocolError):
            self.runner("scripts/demo.py").run("probe")

    def test_empty_stdout_is_protocol_violation(self):
        self.write_script("demo.py", "pass\n")
        with self.assertRaises(rscript.ScriptProtocolError):
            self.runner("scripts/demo.py").run("probe")

    def test_timeout(self):
        self.write_script("demo.py", """
import time
time.sleep(2)
print('{}')
""")
        with self.assertRaises(rscript.ScriptTimeoutError) as ctx:
            self.runner("scripts/demo.py", core_cfg={"script": {"timeout": 0.2}}).run("probe")
        self.assertEqual(ctx.exception.exit_code, EXIT_FAIL)

    def test_stderr_is_redacted_and_truncated(self):
        self.write_script("demo.py", f"""
import sys
sys.stderr.write("token={TOKEN} " + "x" * 5000)
sys.exit(1)
""")
        with self.assertRaises(rscript.ScriptError) as ctx:
            self.runner("scripts/demo.py").run("probe")
        message = str(ctx.exception)
        self.assertNotIn(TOKEN, message)
        self.assertIn("***", message)
        self.assertIn("已截断", message)

    def test_token_never_appears_in_argv(self):
        self.write_script("demo.py", """
import json, sys
print(json.dumps({"argv": sys.argv}))
""")
        runner = self.runner("scripts/demo.py")
        argv = runner.run("create_key", {"name": "k", "group": "g"})["argv"]
        self.assertNotIn(TOKEN, " ".join(argv))

    def test_outcome_reports_exit_code_and_status(self):
        self.write_script("demo.py", """
import sys
sys.exit(3)
""")
        outcome = self.runner("scripts/demo.py").invoke("delete_key", {"key_id": 5})
        self.assertEqual(outcome.status, rscript.STATUS_UNSUPPORTED)
        self.assertEqual(outcome.exit_code, 3)
        self.assertFalse(outcome.ok)
        self.assertGreaterEqual(outcome.elapsed, 0)


# ---------------------------------------------------------------------------
# 环境变量隔离
# ---------------------------------------------------------------------------
class ScriptEnvIsolationTests(ScriptHomeMixin, unittest.TestCase):
    def test_script_only_gets_whitelisted_vars(self):
        # 另一个站点的配置绝不能泄漏进本站点脚本
        self.add_site("other", base="https://other.example.com",
                      auth={**config.DEFAULT_AUTH, "access_token": "sk-OTHER1234567890"})
        self.write_script("demo.py", """
import json, os
print(json.dumps({"env": dict(os.environ)}))
""")
        with mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp,
                                          "LEAK_ME": "nope"}):
            env = self.runner("scripts/demo.py").run("probe")["env"]

        for key in ("RELAYCTL_SITE", "RELAYCTL_BASE", "RELAYCTL_TOKEN",
                    "RELAYCTL_COOKIE", "RELAYCTL_USER_ID", "RELAYCTL_PROXY"):
            self.assertIn(key, env)
        for key in ("PATH", "HOME", "LANG"):
            self.assertIn(key, env)

        # 只有白名单里的 RELAYCTL_*，其它（含 RELAYCTL_HOME）一律不给
        leaked = [k for k in env if k.startswith("RELAYCTL_")
                  and k not in rscript.SITE_ENV_VARS]
        self.assertEqual(leaked, [])
        self.assertNotIn("LEAK_ME", env)

        # 值正确、且不夹带任何其它站点信息
        self.assertEqual(env["RELAYCTL_TOKEN"], TOKEN)
        self.assertEqual(env["RELAYCTL_SITE"], "demo")
        self.assertEqual(env["RELAYCTL_BASE"], "https://demo.example.com")
        self.assertEqual(env["RELAYCTL_USER_ID"], "7")
        self.assertEqual(env["RELAYCTL_COOKIE"], "sid=abc")
        joined = json.dumps(env, ensure_ascii=False)
        self.assertNotIn("other.example.com", joined)
        self.assertNotIn("sk-OTHER1234567890", joined)


# ---------------------------------------------------------------------------
# 适配器行为
# ---------------------------------------------------------------------------
class ScriptAdapterTests(ScriptHomeMixin, unittest.TestCase):
    def test_registered_as_script_adapter(self):
        self.assertEqual(ADAPTER_REGISTRY.get("script"), ScriptAdapter)
        self.write_script("demo.py", "print('{}')\n")
        adapter = get_adapter("demo", self.site_cfg("scripts/demo.py"), {})
        self.assertIsInstance(adapter, ScriptAdapter)
        self.assertEqual(adapter.NAME, "script")

    def test_list_keys_is_normalized_without_plaintext(self):
        self.write_script("demo.py", """
import json
print(json.dumps([{"id": 3, "name": "k1", "group": "vip", "status": 1,
                   "unlimited_quota": True, "used_quota": 0,
                   "key": "sk-PLAINTEXTKEY123456", "model_limits": "gpt-4o,claude"}]))
""")
        adapter = get_adapter("demo", self.site_cfg("scripts/demo.py"), {})
        keys = adapter.list_keys()
        self.assertEqual(len(keys), 1)
        key = keys[0]
        self.assertEqual(sorted(key), sorted(
            ["id", "name", "group", "enabled", "status", "unlimited",
             "remain", "used", "key_masked", "models"]))
        self.assertTrue(key["unlimited"])
        self.assertEqual(key["models"], ["gpt-4o", "claude"])
        self.assertEqual(key["key_masked"], "sk-P...3456")
        self.assertNotIn("sk-PLAINTEXTKEY123456", json.dumps(key, ensure_ascii=False))

    def test_cache_never_stores_plaintext(self):
        self.write_script("demo.py", """
import json
print(json.dumps([{"id": 1, "name": "k", "key": "sk-CACHEPLAINTEXT9999"}]))
""")
        adapter = get_adapter("demo", self.site_cfg("scripts/demo.py"), {})
        keys = adapter.list_keys()
        rcache.write("demo", "keys", keys)
        with open(rcache.cache_path("demo"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("sk-CACHEPLAINTEXT9999", text)
        self.assertIn("key_masked", text)

    def test_create_key_result_is_sanitized(self):
        self.write_script("demo.py", """
import json
print(json.dumps({"id": 9, "name": "k", "group": "vip", "unlimited": False,
                  "key": "sk-SECRETKEY000111", "_raw_key": "sk-SECRETKEY000111",
                  "key_masked": "sk-S...0111"}))
""")
        adapter = get_adapter("demo", self.site_cfg("scripts/demo.py"), {})
        res = adapter.create_key("k", "vip", quota=1)
        self.assertEqual(res["id"], 9)
        self.assertNotIn("key", res)
        self.assertNotIn("_raw_key", res)

    def test_update_key_only_sends_changed_fields(self):
        self.write_script("demo.py", """
import json, sys
args = json.loads(sys.argv[sys.argv.index("--json-args") + 1])
print(json.dumps({"echo": args, "id": args.get("key_id"), "updated": True}))
""")
        adapter = get_adapter("demo", self.site_cfg("scripts/demo.py"), {})
        res = adapter.update_key(5, group="vip", name="new-name")
        self.assertEqual(res["echo"]["key_id"], 5)
        self.assertEqual(res["echo"]["group"], "vip")
        self.assertEqual(res["echo"]["name"], "new-name")
        self.assertNotIn("quota", res["echo"])
        self.assertNotIn("status", res["echo"])

    def test_delete_key_bool(self):
        self.write_script("ok.py", """
import json
print(json.dumps({"id": 4, "ok": True}))
""")
        self.write_script("no.py", """
import json
print(json.dumps({"id": 4, "ok": False}))
""")
        ok = get_adapter("demo", self.site_cfg("scripts/ok.py"), {})
        no = get_adapter("demo", self.site_cfg("scripts/no.py"), {})
        self.assertTrue(ok.delete_key(4))
        self.assertFalse(no.delete_key(4))

    def test_probe_fills_expected_fields(self):
        self.write_script("demo.py", "print('{\"auth_ok\": true}')\n")
        adapter = get_adapter("demo", self.site_cfg("scripts/demo.py"), {})
        probe = adapter.probe()
        for field in ("site", "type", "system_name", "version", "auth_ok",
                      "quota_per_unit", "quota_display_type"):
            self.assertIn(field, probe)
        self.assertTrue(probe["auth_ok"])


# ---------------------------------------------------------------------------
# script_base 便捷基类
# ---------------------------------------------------------------------------
class ScriptBaseTests(unittest.TestCase):
    @staticmethod
    def run_adapter(adapter, argv):
        """执行 adapter.main() 并捕获 stdout / stderr。"""
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = adapter.main(argv)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def base_adapter(cls=None, **env):
        cls = cls or script_base.ScriptAdapterBase
        return cls(env={"RELAYCTL_SITE": "demo", "RELAYCTL_TOKEN": TOKEN, **env})

    def test_unimplemented_action_exits_three(self):
        for action in script_base.ACTIONS:
            code, out, err = self.run_adapter(self.base_adapter(), [action])
            self.assertEqual(code, 3, action)
            self.assertEqual(out.strip(), "")
            self.assertIn(action, err)

    def test_unknown_action_exits_three(self):
        self.assertEqual(self.run_adapter(self.base_adapter(), ["explode"])[0], 3)

    def test_missing_action_exits_three(self):
        self.assertEqual(self.run_adapter(self.base_adapter(), [])[0], 3)

    def test_implemented_action_prints_single_json(self):
        class Impl(script_base.ScriptAdapterBase):
            def probe(self):
                return {"site": self.site, "auth_ok": bool(self.token)}

        code, out, _ = self.run_adapter(self.base_adapter(Impl), ["probe"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"site": "demo", "auth_ok": True})
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_auth_error_exits_two(self):
        class Impl(script_base.ScriptAdapterBase):
            def balance(self):
                raise script_base.AuthError("HTTP 401")

        self.assertEqual(self.run_adapter(self.base_adapter(Impl), ["balance"])[0], 2)

    def test_generic_exception_exits_one_and_redacts(self):
        class Impl(script_base.ScriptAdapterBase):
            def balance(self):
                raise RuntimeError(f"boom token={TOKEN}")

        code, out, err = self.run_adapter(self.base_adapter(Impl), ["balance"])
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "")
        self.assertNotIn(TOKEN, err)
        self.assertIn("***", err)

    def test_json_args_parsing(self):
        self.assertEqual(script_base.parse_args(["--json-args", '{"a":1}']), {"a": 1})
        self.assertEqual(script_base.parse_args(["--json-args={\"a\":2}"]), {"a": 2})
        with self.assertRaises(script_base.ScriptError):
            script_base.parse_args(["--json-args", "{bad"])
        with self.assertRaises(script_base.ScriptError):
            script_base.parse_args(["--oops"])

    def test_script_base_is_standalone(self):
        """基类必须零依赖：可整段复制进脚本，也绝不会与宿主 relayctl 包循环导入。"""
        with open(script_base.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "script_base 不得使用相对导入")
                self.assertFalse((node.module or "").split(".")[0] == "relayctl",
                                 f"script_base 不得导入 relayctl: {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.split(".")[0] == "relayctl",
                                     f"script_base 不得导入 relayctl: {alias.name}")

    def test_extra_host_args_do_not_break_signature(self):
        """宿主注入 dry_run 等字段时，脚本签名不覆盖它也不会 TypeError。"""

        class Impl(script_base.ScriptAdapterBase):
            def delete_key(self, key_id=None):
                return {"id": key_id, "ok": True}

        self.assertEqual(self.base_adapter(Impl).handle(
            "delete_key", {"key_id": 8, "dry_run": True}), {"id": 8, "ok": True})

    def test_json_args_round_trip_through_main(self):
        class Impl(script_base.ScriptAdapterBase):
            def create_key(self, name=None, group=None, **kwargs):
                return {"name": name, "group": group, "dry_run": kwargs.get("dry_run")}

        code, out, _ = self.run_adapter(
            self.base_adapter(Impl),
            ["create_key", "--json-args", '{"name":"k","group":"vip","dry_run":true}'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["group"], "vip")


# ---------------------------------------------------------------------------
# CLI: relayctl script new / test
# ---------------------------------------------------------------------------
def _load_cli():
    import importlib.machinery
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "bin", "relayctl")
    loader = importlib.machinery.SourceFileLoader("relayctl_script_cli", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CLI = _load_cli()


class ScriptCliTests(ScriptHomeMixin, unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = CLI.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_script_new_then_test_end_to_end(self):
        self.add_site("demo")
        rc, out, _ = self.run_cli("script", "new", "demo")
        self.assertEqual(rc, 0, out)

        script_path = os.path.join(paths.scripts_dir(), "demo.py")
        self.assertTrue(os.path.exists(script_path))
        self.assertEqual(stat.S_IMODE(os.stat(script_path).st_mode), 0o750)

        site = config.load_site("demo")
        self.assertEqual(site["adapter"], "script")
        self.assertEqual(site["script"], "scripts/demo.py")
        self.assertTrue(site["docs"]["script_howto"])

        rc, out, _ = self.run_cli("script", "test", "demo")
        self.assertEqual(rc, 0, out)
        for action in rscript.ACTIONS:
            self.assertIn(action, out)
        self.assertIn("unsupported", out)

    def test_script_test_json_is_stable(self):
        self.add_site("demo")
        self.run_cli("script", "new", "demo")
        rc, out, _ = self.run_cli("script", "test", "demo", "--json")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertIn("schema", payload)
        self.assertTrue(payload["ok"])
        # 规范契约只在 data；信封顶层不再重复 data 内容
        self.assertEqual(sorted(payload["data"]), sorted(
            ["site", "script", "timeout", "ok", "summary", "actions"]))
        self.assertTrue(payload["data"]["ok"])
        self.assertEqual(payload["data"]["summary"]["total"], len(rscript.ACTIONS))
        self.assertEqual(payload["data"]["summary"]["unsupported"], len(rscript.ACTIONS))
        self.assertEqual([a["action"] for a in payload["data"]["actions"]],
                         list(rscript.ACTIONS))
        for entry in payload["data"]["actions"]:
            self.assertEqual(entry["status"], "unsupported")
            self.assertEqual(entry["exit_code"], 3)
            self.assertIn("elapsed_ms", entry)
        self.assertEqual(json.loads(out), payload)  # 可重复解析，结构稳定

    def test_script_test_single_action(self):
        self.add_site("demo")
        self.run_cli("script", "new", "demo")
        rc, out, _ = self.run_cli("script", "test", "demo", "--action", "probe")
        self.assertEqual(rc, 0)
        self.assertIn("probe", out)
        self.assertNotIn("delete_key", out)

    def test_script_test_works_on_parked_site(self):
        """adapter=script 的站点即使被搁置（parked）也必须能体检。"""
        self.add_site("demo", status=config.STATUS_PARKED,
                      parked_reason="等待补齐凭据")
        rc, out, _ = self.run_cli("script", "new", "demo")
        self.assertEqual(rc, 0, out)
        self.assertEqual(config.load_site("demo")["status"], config.STATUS_PARKED)

        rc, out, _ = self.run_cli("script", "test", "demo", "--json")
        self.assertEqual(rc, 0, out)
        payload = json.loads(out)
        self.assertTrue(payload["data"]["ok"])
        self.assertEqual(payload["data"]["summary"]["unsupported"], len(rscript.ACTIONS))

    def test_script_test_reports_failure(self):
        self.add_site("demo", adapter="script", script="scripts/demo.py")
        self.write_script("demo.py", "print('oops')\n")
        rc, out, _ = self.run_cli("script", "test", "demo")
        self.assertEqual(rc, EXIT_FAIL)
        self.assertIn("failed", out)

        rc, out, _ = self.run_cli("script", "test", "demo", "--json")
        self.assertEqual(rc, EXIT_FAIL)
        self.assertFalse(json.loads(out)["ok"])
        self.assertEqual(json.loads(out)["data"]["summary"]["failed"], len(rscript.ACTIONS))

    def test_script_new_refuses_without_force(self):
        self.add_site("demo")
        self.run_cli("script", "new", "demo")
        rc, out, _ = self.run_cli("script", "new", "demo")
        self.assertEqual(rc, 4)
        self.assertIn("--force", out)

        rc, out, _ = self.run_cli("script", "new", "demo", "--force")
        self.assertEqual(rc, 0, out)

    def test_script_test_requires_script_adapter(self):
        self.add_site("demo")
        rc, out, _ = self.run_cli("script", "test", "demo")
        self.assertEqual(rc, 4)
        self.assertIn("script new", out)

    def test_script_test_unknown_site(self):
        rc, out, _ = self.run_cli("script", "test", "ghost")
        self.assertEqual(rc, 4)
        self.assertIn("未登记", out)

    def test_generated_template_runs_standalone(self):
        """模板脚本必须可直接被宿主调用（默认动作 -> 退出码 3）。"""
        self.add_site("demo")
        self.run_cli("script", "new", "demo")
        site = config.load_site("demo", resolve=True)
        script_path = rscript.resolve_script_path(site["script"], site="demo")

        env = rscript.build_env("demo", site, {})
        proc = subprocess.run([script_path, "probe"], shell=False, env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout.strip(), "")

        proc = subprocess.run([script_path, "list_keys"], shell=False, env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 3)


if __name__ == "__main__":
    unittest.main()
