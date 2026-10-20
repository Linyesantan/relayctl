import contextlib
import importlib.machinery
import importlib.util
import io
import os
import tempfile
import unittest
from unittest import mock

import yaml

from relayctl import config, paths
from relayctl.errors import EXIT_OK, EXIT_USAGE

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
