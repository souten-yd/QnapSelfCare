"""QnapSelfCare local health record manager."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import sqlite3
import zipfile
import threading
from urllib.parse import parse_qs, urlsplit

import updater
from self_update import UpdateManager, UpdateConflict
from collectors import CollectorManager
from storage import CSV_FIELDS, Store, integer
from durability import DataUnavailable, file_lock
from protection import ProtectionManager
from homehub_migration import MigrationManager
from wellness_ai import Coach

ROOT = Path(__file__).resolve().parent
VERSION = "0.3.16"
PORT = 17863
BLUETOOTH_SYSFS = Path("/sys/class/bluetooth")
MAX_BODY = 32 * 1024 * 1024


def architecture(machine=None):
    return {"x86_64": "x86_64", "AMD64": "x86_64", "aarch64": "arm_64", "arm64": "arm_64"}.get(machine or platform.machine())


def bluetooth_adapters(root=BLUETOOTH_SYSFS):
    try:
        return sorted(p.name for p in root.iterdir() if re.fullmatch(r"hci\d+", p.name))
    except OSError:
        return []


class Handler(BaseHTTPRequestHandler):
    server_version = "QnapSelfCare"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    @property
    def store(self):
        return self.server.store

    def _headers(self, status, size, mime, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

    def _send(self, status, body, mime="application/json; charset=utf-8", filename=None):
        self._headers(status, len(body), mime, filename)
        self.wfile.write(body)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def _filters(self, query):
        return {key: query[key][0] for key in ("user_id", "kind", "since", "until") if query.get(key)}

    def do_GET(self):
        try:
            self.get()
        except (ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
            self._json(400, {"error": str(error)})
        except (OSError, sqlite3.DatabaseError, DataUnavailable) as error:
            self._json(503, {"error": f"読み込みに失敗しました: {error}"})

    def get(self):
        parsed = urlsplit(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                  "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/migration.js": ("migration.js", "text/javascript; charset=utf-8"),
                  "/wellness.js": ("wellness.js", "text/javascript; charset=utf-8"),
                  "/protection.js": ("protection.js", "text/javascript; charset=utf-8"),
                  "/update.js": ("update.js", "text/javascript; charset=utf-8"),
                  "/icon.svg": ("icon.svg", "image/svg+xml")}
        if path in static:
            name, mime = static[path]
            self._send(200, (ROOT / "web" / name).read_bytes(), mime)
        elif path == "/api/status":
            self._json(200, {"version": VERSION, "architecture": architecture(),
                             "bluetooth_adapters": bluetooth_adapters(),
                             "record_count": None if self.store.error else self.store.records(limit=0)["total"],
                             "recovery_required": bool(self.store.error),
                             "database_error": self.store.error,
                             "collection_enabled": False if self.store.error else any(d["auto_sync"] for d in self.store.devices()),
                             "data_path": str(self.store.path)})
        elif path == "/api/users":
            self._json(200, self.store.users())
        elif path in ('/api/wellness/profile', '/api/wellness/summary', '/api/wellness/meals', '/api/wellness/energy'):
            user_id = query.get('user_id', [''])[0]
            self._json(200, {'/api/wellness/energy': self.store.energy_report, '/api/wellness/profile': self.store.wellness_profile,
                             '/api/wellness/summary': self.store.wellness_summary,
                             '/api/wellness/meals': self.store.meals}[path](user_id))
        elif path == '/api/ai/settings':
            self._json(200, self.server.coach.status())
        elif path == "/api/devices":
            self._json(200, self.store.devices())
        elif path == "/api/records":
            self._json(200, self.store.records(**self._filters(query),
                limit=integer(query.get("limit", ["200"])[0], "件数", 1, 2000),
                offset=integer(query.get("offset", ["0"])[0], "開始位置", 0, 10000000)))
        elif path == "/api/listener":
            self._json(200, self.server.collector.listener_status())
        elif path == "/api/jobs":
            self._json(200, self.store.jobs())
        elif path == "/api/diagnostics":
            result = self.server.protection.diagnostics()
            result.update(self.server.collector.diagnostics())
            self._json(200, result)
        elif path == "/api/homehub-migration":
            self._json(200, self.server.migration.status())
        elif path == "/api/protection":
            self._json(200, self.server.protection.status())
        elif path == "/api/protection/download":
            name = query.get('name', [''])[0]
            with self.server.protection.download(name) as stream:
                self._headers(200, os.fstat(stream.fileno()).st_size, 'application/zip', name)
                shutil.copyfileobj(stream, self.wfile, 1024 * 1024)
        elif path == "/api/export.csv":
            self._send(200, self.store.export_csv(**self._filters(query)).encode(), "text/csv; charset=utf-8", "selfcare-records.csv")
        elif path == "/api/template.csv":
            self._send(200, ("\ufeff" + ",".join(CSV_FIELDS) + "\r\n").encode(), "text/csv; charset=utf-8", "selfcare-template.csv")
        elif path == "/api/backup":
            self._send(200, json.dumps(self.store.backup(), ensure_ascii=False).encode(), "application/json; charset=utf-8", "selfcare-backup.json")
        elif path == "/api/update":
            arch = architecture()
            if not arch:
                raise ValueError("未対応のCPU構成です")
            asset = updater.latest(VERSION, arch)
            self._json(200, {"available": asset is not None,
                             "version": asset["version"] if asset else None,
                             "url": asset["url"] if asset else None})
        elif path == "/api/update/status":
            self._json(200, self.server.updates.status())
        elif path == "/api/update/log":
            self._json(200, {"log": self.server.updates.log_tail()})
        else:
            self._json(404, {"error": "見つかりません"})

    def _body(self):
        # A custom header prevents cross-origin HTML forms from changing records.
        # This is not a login requirement; native integrations send the same header.
        if self.headers.get("X-SelfCare-Request") != "1":
            raise PermissionError("X-SelfCare-Request: 1 ヘッダーが必要です")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type: application/json が必要です")
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Content-Lengthを指定してください")
        size = integer(self.headers.get("Content-Length", "0"), "リクエストサイズ", 2, MAX_BODY)
        body = json.loads(self.rfile.read(size), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("有限の数値が必要です")))
        if not isinstance(body, dict):
            raise ValueError("JSONオブジェクトが必要です")
        return body

    def do_POST(self):
        self.mutate("POST")

    def do_DELETE(self):
        self.mutate("DELETE")

    def mutate(self, method):
        try:
            body = self._body()
            path = urlsplit(self.path).path
            if method == "POST":
                if path == "/api/homehub-migration":
                    if set(body) != {'action'}:
                        raise ValueError('actionのみ指定してください')
                    self._json(202, self.server.migration.apply(body['action']))
                    return
                elif path == "/api/protection/settings":
                    self._json(200, self.server.protection.save_settings(body))
                    return
                elif path == "/api/protection/backup":
                    if body:
                        raise ValueError('追加の引数は指定できません')
                    self._json(202, self.server.protection.request_backup())
                    return
                elif path == "/api/protection/verify":
                    if body:
                        raise ValueError('追加の引数は指定できません')
                    self._json(202, self.server.protection.request_verification())
                    return
                elif path == "/api/update/apply":
                    self.store.require_available()
                    if set(body) != {"version"}:
                        raise ValueError("versionのみ指定してください。任意のURL・コマンドは実行できません")
                    self._json(202, self.server.updates.apply(body["version"]))
                    return
                elif path == "/api/users":
                    result = self.store.save_user(body)
                elif path == '/api/wellness/profile':
                    if set(body) != {'user_id', 'profile'}:
                        raise ValueError('利用者と健康設定を指定してください')
                    result = self.store.save_wellness_profile(body['user_id'], body['profile'])
                elif path in ('/api/wellness/activity', '/api/wellness/weight-plan'):
                    if set(body) != {'user_id', 'entry'}:
                        raise ValueError('利用者と記録を指定してください')
                    handler = self.store.save_activity if path.endswith('activity') else self.store.save_weight_plan
                    result = handler(body['user_id'], body['entry'])
                elif path == '/api/wellness/meals':
                    if set(body) != {'user_id', 'meal'}:
                        raise ValueError('利用者と食事記録を指定してください')
                    result = self.store.add_meal(body['user_id'], body['meal'])
                elif path == '/api/ai/settings':
                    result = self.server.coach.save(body)
                elif path == '/api/ai/key':
                    if set(body) != {'provider', 'key'}:
                        raise ValueError('接続先とAPIキーを指定してください')
                    result = self.server.coach.set_key(body['provider'], body['key'])
                elif path == '/api/ai/test':
                    result = self.server.coach.test_connection()
                elif path == '/api/ai/consult':
                    if set(body) not in ({'user_id', 'mode', 'question'}, {'user_id', 'mode', 'question', 'consent'}):
                        raise ValueError('相談内容を指定してください')
                    result = self.server.coach.consult(body['user_id'], body['mode'], body['question'])
                elif path == "/api/devices":
                    result = self.store.save_device(body)
                elif path == "/api/records":
                    result = self.store.add_records([body])
                elif re.fullmatch(r"/api/records/[A-Za-z0-9_-]{1,64}", path):
                    result = self.store.edit_record(path.rsplit("/", 1)[1], body)
                elif path == "/api/import":
                    result = self.store.import_csv(body.get("csv"), body.get("user_id")) if "csv" in body else self.store.add_records(body.get("records"), "api")
                elif path in ('/api/omron-history/preview', '/api/omron-history/apply'):
                    if set(body) != ({'csv', 'user_id'} if path.endswith('preview') else {'csv', 'user_id', 'preview_token'}):
                        raise ValueError('CSVと取込先の利用者を指定してください')
                    result = self.store.omron_history(body['csv'], body['user_id'],
                        apply=path.endswith('apply'), preview_token=body.get('preview_token'))
                elif path == "/api/restore":
                    result = self.store.restore(body)
                elif path == "/api/jobs":
                    result = self.server.collector.submit(body.get("action"), body.get("device_id"), body.get("adapter", "hci0"), body.get("exclusive", False), body.get("transport", "homehub"), body.get('diagnostic', False))
                else:
                    self._json(405, {"error": "対応していない操作です"})
                    return
            else:
                if path == '/api/wellness/activity':
                    if set(body) != {'user_id', 'date'}:
                        raise ValueError('利用者と日付を指定してください')
                    self.store.delete_activity(body['user_id'], body['date'])
                    self._json(200, {'deleted': True})
                    return
                if path == '/api/wellness/meals':
                    if set(body) != {'user_id', 'id'}:
                        raise ValueError('利用者と食事記録を指定してください')
                    self.store.delete_meal(body['user_id'], body['id'])
                    self._json(200, {'deleted': True})
                    return
                match = re.fullmatch(r"/api/(users|devices|records)/([A-Za-z0-9_-]{1,64})", path)
                if not match:
                    self._json(404, {"error": "見つかりません"})
                    return
                kind, identifier = match.groups()
                {"users": self.store.delete_user, "devices": self.store.delete_device, "records": self.store.delete_record}[kind](identifier)
                result = {"deleted": True}
            self._json(200, result)
        except PermissionError as error:
            self._json(403, {"error": str(error)})
        except (DataUnavailable, sqlite3.DatabaseError) as error:
            self._json(503, {"error": str(error)})
        except UpdateConflict as error:
            self._json(409, {"error": str(error)})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
        except Exception:
            self._json(500, {"error": "保存処理に失敗しました。保存先の空き容量・権限を確認してください"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lan", action="store_true", help="listen on all IPv4 interfaces")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--data-dir", default=os.environ.get("SELFCARE_DATA_DIR", "/share/Container/QnapSelfCare"))
    args = parser.parse_args()
    os.umask(0o077)
    # Also held by the offline restore command: never replace a database in active use.
    with file_lock(Path(args.data_dir) / 'service.lock'):
        serve(args)


def serve(args):
    store = Store(Path(args.data_dir) / "data", state_directory=args.data_dir)
    collector = CollectorManager(store)
    protection = ProtectionManager(store)
    with ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.port), Handler) as server:
        server.store, server.collector, server.protection = store, collector, protection
        server.coach = Coach(args.data_dir, store)
        server.migration = MigrationManager(args.data_dir)
        server.updates = UpdateManager(args.data_dir, ROOT, VERSION, architecture(), args.port)
        collector.start()
        protection.start()
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        print(f"QnapSelfCare {VERSION} listening on {server.server_address}; data: {store.path}", flush=True)
        try:
            server.serve_forever()
        finally:
            protection.close()
            collector.close()


if __name__ == "__main__":
    main()
