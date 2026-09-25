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
        interfaces = [(1, "tun0"), (2, "eth0"), (3, "tailscale0"), (4, "docker0")]
        ip_by_name = {"tun0": "10.7.7.7", "eth0": "192.168.68.57",
                      "tailscale0": "100.101.102.103", "docker0": "172.17.0.1"}
        with patch.object(webapp.socket, "if_nameindex", return_value=interfaces), \
             patch.object(webapp, "interface_ipv4", side_effect=ip_by_name.get) as lookup:
            self.assertEqual(webapp.listen_addresses(),
                             ["192.168.68.57", "127.0.0.1", "100.101.102.103"])
            self.assertEqual([call.args[0] for call in lookup.call_args_list],
                             ["eth0", "tailscale0"])

    def test_missing_lan_interface_fails_with_reason(self):
        with patch.object(webapp.socket, "if_nameindex", return_value=[(1, "tun0")]):
            with self.assertRaisesRegex(ValueError, "no private IPv4"):
                webapp.listen_addresses()


if __name__ == "__main__":
    unittest.main()
