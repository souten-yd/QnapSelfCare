import base64
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import webapp


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        cls.server.token = "a" * 40
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, password=None, method="GET"):
        headers = {}
        if password is not None:
            auth = base64.b64encode(f"admin:{password}".encode()).decode()
            headers["Authorization"] = "Basic " + auth
        return urlopen(Request(self.base + path, headers=headers, method=method), timeout=2)

    def test_dashboard_is_protected_and_read_only(self):
        with self.assertRaises(HTTPError) as failure:
            self.request("/")
        self.assertEqual(failure.exception.code, 401)
        with self.assertRaises(HTTPError) as failure:
            self.request("/api/status", "wrong")
        self.assertEqual(failure.exception.code, 401)
        with self.request("/", "a" * 40) as response:
            self.assertIn("QnapSelfCare", response.read().decode())
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.assertRaises(HTTPError) as failure:
            self.request("/api/status", "a" * 40, "POST")
        self.assertEqual(failure.exception.code, 405)

    def test_status_does_not_claim_collection(self):
        with self.request("/api/status", "a" * 40) as response:
            data = json.load(response)
        self.assertEqual(data["version"], webapp.VERSION)
        self.assertFalse(data["collection_enabled"])
        self.assertEqual(data["devices"]["HBF-228T"], "未接続")


if __name__ == "__main__":
    unittest.main()
