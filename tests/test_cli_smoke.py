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

from relayctl import config, paths
from relayctl import cache as rcache
from relayctl import scheduler as rsched
from relayctl.errors import EXIT_OK, EXIT_USAGE, EXIT_FAIL

_HERE = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.join(os.path.dirname(_HERE), "bin", "relayctl")


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("relayctl_cli_under_test", CLI_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CLI = _load_cli()


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def run_cli(self, *argv):
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = CLI.main(list(argv))
        return rc, buf.getvalue(), err.getvalue()

    def add_site(self, name="cli-site", status=config.STATUS_ACTIVE, **override):
        site = {
            "name": name,
            "base": f"https://{name}.example.com",
            "adapter": "newapi",
            "panel": "newapi",
            "auth": dict(config.DEFAULT_AUTH),
            "docs": dict(config.DEFAULT_DOCS),
            "proxy": "",
            "refresh_interval": "",
            "tags": [],
            "status": status,
            "parked_reason": "",
        }
        site.update(override)
        config.save_site(name, site)
        return site

    def patch_adapters(self, errors=None, balance=None):
        """把 CLI 与调度器里的适配器统统替换掉（测试绝不打真实网络）。"""

        class FakeAdapter:
            def __init__(self, name):
                self.name = name
                self.calls = []

            def _maybe(self, section):
                self.calls.append(section)
                if errors and section in errors:
                    raise errors[section]

            def probe(self):
                self._maybe("probe")
                return {"site": self.name, "system_name": "New API", "version": "v1",
                        "auth_ok": True, "quota_per_unit": 500000,
                        "quota_display_type": "USD", "status_ok": True}

            def balance(self):
                self._maybe("balance")
                return balance or {"balance": "2.0000", "used": "1.0000",
                                   "raw_quota": 1000000, "raw_used": 500000,
                                   "quota_per_unit": 500000, "display_type": "USD",
                                   "user_group": "default"}

            def groups(self):
                self._maybe("groups")
                return [{"name": "default", "desc": "", "ratio": 1}]

            def list_keys(self):
                self._maybe("keys")
                return [{"id": 7, "name": "k1", "key_masked": "sk-7...abcd"}]

        created = []

        def factory(name, s_cfg, cfg):
            adapter = FakeAdapter(name)
            created.append(adapter)
            return adapter

        self._cli_patch = mock.patch.object(CLI, "get_adapter", side_effect=factory)
        self._sched_patch = mock.patch.object(rsched, "get_adapter", side_effect=factory)
        self._cli_patch.start()
        self._sched_patch.start()
        self.addCleanup(self._cli_patch.stop)
        self.addCleanup(self._sched_patch.stop)
        return created


class CacheFreshnessCliTests(CliTestBase):
    def test_balance_shows_update_time_and_stale_warning(self):
        self.add_site("stale-site")
        self.patch_adapters()
        self.run_cli("balance", "stale-site")            # 正常拉取一次
        rcache.mark_stale("stale-site", "balance",
                          RuntimeError("upstream 挂了 token=sk-SUPERSECRET123456"))

        rc, out, _ = self.run_cli("balance", "stale-site", "--cached")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("最新更新时间", out)
        self.assertIn("stale", out)
        self.assertIn("上次错误", out)
        self.assertNotIn("SUPERSECRET", out)

    def test_failed_refresh_still_serves_old_data(self):
        self.add_site("srv-site")
        self.patch_adapters()
        self.run_cli("balance", "srv-site")
        created = self.patch_adapters(errors={"balance": RuntimeError("boom")})
        rc, out, _ = self.run_cli("balance", "srv-site", "--refresh")
        self.assertEqual(rc, EXIT_OK, out)   # 有旧缓存就返回旧值，不中断命令
        self.assertIn("2.0000", out)
        self.assertIn("stale", out)
        self.assertTrue(created)

    def test_status_shows_update_time(self):
        self.add_site()
        self.patch_adapters()
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("最新更新时间", out)

    def test_status_json_reports_freshness(self):
        self.add_site()
        self.patch_adapters()
        rc, out, _ = self.run_cli("status", "--json")
        payload = json.loads(out)
        entry = payload["cli-site"]
        for key in ("fetched_at", "from_cache", "stale", "last_error"):
            self.assertIn(key, entry)

    def test_probe_reads_cache_and_supports_refresh(self):
        self.add_site()
        created = self.patch_adapters()
        self.run_cli("probe", "cli-site", "--refresh")
        self.assertEqual(created[0].calls, ["probe"])

        created = self.patch_adapters()
        rc, out, _ = self.run_cli("probe", "cli-site", "--cached")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("最新更新时间", out)
        self.assertEqual(created[0].calls, [])  # 缓存命中，不再打网络
        self.assertIn("New API", out)


class DaemonCommandTests(CliTestBase):
    def test_daemon_once_json(self):
        self.add_site("daemon-site", refresh_interval="15m")
        self.add_site("daemon-parked", status=config.STATUS_PARKED)
        self.add_site("daemon-manual", refresh_interval="0")
        self.patch_adapters()

        rc, out, _ = self.run_cli("daemon", "--once", "--json")
        self.assertEqual(rc, EXIT_OK, out)
        payload = json.loads(out)
        self.assertIn("summary", payload)
        entry = payload["sites"]["daemon-site"]
        for key in ("sections", "success", "errors", "next_refresh_at", "next_refresh_at_local"):
            self.assertIn(key, entry)
        self.assertEqual(sorted(entry["sections"]), sorted(rsched.SECTIONS))
        self.assertTrue(entry["success"])
        self.assertFalse(entry["errors"])
        self.assertIsNotNone(entry["next_refresh_at"])

        self.assertFalse(payload["sites"]["daemon-parked"]["refreshed"])
        self.assertFalse(payload["sites"]["daemon-manual"]["refreshed"])
        self.assertEqual(payload["summary"]["refreshed"], 1)

    def test_daemon_once_human_output(self):
        self.add_site("daemon-site")
        self.patch_adapters()
        rc, out, _ = self.run_cli("daemon", "--once")
        self.assertEqual(rc, EXIT_OK, out)
        self.assertIn("daemon-site", out)
        self.assertIn("成功", out)
        self.assertIn("下次刷新", out)
        # 刷新结果必须落到 $RELAYCTL_HOME/logs/daemon.log（0600）
        log_path = os.path.join(paths.logs_dir(), rsched.LOG_NAME)
        self.assertTrue(os.path.exists(log_path))
        self.assertIn("daemon-site", open(log_path, encoding="utf-8").read())

    def test_daemon_once_reports_failures(self):
        self.add_site("broken-site")
        self.patch_adapters(errors={"balance": RuntimeError("HTTP 500 token=sk-DEADBEEF1234")})
        rc, out, _ = self.run_cli("daemon", "--once", "--json")
        self.assertEqual(rc, EXIT_FAIL)
        payload = json.loads(out)
        entry = payload["sites"]["broken-site"]
        self.assertFalse(entry["success"])
        self.assertTrue(entry["errors"])
        self.assertFalse(entry["sections"]["balance"]["ok"])
        self.assertTrue(entry["sections"]["keys"]["ok"])  # 单分段失败不牵连其它分段
        self.assertNotIn("DEADBEEF", out)


class MigrateCommandTests(CliTestBase):
    def _legacy(self):
        with open(paths.legacy_sites_path(), "w", encoding="utf-8") as f:
            yaml.safe_dump({"sites": {"cli-old": {
                "tier": "api", "adapter": "newapi", "base": "https://cli.example.com",
                "auth": {"access_token": "legacy-token", "user_id": 9},
            }}}, f)

    def test_migrate_dry_run_then_apply(self):
        self._legacy()
        rc, out, _ = self.run_cli("migrate", "--dry-run")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("cli-old", out)
        self.assertFalse(os.path.exists(paths.site_path("cli-old")))

        rc, out, _ = self.run_cli("migrate")
        self.assertEqual(rc, EXIT_OK)
        self.assertTrue(os.path.exists(paths.site_path("cli-old")))
        site = config.load_site("cli-old")
        self.assertEqual(sorted(site["docs"]), sorted(config.DOCS_FIELDS))
        self.assertEqual(site["status"], config.STATUS_ACTIVE)


class SiteCommandTests(CliTestBase):
    ADD_ARGS = (
        "site", "add",
        "--name", "cli-demo",
        "--base", "https://cli-demo.example.com",
        "--console-url", "https://cli-demo.example.com/console",
        "--panel", "newapi",
        "--adapter", "newapi",
        "--auth-type", "access_token",
        "--token-env", "RELAYCTL_CLI_DEMO_TOKEN",
        "--user-id", "7",
        "--login-method", "控制台登录",
        "--token-howto", "个人设置 -> 生成系统访问令牌",
        "--script-howto", "无",
        "--notes", "冒烟测试",
        "--proxy", "none",
        "--refresh-interval", "30m",
        "--tags", "cli,smoke",
        "--status", "active",
    )

    def test_site_add_then_list_then_rm(self):
        rc, out, _ = self.run_cli(*self.ADD_ARGS)
        self.assertEqual(rc, EXIT_OK, out)
        path = paths.site_path("cli-demo")
        self.assertTrue(os.path.exists(path))

        raw = yaml.safe_load(open(path, encoding="utf-8"))
        self.assertEqual(sorted(raw["docs"]), sorted(config.DOCS_FIELDS))
        self.assertEqual(raw["docs"]["token_howto"], "个人设置 -> 生成系统访问令牌")
        self.assertEqual(raw["tags"], ["cli", "smoke"])
        self.assertEqual(raw["auth"]["access_token_env"], "RELAYCTL_CLI_DEMO_TOKEN")

        rc, out, _ = self.run_cli("site", "list")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("cli-demo", out)

        rc, out, _ = self.run_cli("site", "show", "cli-demo")
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("access_token_env", out)
        self.assertNotIn("RELAYCTL_CLI_DEMO_TOKEN=", out)

        # 破坏性操作必须显式确认
        rc, out, _ = self.run_cli("site", "rm", "cli-demo")
        self.assertEqual(rc, EXIT_USAGE)
        self.assertTrue(os.path.exists(path))

        rc, out, _ = self.run_cli("site", "rm", "cli-demo", "--yes")
        self.assertEqual(rc, EXIT_OK)
        self.assertFalse(os.path.exists(path))

    def test_site_add_requires_base(self):
        rc, out, _ = self.run_cli("site", "add", "--name", "no-base")
        self.assertEqual(rc, EXIT_USAGE)
        self.assertIn("base", out)

    def test_site_set_updates_status(self):
        self.run_cli(*self.ADD_ARGS)
        rc, out, _ = self.run_cli("site", "set", "cli-demo", "--status", "parked",
                                  "--parked-reason", "临时搁置")
        self.assertEqual(rc, EXIT_OK, out)
        site = config.load_site("cli-demo")
        self.assertEqual(site["status"], config.STATUS_PARKED)
        self.assertEqual(site["parked_reason"], "临时搁置")
        self.assertEqual(site["tier"], "parked")

    def test_unknown_site_is_usage_error(self):
        rc, out, _ = self.run_cli("balance", "ghost")
        self.assertEqual(rc, EXIT_USAGE)
        self.assertIn("未登记", out)


if __name__ == "__main__":
    unittest.main()
