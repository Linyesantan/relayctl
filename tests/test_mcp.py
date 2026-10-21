import io
import json
import os
import tempfile
import unittest
from unittest import mock

from relayctl import config
from relayctl.mcp import MCPServer, run_stdio


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"RELAYCTL_HOME": self.home})
        self.env.start()
        self.addCleanup(self.env.stop)
        config.init_layout()

    def test_tools_list_contains_safe_tools_and_hides_sensitive_tools(self):
        response = MCPServer().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = {item["name"] for item in response["result"]["tools"]}
        self.assertTrue({"status", "balance", "groups", "keys", "key_add", "import_sites",
                         "import_keys", "dist_export", "site_add", "doctor"}.issubset(names))
        self.assertNotIn("key_rm", names)
        self.assertNotIn("site_rm", names)
        self.assertNotIn("dist_export_reveal", names)

    def test_switches_register_sensitive_tools(self):
        names = {item["name"] for item in MCPServer(True, True).tools()}
        self.assertTrue({"key_rm", "site_rm", "dist_export_reveal"}.issubset(names))

    def test_tools_call_returns_structured_content(self):
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "status", "arguments": {}}}
        response = MCPServer().handle(request)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["sites"], [])
        json.loads(response["result"]["content"][0]["text"])

    def test_unknown_method_is_method_not_found(self):
        response = MCPServer().handle({"jsonrpc": "2.0", "id": 3, "method": "unknown"})
        self.assertEqual(response["error"]["code"], -32601)
        self.assertEqual(response["error"]["data"]["exit_code"], 3)

    def test_stdio_stdout_contains_only_json_frames(self):
        source = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
            '{bad json}\n')
        stdout = io.StringIO()
        stderr = io.StringIO()
        self.assertEqual(run_stdio(stdin=source, stdout=stdout, stderr=stderr), 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        parsed = [json.loads(line) for line in lines]
        self.assertIn("result", parsed[0])
        self.assertEqual(parsed[1]["error"]["code"], -32700)
        self.assertEqual(stderr.getvalue(), "")

    def test_unknown_tool_uses_method_not_found(self):
        request = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                   "params": {"name": "site_rm", "arguments": {"site": "x", "confirm": True}}}
        response = MCPServer().handle(request)
        self.assertEqual(response["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
