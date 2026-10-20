import os
import stat
import tempfile
import threading
import unittest
from unittest import mock

from relayctl import config, paths
from relayctl.errors import ConfigError

import yaml


def sample_site(name="demo", **override):
    site = {
        "name": name,
        "base": "https://demo.example.com",
        "console_url": "https://demo.example.com/console",
        "panel": "newapi",
        "adapter": "newapi",
        "script": None,
        "auth": dict(config.DEFAULT_AUTH),
        "docs": dict(config.DEFAULT_DOCS),
        "proxy": "",
        "refresh_interval": "",
        "tags": [],
        "status": config.STATUS_ACTIVE,
        "parked_reason": "",
    }
    site.update(override)
    return site


class HomeTestMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.tmp})
        self._env.start()

    def tearDown(self):
        self._env.stop()


class PathsAndLayoutTests(HomeTestMixin, unittest.TestCase):
    def test_home_is_dynamic_not_frozen_at_import(self):
        self.assertEqual(paths.home(), os.path.abspath(self.tmp))
        other = os.path.join(tempfile.gettempdir(), "relayctl-home-check")
        with mock.patch.dict(os.environ, {"RELAYCTL_HOME": other}):
            self.assertEqual(paths.home(), other)
        self.assertEqual(paths.home(), os.path.abspath(self.tmp))

    def test_default_home_without_env(self):
        env = {k: v for k, v in os.environ.items() if k != "RELAYCTL_HOME"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.home(),
                             os.path.abspath(os.path.expanduser("~/.config/relayctl")))

    def test_init_layout_dirs_are_private(self):
        config.init_layout()
        for d in (paths.home(), paths.sites_dir(), paths.scripts_dir(),
                  paths.cache_dir(), paths.dist_dir(), paths.logs_dir()):
            self.assertTrue(os.path.isdir(d), d)
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)
        self.assertTrue(os.path.exists(paths.config_path()))
        self.assertEqual(stat.S_IMODE(os.stat(paths.config_path()).st_mode), 0o600)

    def test_v2_layout_has_no_legacy_state_dir(self):
        config.init_layout()
        self.assertFalse(os.path.exists(os.path.join(paths.home(), "state")))

    def test_site_name_cannot_escape_sites_dir(self):
        for bad in ("../evil", "a/b", "..", "/abs", "sp ace/../x"):
            with self.assertRaises(ConfigError):
                paths.site_path(bad)
        # 合法名必须落在 sites/ 下
        self.assertEqual(os.path.dirname(paths.site_path("ok-1")), paths.sites_dir())


class GlobalConfigTests(HomeTestMixin, unittest.TestCase):
    def test_defaults(self):
        cfg = config.load_global()
        self.assertEqual(cfg["proxy"], "none")
        self.assertEqual(cfg["refresh_interval"], "30m")
        self.assertEqual(cfg["cache"]["mode"], "auto")
        self.assertEqual(cfg["web"]["host"], "127.0.0.1")
        self.assertEqual(cfg["web"]["port"], 8088)
        self.assertEqual(cfg["web"]["access_password"], "")

    def test_global_roundtrip_is_private(self):
        config.save_global({"proxy": "auto", "web": {"port": 9090}})
        cfg = config.load_global()
        self.assertEqual(cfg["proxy"], "auto")
        self.assertEqual(cfg["web"]["port"], 9090)
        self.assertEqual(cfg["web"]["host"], "127.0.0.1")  # 未覆盖的键保持默认
        self.assertEqual(stat.S_IMODE(os.stat(paths.config_path()).st_mode), 0o600)

    def test_broken_global_config_raises(self):
        with open(paths.config_path(), "w", encoding="utf-8") as f:
            f.write("proxy: [unclosed\n")
        with self.assertRaises(ConfigError):
            config.load_global()


