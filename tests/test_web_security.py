import copy
import http.client
import json
import threading
import unittest
from unittest import mock

from relayctl import config, web
from relayctl.errors import ConfigError


class WebSecurityTests(unittest.TestCase):
    def start_server(self, password="web-secret", sites=None):
        cfg = copy.deepcopy(config.DEFAULT_CONFIG)
        cfg["web"]["access_password"] = password
        cfg["web"]["scheduler_enabled"] = False
        cfg["sites"] = sites or {}
        web.RelayctlWebHandler.config = cfg
        web.RelayctlWebHandler.sessions = {}
        server = web.HTTPServer(("127.0.0.1", 0), web.RelayctlWebHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def request(server, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        conn.close()
        return result

    def test_unauthenticated_api_is_401_and_no_cors_star(self):
        server = self.start_server()
        status, headers, body = self.request(server, "GET", "/api/status")
        self.assertEqual(status, 401)
        self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(json.loads(body)["message"], "未认证")

    def test_bearer_auth_allows_api(self):
        server = self.start_server()
        status, headers, body = self.request(
            server, "GET", "/api/status", headers={"Authorization": "Bearer web-secret"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"], [])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_cross_site_post_is_rejected_even_with_bearer(self):
        server = self.start_server()
        body = json.dumps({"site": "none"})
        status, _, _ = self.request(server, "POST", "/api/key/add", body=body, headers={
            "Authorization": "Bearer web-secret",
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
        })
        self.assertEqual(status, 403)

    def test_request_body_limit(self):
        server = self.start_server(password="")
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        conn.putrequest("POST", "/api/key/add")
        conn.putheader("Content-Length", str(web.MAX_BODY_BYTES + 1))
        conn.putheader("Origin", f"http://127.0.0.1:{server.server_port}")
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        conn.close()

    def test_form_login_creates_session(self):
        server = self.start_server()
        origin = f"http://127.0.0.1:{server.server_port}"
        body = "password=web-secret"
        status, headers, _ = self.request(server, "POST", "/login", body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
        })
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request(server, "GET", "/", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"relayctl", body)

    def test_json_redacts_secrets_and_ui_avoids_inner_html(self):
        site = {
            "tier": "parked", "parked_reason": "token=sk-WEBSECRET123456789",
            "auth": {"access_token": "sk-WEBSECRET123456789"},
        }
        server = self.start_server(sites={"safe": site})
        status, _, body = self.request(
            server, "GET", "/api/status", headers={"Authorization": "Bearer web-secret"})
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn("WEBSECRET", text)
        self.assertNotIn("innerHTML", web.HTML_TEMPLATE)
        self.assertIn("textContent", web.HTML_TEMPLATE)

    def test_non_loopback_without_password_refuses_start(self):
        cfg = copy.deepcopy(config.DEFAULT_CONFIG)
        cfg["sites"] = {}
        cfg["web"]["access_password"] = ""
        with mock.patch.object(web, "HTTPServer") as http_server, \
                mock.patch.object(web, "start_background_scheduler") as scheduler:
            with self.assertRaises(ConfigError):
                web.run_web(host="0.0.0.0", config_override=cfg)
        http_server.assert_not_called()
        scheduler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
