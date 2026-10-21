import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from relayctl import config, doctor, paths
from relayctl import schema as relay_schema
from relayctl.errors import EXIT_FAIL, EXIT_OK, EXIT_USAGE


class SchemaDoctorTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.home}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        config.init_layout()

    def test_all_schemas_are_json_and_have_key_fields(self):
        schemas = relay_schema.schemas("all")
        json.loads(json.dumps(schemas))
        for value in schemas.values():
            self.assertEqual(value["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("auth", schemas["site"]["properties"])
        self.assertIn("request", schemas["script"]["properties"])
        self.assertIn("keys", schemas["dist"]["properties"])

    def test_doctor_clean_layout_passes_without_network(self):
        with mock.patch.object(doctor, "get_adapter") as adapter:
            report, code = doctor.run(False)
        self.assertEqual(code, EXIT_OK, report)
        self.assertEqual(report["summary"]["failed"], 0)
        adapter.assert_not_called()

    def test_doctor_detects_bad_permissions(self):
        os.chmod(paths.cache_dir(), 0o755)
        report, code = doctor.run(False)
        self.assertEqual(code, EXIT_FAIL)
        item = next(check for check in report["checks"] if check["name"] == "permission.dir.cache")
        self.assertFalse(item["ok"])
        self.assertIn("0700", item["detail"])

    def test_doctor_detects_bad_site_and_missing_env(self):
        bad = paths.site_path("broken")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("- not-a-map\n")
        os.chmod(bad, 0o600)
        site = {
            **config.SITE_DEFAULTS,
            "name": "env-site",
            "base": "https://example.com",
            "auth": {**config.DEFAULT_AUTH, "access_token_env": "MISSING_DOCTOR_TOKEN"},
            "docs": dict(config.DEFAULT_DOCS),
        }
        config.save_site("env-site", site)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISSING_DOCTOR_TOKEN", None)
            report, code = doctor.run(False)
        self.assertEqual(code, EXIT_USAGE)
        failed = {item["name"] for item in report["checks"] if not item["ok"]}
        self.assertIn("site.broken.config", failed)
        self.assertIn("site.env-site.env.access_token_env", failed)

    def test_doctor_detects_non_loopback_without_password(self):
        cfg = config.load_global()
        cfg["web"] = {**cfg["web"], "host": "0.0.0.0", "access_password": ""}
        config.save_global(cfg)
        report, code = doctor.run(False)
        self.assertEqual(code, EXIT_USAGE)
        check = next(item for item in report["checks"] if item["name"] == "web.access")
        self.assertFalse(check["ok"])
        self.assertNotIn("token", json.dumps(report).lower())

    def test_skill_has_seven_steps_and_safety_terms(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        filename = os.path.join(root, "skill", "relayctl", "SKILL.md")
        self.assertTrue(os.path.isfile(filename))
        with open(filename, encoding="utf-8") as handle:
            text = handle.read()
        for number in range(1, 8):
            self.assertIn(f"{number}.", text)
        for term in ("probe-url", "系统访问令牌", "docs.script_howto", "--dry-run", "不打印完整 Key", "不把", "git"):
            self.assertIn(term, text)


if __name__ == "__main__":
    unittest.main()
