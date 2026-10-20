import unittest
import json
import os
import stat
import tempfile
import threading
from requests import Request, Response
import requests

from relayctl.core import HttpClient, secure_write_json


class CoreSafetyTests(unittest.TestCase):
    def test_cross_origin_redirect_is_rejected(self):
        c = HttpClient("https://a.test", auth={"access_token": "secret", "user_id": 7})
        response = Response()
        response.request = Request("GET", "https://a.test/x").prepare()
        redirected = Request("GET", "https://b.test/y", headers=c.get_headers()).prepare()
        with self.assertRaises(requests.exceptions.TooManyRedirects):
            c.session.rebuild_auth(redirected, response)

    def test_cross_origin_scheme_port_and_host_are_rejected(self):
        c = HttpClient("https://a.test", auth={"access_token": "secret"})
        for old_url, new_url in (
            ("https://a.test/x", "http://a.test/y"),
            ("https://a.test:443/x", "https://a.test:444/y"),
            ("https://a.test/x", "https://b.test/y"),
        ):
            response = Response()
            response.request = Request("GET", old_url).prepare()
            redirected = Request("GET", new_url, headers=c.get_headers()).prepare()
            with self.assertRaises(requests.exceptions.TooManyRedirects):
                c.session.rebuild_auth(redirected, response)

    def test_same_origin_redirect_is_allowed(self):
        c = HttpClient("https://a.test", auth={"access_token": "secret"})
        response = Response()
        response.request = Request("GET", "https://a.test/x").prepare()
        redirected = Request("GET", "https://a.test/y", headers=c.get_headers()).prepare()
        c.session.rebuild_auth(redirected, response)

    def test_concurrent_secure_json_writes_are_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            barrier = threading.Barrier(3)
            errors = []
            def writer(value):
                try:
                    barrier.wait(timeout=3)
                    secure_write_json(path, {"value": value, "items": list(range(50))})
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=writer, args=(value,)) for value in ("A", "B")]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=3)
            for thread in threads:
                thread.join(3)
            self.assertFalse(errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            self.assertIn(data["value"], ("A", "B"))
            self.assertEqual(data["items"], list(range(50)))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
