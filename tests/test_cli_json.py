import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from relayctl import config
from relayctl.errors import EXIT_USAGE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_PATH = os.path.join(ROOT, "bin", "relayctl")


def load_cli():
    loader = importlib.machinery.SourceFileLoader("relayctl_cli_json_tests", CLI_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CLI = load_cli()


class CliJsonTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.home})
        self.env.start()
        self.addCleanup(self.env.stop)
        config.init_layout()

    def run_cli(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = CLI.main(list(args))
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_schema_uses_stable_envelope(self):
        code, payload, _ = self.run_cli("schema", "--kind", "site", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(set(payload), {"schema", "ok", "command", "data", "meta", "warnings", "error"})
        self.assertEqual(payload["schema"], "relayctl/v2")
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])

    def test_legacy_dict_alias_and_raw_mode(self):
        code, payload, _ = self.run_cli("site", "list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"], {})
        code, raw, _ = self.run_cli("site", "list", "--json-raw")
        self.assertEqual(code, 0)
        self.assertEqual(raw, {})

    def test_usage_error_is_four_and_matches_envelope(self):
        code, payload, _ = self.run_cli("balance", "missing", "--json")
        self.assertEqual(code, EXIT_USAGE)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], code)
        self.assertEqual(payload["meta"]["exit_code"], code)

    def test_argparse_error_is_four_and_json(self):
        code, payload, _ = self.run_cli("balance", "--json")
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(payload["error"]["code"], EXIT_USAGE)

    def test_all_command_parsers_register_json_flags(self):
        parser, *_ = CLI.build_parser()
        stack = [parser]
        while stack:
            current = stack.pop()
            self.assertIn("--json", current._option_string_actions)
            self.assertIn("--json-raw", current._option_string_actions)
            for action in current._actions:
                if isinstance(action, argparse._SubParsersAction):
                    stack.extend(action.choices.values())


if __name__ == "__main__":
    unittest.main()
