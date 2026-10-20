import os
import tempfile
import unittest
from unittest import mock

import yaml

from relayctl import config, migrate, paths


LEGACY = {
    "sites": {
        "old-api": {
            "tier": "api",
            "adapter": "newapi",
            "base": "https://api.example.com",
            "proxy": "auto",
            "auth": {"access_token": "plain-token", "user_id": 12345},
        },
        "old-parked": {
            "tier": "parked",
            "base": "https://parked.example.com",
            "parked_reason": "等待生成系统访问令牌",
        },
    }
}


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()
        config.init_layout()
        with open(paths.legacy_sites_path(), "w", encoding="utf-8") as f:
            yaml.safe_dump(LEGACY, f, allow_unicode=True)

    def tearDown(self):
        self._env.stop()

    def test_dry_run_writes_nothing(self):
        result = migrate.migrate(dry_run=True)
        self.assertEqual(sorted(result["added"]), ["old-api", "old-parked"])
        self.assertEqual(result["counts"], {"added": 2, "skipped": 0, "failed": 0})
        self.assertFalse(os.path.exists(paths.site_path("old-api")))
        self.assertFalse(os.path.exists(paths.site_path("old-parked")))

    def test_migrate_writes_each_site_atomically(self):
        result = migrate.migrate()
        self.assertEqual(sorted(result["added"]), ["old-api", "old-parked"])
        for name in ("old-api", "old-parked"):
            self.assertTrue(os.path.exists(paths.site_path(name)))

        site = config.load_site("old-api")
        # 旧字段保留
        self.assertEqual(site["base"], "https://api.example.com")
        self.assertEqual(site["adapter"], "newapi")
        self.assertEqual(site["proxy"], "auto")
        self.assertEqual(site["auth"]["access_token"], "plain-token")
        self.assertEqual(site["auth"]["user_id"], 12345)
        # 新增字段补全
        self.assertEqual(site["name"], "old-api")
        self.assertEqual(site["console_url"], "")
        self.assertEqual(site["panel"], "newapi")
        self.assertEqual(sorted(site["docs"]), sorted(config.DOCS_FIELDS))
        self.assertEqual(site["tags"], [])
        self.assertEqual(site["refresh_interval"], "")
        # tier -> status
        self.assertEqual(site["status"], config.STATUS_ACTIVE)
        self.assertEqual(site["tier"], "api")

        parked = config.load_site("old-parked")
        self.assertEqual(parked["status"], config.STATUS_PARKED)
        self.assertEqual(parked["parked_reason"], "等待生成系统访问令牌")
        self.assertEqual(parked["tier"], "parked")

    def test_migrate_is_idempotent_and_keeps_legacy_file(self):
        first = migrate.migrate()
        self.assertEqual(first["counts"]["added"], 2)
        second = migrate.migrate()
        self.assertEqual(second["counts"], {"added": 0, "skipped": 2, "failed": 0})
        self.assertEqual(sorted(second["skipped"]), ["old-api", "old-parked"])
        # 旧文件保留，可随时重跑
        self.assertTrue(os.path.exists(paths.legacy_sites_path()))

    def test_bad_site_name_recorded_as_failed(self):
        with open(paths.legacy_sites_path(), "w", encoding="utf-8") as f:
            yaml.safe_dump({"sites": {"../escape": {"base": "https://x.example.com"}}}, f)
        result = migrate.migrate()
        self.assertEqual(result["counts"], {"added": 0, "skipped": 0, "failed": 1})
        self.assertIn("../escape", result["failed"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.yaml")))

    def test_missing_legacy_file_is_noop(self):
        os.unlink(paths.legacy_sites_path())
        result = migrate.migrate()
        self.assertEqual(result["counts"], {"added": 0, "skipped": 0, "failed": 0})


if __name__ == "__main__":
    unittest.main()
