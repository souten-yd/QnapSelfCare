from http.server import ThreadingHTTPServer
import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import webapp


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, method="GET"):
        return urlopen(Request(self.base + path, method=method), timeout=2)

    def test_dashboard_is_open_and_read_only(self):
        with self.request("/") as response:
            self.assertIn("QnapSelfCare", response.read().decode())
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.assertRaises(HTTPError) as failure:
            self.request("/api/status", "POST")
        self.assertEqual(failure.exception.code, 405)

    def test_status_does_not_claim_collection(self):
        with self.request("/api/status") as response:
            data = json.load(response)
        self.assertEqual(data["version"], webapp.VERSION)
        self.assertFalse(data["collection_enabled"])
        self.assertEqual(data["devices"]["HBF-228T"], "未接続")
        self.assertIn("bluetooth_adapters", data)

    def test_adapter_discovery_does_not_initialize_bluetooth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hci1").mkdir()
            (root / "hci0").mkdir()
            (root / "other").mkdir()
            self.assertEqual(webapp.bluetooth_adapters(root), ["hci0", "hci1"])
            self.assertEqual(webapp.bluetooth_adapters(root / "missing"), [])

    def test_no_source_subnet_filter(self):
        # No source-network setting is required for the status endpoint.
        with self.request("/api/status") as response:
            self.assertEqual(response.status, 200)

    def test_listen_addresses_are_explicit(self):
        class Route:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def connect(self, *_): pass
            def getsockname(self): return ("192.168.68.57", 0)
        with patch.object(webapp.socket, "socket", return_value=Route()), \
             patch.object(webapp.socket, "if_nameindex", return_value=[(1, "eth0"), (2, "tailscale0")]), \
             patch.object(webapp, "interface_ipv4", return_value="100.101.102.103"):
            self.assertEqual(webapp.listen_addresses(),
                             ["192.168.68.57", "127.0.0.1", "100.101.102.103"])


if __name__ == "__main__":
    unittest.main()
