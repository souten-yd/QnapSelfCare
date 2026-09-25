"""Small read-only status UI for QnapSelfCare."""

import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import platform
import socket
import struct
import threading

import updater


ROOT = Path(__file__).resolve().parent
VERSION = "0.2.3"
PORT = 17863
BLUETOOTH_SYSFS = Path("/sys/class/bluetooth")
PRIVATE_LANS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def interface_ipv4(name):
    """Read an interface's assigned IPv4 without changing its state."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        request = struct.pack("256s", name.encode("ascii"))
        return socket.inet_ntoa(fcntl.ioctl(control.fileno(), 0x8915, request)[20:24])


def listen_addresses():
    """Bind only the LAN and optional Tailscale interface plus loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("192.0.2.1", 9))  # Select an interface without sending a packet.
        lan = ipaddress.IPv4Address(route.getsockname()[0])
    if not any(lan in private for private in PRIVATE_LANS):
        raise ValueError("default route has no private LAN IPv4 address")
    addresses = [str(lan), "127.0.0.1"]
    for _, name in socket.if_nameindex():
        if name != "tailscale0":
            continue
        try:
            address = ipaddress.IPv4Address(interface_ipv4(name))
            if address in ipaddress.ip_network("100.64.0.0/10") and str(address) not in addresses:
                addresses.append(str(address))
        except OSError:
            pass  # Some QNAP Tailscale installations use userspace networking.
    return addresses
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
    parser.add_argument("--lan", action="store_true", help="listen on the LAN, loopback, and tailscale0 addresses")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    addresses = listen_addresses() if args.lan else ["127.0.0.1"]
    servers = []
    try:
        for address in addresses:
            server = ThreadingHTTPServer((address, args.port), Handler)
            servers.append(server)
            print(f"QnapSelfCare listening on {address}:{args.port}", flush=True)
        for server in servers[1:]:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        servers[0].serve_forever()
    finally:
        for server in servers:
            server.server_close()


if __name__ == "__main__":
    main()
