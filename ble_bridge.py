# SPDX-License-Identifier: GPL-3.0-or-later
"""Local Unix socket bridge for the optional QNAP USB Bluetooth container."""
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import threading

LOCK = threading.Lock()
ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.reply(200, {"bleak": True, "dbus": Path("/run/dbus/system_bus_socket").exists()})

    def do_POST(self):
        if self.path != "/run":
            self.reply(404, {"error": "Unknown operation"})
            return
        if not LOCK.acquire(blocking=False):
            self.reply(409, {"error": "Bluetooth処理が実行中です"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length < 65536:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if payload.get("action") not in ("scan", "pair", "sync"):
                raise ValueError("Unknown action")
            result = subprocess.run([sys.executable, str(ROOT / "ble_worker.py")], input=json.dumps(payload),
                                    capture_output=True, text=True, timeout=190)
            body = json.loads(result.stdout)
            self.reply(200 if result.returncode == 0 else 400, body)
        except Exception as error:
            self.reply(400, {"error": str(error) or type(error).__name__})
        finally:
            LOCK.release()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if __name__ == "__main__":
    path = Path("/data/run/ble.sock")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with Server(str(path), Handler) as server:
        os.chmod(path, 0o600)
        try:
            server.serve_forever()
        finally:
            path.unlink(missing_ok=True)