class SiteFileTests(HomeTestMixin, unittest.TestCase):
    def test_save_site_creates_private_file(self):
        path = config.save_site("demo", sample_site())
        self.assertEqual(path, os.path.join(paths.sites_dir(), "demo.yaml"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(paths.sites_dir()).st_mode), 0o700)
        raw = yaml.safe_load(open(path, encoding="utf-8"))
        self.assertEqual(raw["name"], "demo")
        self.assertEqual(sorted(raw["docs"]), sorted(config.DOCS_FIELDS))

    def test_concurrent_site_writes_are_atomic(self):
        names = [f"s{i}" for i in range(6)]
        barrier = threading.Barrier(len(names))
        errors = []

        def writer(name):
            try:
                barrier.wait(timeout=5)
                config.save_site(name, sample_site(name=name, base=f"https://{name}.example.com"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertFalse(errors)
        for name in names:
            raw = yaml.safe_load(open(paths.site_path(name), encoding="utf-8"))
            self.assertEqual(raw["name"], name)

    def test_broken_site_file_is_isolated(self):
        config.save_site("good", sample_site(name="good"))
        with open(paths.site_path("broken"), "w", encoding="utf-8") as f:
            f.write("name: broken\nbase: [unclosed\n")

        sites, errors = config.load_sites()
        self.assertIn("good", sites)
        self.assertNotIn("broken", sites)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].name, "broken")

        # 聚合入口继续输出旧调用方可用的 {sites: ...}
        cfg = config.load_config()
        self.assertIn("good", cfg["sites"])
        self.assertEqual(len(cfg["site_errors"]), 1)

    def test_missing_docs_backfilled_with_warning(self):
        config.init_layout()
        raw = {"base": "https://x.example.com", "adapter": "newapi"}
        with open(paths.site_path("legacy"), "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f)
        site = config.load_site("legacy")
        self.assertEqual(sorted(site["docs"]), sorted(config.DOCS_FIELDS))
        self.assertTrue(any("docs." in w for w in site["_warnings"]))

    def test_new_write_requires_complete_docs(self):
        incomplete = sample_site()
        del incomplete["docs"]["notes"]
        with self.assertRaises(ConfigError):
            config.save_site("demo", incomplete)

    def test_strict_write_rejects_bad_fields(self):
        with self.assertRaises(ConfigError):
            config.save_site("demo", sample_site(base="not-a-url"))
        with self.assertRaises(ConfigError):
            config.save_site("demo", sample_site(status="whatever"))
        with self.assertRaises(ConfigError):
            config.save_site("demo", sample_site(refresh_interval="半小时"))
        with self.assertRaises(ConfigError):
            config.save_site("demo", sample_site(panel="unknown"))
        with self.assertRaises(ConfigError):
            config.save_site("demo", sample_site(adapter="unknown"))
        with self.assertRaises(ConfigError):
            config.save_site("demo", sample_site(adapter="script", panel="custom", script=None))

    def test_auth_types_match_v2_schema(self):
        self.assertEqual(config.VALID_AUTH_TYPES,
                         ("access_token", "cookie", "password", "none"))
        for auth_type in config.VALID_AUTH_TYPES:
            config.save_site(auth_type, sample_site(
                name=auth_type, auth={**config.DEFAULT_AUTH, "type": auth_type}))
            self.assertEqual(config.load_site(auth_type)["auth"]["type"], auth_type)

        # 旧 bearer 值读取时兼容迁移，写回使用 access_token。
        normalized, warnings = config.normalize_site(
            sample_site(auth={**config.DEFAULT_AUTH, "type": "bearer"}), strict=False)
        self.assertEqual(normalized["auth"]["type"], "access_token")
        self.assertTrue(any("旧值" in warning for warning in warnings))

    def test_status_maps_to_legacy_tier(self):
        config.save_site("act", sample_site(name="act", status="active"))
        config.save_site("pak", sample_site(name="pak", status="parked"))
        self.assertEqual(config.load_site("act")["tier"], "api")
        self.assertEqual(config.load_site("pak")["tier"], "parked")

    def test_delete_site_only_removes_one(self):
        config.save_site("a", sample_site(name="a"))
        config.save_site("b", sample_site(name="b"))
        self.assertTrue(config.delete_site("a"))
        self.assertFalse(config.delete_site("a"))
        self.assertIn("b", config.load_sites()[0])


class TokenEnvTests(HomeTestMixin, unittest.TestCase):
    def test_token_env_resolved_at_runtime_not_written_back(self):
        env_name = "RELAYCTL_TEST_TOKEN"
        with mock.patch.dict(os.environ, {env_name: "sk-runtime-secret"}):
            config.save_site("demo", sample_site(
                name="demo", auth={**config.DEFAULT_AUTH, "access_token_env": env_name}))
            site = config.load_site("demo", resolve=True)
            self.assertEqual(site["auth"]["access_token"], "sk-runtime-secret")
            # 任何回写都不得把明文 token 落到配置里
            config.save_site("demo", site)
            raw = yaml.safe_load(open(paths.site_path("demo"), encoding="utf-8"))
            self.assertEqual(raw["auth"]["access_token"], "")
            self.assertEqual(raw["auth"]["access_token_env"], env_name)
            self.assertNotIn("sk-runtime-secret", open(paths.site_path("demo"), encoding="utf-8").read())

    def test_missing_env_keeps_token_empty(self):
        self.assertNotIn("RELAYCTL_MISSING_TOKEN", os.environ)
        config.save_site("demo2", sample_site(
            name="demo2", auth={**config.DEFAULT_AUTH, "access_token_env": "RELAYCTL_MISSING_TOKEN"}))
        site = config.load_site("demo2", resolve=True)
        self.assertEqual(site["auth"]["access_token"], "")
        self.assertIn("missing", site["auth"]["_token_source"])

    def test_password_env_resolved_but_never_written(self):
        env_name = "RELAYCTL_TEST_PASSWORD"
        with mock.patch.dict(os.environ, {env_name: "runtime-password"}):
            config.save_site("password-site", sample_site(
                name="password-site",
                auth={**config.DEFAULT_AUTH, "type": "password", "username": "alice",
                      "password_env": env_name}))
            site = config.load_site("password-site", resolve=True)
            self.assertEqual(site["auth"]["password"], "runtime-password")
            config.save_site("password-site", site)
            raw = yaml.safe_load(open(paths.site_path("password-site"), encoding="utf-8"))
            self.assertEqual(raw["auth"]["password"], "")
            self.assertEqual(raw["auth"]["password_env"], env_name)


if __name__ == "__main__":
    unittest.main()
