import unittest

from requests import Request, Response
import requests

from relayctl.core import HttpClient
from relayctl.errors import ConfigError


def _session_redirect(client, old_url, new_url, old_headers=None, new_headers=None):
    response = Response()
    response.request = Request("GET", old_url, headers=old_headers).prepare()
    redirected = Request("GET", new_url, headers=new_headers if new_headers is not None
                         else client.get_headers()).prepare()
    client.session.rebuild_auth(redirected, response)
    return redirected


class CrossOriginCredentialTests(unittest.TestCase):
    def test_cookie_request_rejected_on_cross_origin(self):
        c = HttpClient("https://a.test", auth={"cookie": "session=secret"})
        with self.assertRaises(requests.exceptions.TooManyRedirects):
            _session_redirect(c, "https://a.test/x", "https://b.test/y")

    def test_original_request_credentials_are_enough_to_reject(self):
        """即使即将发出的请求已被清洗，只要原始请求带过凭据也必须拒绝。"""
        c = HttpClient("https://a.test", auth={"access_token": "secret"})
        with self.assertRaises(requests.exceptions.TooManyRedirects):
            _session_redirect(c, "https://a.test/x", "https://b.test/y",
                              old_headers={"Authorization": "Bearer secret", "Cookie": "s=1"},
                              new_headers={"Accept": "application/json"})

    def test_same_origin_with_cookie_is_allowed(self):
        c = HttpClient("https://a.test", auth={"cookie": "session=secret", "access_token": "t"})
        _session_redirect(c, "https://a.test/x", "https://a.test/y")

    def test_default_ports_are_normalized(self):
        c = HttpClient("https://a.test", auth={"access_token": "secret"})
        for old_url, new_url in (
            ("https://a.test/x", "https://a.test:443/y"),
            ("https://a.test:443/x", "https://a.test/y"),
            ("http://a.test/x", "http://a.test:80/y"),
        ):
            _session_redirect(c, old_url, new_url)

    def test_explicit_non_default_port_is_cross_origin(self):
        c = HttpClient("https://a.test", auth={"access_token": "secret"})
        with self.assertRaises(requests.exceptions.TooManyRedirects):
            _session_redirect(c, "https://a.test/x", "https://a.test:8443/y")

    def test_redirect_without_credentials_is_left_to_requests(self):
        c = HttpClient("https://a.test")
        _session_redirect(c, "https://a.test/x", "https://b.test/y")


class HttpClientConfigTests(unittest.TestCase):
    def test_user_agent_is_relayctl_2(self):
        c = HttpClient("https://a.test")
        self.assertEqual(c.get_headers()["User-Agent"], "relayctl/2.0")

    def test_non_string_proxy_raises_config_error(self):
        for bad in ({"http": "http://127.0.0.1:7890"}, 8080, ["http://127.0.0.1:7890"]):
            with self.assertRaises(ConfigError):
                HttpClient("https://a.test", proxy=bad)

    def test_string_proxy_modes(self):
        self.assertEqual(HttpClient("https://a.test", proxy="auto").session.trust_env, True)
        self.assertEqual(HttpClient("https://a.test", proxy="none").session.trust_env, False)
        self.assertEqual(HttpClient("https://a.test", proxy=None).session.trust_env, False)
        explicit = HttpClient("https://a.test", proxy="http://127.0.0.1:7890")
        self.assertEqual(explicit.session.proxies["http"], "http://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
