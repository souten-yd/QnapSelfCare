"""Small read-only local network status UI for QnapSelfCare."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import platform

import updater


ROOT = Path(__file__).resolve().parent
VERSION = "0.2.1"
PORT = 17863
BLUETOOTH_SYSFS = Path("/sys/class/bluetooth")


def architecture(machine=None):
    machine = machine or platform.machine()
    return {"x86_64": "x86_64", "AMD64": "x86_64", "aarch64": "arm_64", "arm64": "arm_64"}.get(machine)


def bluetooth_adapters(root=BLUETOOTH_SYSFS):
    """Report adapter names without scanning, bonding, or changing HCI state."""
    try:
        return sorted(p.name for p in root.iterdir() if p.name.startswith("hci") and p.name[3:].isdigit())
    except OSError:
        return []


class Handler(BaseHTTPRequestHandler):
    server_version = "QnapSelfCare"

    def _send(self, status, body, mime="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        static = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/icon.svg": ("icon.svg", "image/svg+xml"),
        }
        if path in static:
            name, mime = static[path]
            self._send(200, (ROOT / "web" / name).read_bytes(), mime)
        elif path == "/api/status":
            self._json(200, {"version": VERSION, "architecture": architecture(),
                             "bluetooth_adapters": bluetooth_adapters(),
                             "devices": {"HEM-6232T": "未接続", "HBF-228T": "未接続"},
                             "collection_enabled": False})
        elif path == "/api/update":
            arch = architecture()
            if arch is None:
                self._json(400, {"error": "未対応のCPU構成です"})
                return
            try:
                asset = updater.latest(VERSION, arch)
            except (ValueError, OSError, json.JSONDecodeError) as error:
                self._json(502, {"error": f"更新情報を取得できません: {error}"})
                return
            self._json(200, {"available": asset is not None,
                             "version": asset["version"] if asset else None,
                             "url": asset["url"] if asset else None})
        else:
            self._json(404, {"error": "見つかりません"})

    def do_POST(self):
        self._json(405, {"error": "読み取り専用です"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
