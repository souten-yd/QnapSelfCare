"""Serialized, bounded Bluetooth jobs and optional automatic synchronization."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time

from storage import now, sync_day
from durability import write_json

ROOT = Path(__file__).resolve().parent
HOMEHUB_SOCKET = Path(os.environ.get("SELFCARE_HOMEHUB_SOCKET", "/share/Container/QnapHomeHub/data/radio/ble.sock"))
ACTIVE_WORKERS = set()
WORKER_LOCK = threading.Lock()


class BluetoothFailure(ValueError):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic if isinstance(diagnostic, dict) else None


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=320):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def bridge_request(path, payload=None, endpoint=None):
    connection = UnixHTTPConnection(path, timeout=5 if endpoint == "/watch" else 320 if payload else 3)
    try:
        body = json.dumps(payload).encode() if payload else None
        connection.request("POST" if payload else "GET", endpoint or ("/run" if payload else "/health"), body,
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read(4 * 1024 * 1024))
        if response.status != 200:
            raise BluetoothFailure(data.get("error", "Bluetoothブリッジに接続できません"), data.get('diagnostic'))
        return data
    finally:
        connection.close()


def run_worker(request, directory):
    if request.get("transport", "homehub") == "homehub":
        if not HOMEHUB_SOCKET.exists():
            raise ValueError("共通Bluetoothサービスが未起動です。QnapHomeHubのradioサービスを起動してください")
        return bridge_request(HOMEHUB_SOCKET, request)
    bridge = Path(directory).parent / "run/ble.sock"
    if bridge.exists():
        return bridge_request(bridge, request)
    env = dict(os.environ)
    vendor = str(Path(directory).parent / "python")
    env["PYTHONPATH"] = vendor + os.pathsep + env.get("PYTHONPATH", "")
    env["SELFCARE_BLE_LOCK"] = str(Path(directory).parent / "ble.lock")
    process = subprocess.Popen([sys.executable, str(ROOT / "ble_worker.py")], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    with WORKER_LOCK:
        ACTIVE_WORKERS.add(process)
    try:
        stdout, _ = process.communicate(json.dumps(request), timeout=190)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        with WORKER_LOCK:
            ACTIVE_WORKERS.discard(process)
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        raise ValueError("Bluetooth処理が終了しました。Python/BlueZの導入状態を確認してください") from None
    if process.returncode or response.get("error"):
        raise BluetoothFailure(response.get("error", "Bluetooth処理に失敗しました"), response.get('diagnostic'))
    return response


class CollectorManager:
    def __init__(self, store, runner=run_worker):
        self.store = store
        self.runner = runner
        self.queue = queue.Queue(maxsize=8)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.pending = set()
        self.next_attempt = {}
        self.sync_day = sync_day()
        self.next_scan = 0
        self.next_watch = 0
        self.watch_configured = False
        self.listen_error = None
        self.listen_ready = False
        self.listen_seen = {}
        self.listen_delayed = {}
        self.listen_jobs = {}
        self.scan_group = 0
        self.watch_error = None
        self.thread = threading.Thread(target=self.loop, daemon=True, name="selfcare-collector")

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        with WORKER_LOCK:
            for process in ACTIVE_WORKERS:
                process.terminate()
        self.thread.join(timeout=3)

    def submit(self, action, device_id=None, adapter="hci0", exclusive=False, transport="homehub", diagnostic=False, automatic=False, watch_ticket=None):
        self.store.require_available()
        if not isinstance(diagnostic, bool) or (diagnostic and action not in ('pair', 'sync')):
            raise ValueError('詳細診断は手動のペアリング・履歴同期でのみ使用できます')
        if action not in ("scan", "pair", "sync"):
            raise ValueError("対応していない操作です")
        device = self.store.device(device_id) if device_id else None
        if action != "scan":
            if device is None:
                raise ValueError("機器が登録されていません")
            if automatic and action == 'sync' and device.get('automatic_synced_today'):
                raise ValueError('本日の自動同期は完了しています。手動同期はいつでも実行できます')
            if not device["address"]:
                raise ValueError("Bluetoothアドレスを登録してください")
            if action == "sync" and not device["bindings"]:
                raise ValueError("機器内の利用者番号を利用者に割り当ててください")
            if action == "sync" and not device["paired"]:
                raise ValueError("先にペアリングしてください")
            adapter, exclusive, transport = device["adapter"], device["exclusive"], device["transport"]
        import re
        if not re.fullmatch(r"hci\d{1,2}", adapter):
            raise ValueError("Bluetoothアダプターの指定が不正です")
        if transport not in ("homehub", "direct"):
            raise ValueError("Bluetooth接続方式が不正です")
        if transport == "direct" and exclusive is not True:
            raise ValueError("選択したドングルを他のサービスが使用していないことを確認してください")
        pending_key = device_id or "scan"
        with self.lock:
            if pending_key in self.pending or self.queue.full():
                raise ValueError("同じ機器の処理が実行中、または待機中です")
            if watch_ticket is not None:
                if self.listen_delayed.get(pending_key) is not watch_ticket or self.listen_signature(device) != watch_ticket['signature']:
                    raise ValueError('待ち受け予約は取り消されました')
            identifier = self.store.create_job(device_id, action)
            if watch_ticket is not None:
                self.listen_jobs[identifier] = watch_ticket
            # A manual operation takes precedence over a delayed automatic request.
            self.listen_delayed.pop(pending_key, None)
            self.pending.add(pending_key)
            self.queue.put_nowait((identifier, pending_key, action, device, adapter, transport, diagnostic, automatic))
        return {"id": identifier, "state": "queued"}

    def process(self, item):
        identifier, pending_key, action, device, adapter, transport, diagnostic, automatic = item
        self.store.update_job(identifier, "running", "Bluetooth処理を実行しています")
        trace = None
        with self.lock:
            watch_ticket = self.listen_jobs.pop(identifier, None)
        try:
            if automatic and action == 'sync':
                current = self.store.device(device['id'])
                if not current or not current['auto_sync'] or current.get('automatic_synced_today'):
                    self.store.update_job(identifier, 'skipped', '本日の自動同期が完了済み、または自動同期が解除されたため取り消しました')
                    return
            if watch_ticket is not None:
                current = self.store.device(device['id'])
                if self.listen_signature(current) != watch_ticket['signature']:
                    self.store.update_job(identifier, 'skipped', '待ち受け設定の変更または機器削除により予約を取り消しました')
                    return
                device = current
            request = {"action": action, "device": device, "adapter": adapter, "transport": transport}
            if diagnostic:
                request['diagnostic'] = True
            if action == "pair":
                # Preserve an attempted key separately until programming succeeds.
                key_path = self.store.directory.parent / "config" / ("pair-" + device["id"] + ".json")
                key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                pending = json.loads(key_path.read_text()) if key_path.exists() else {}
                key = pending.get("key") if pending.get("address") == device["address"] else None
                if not key:
                    key = secrets.token_hex(16)
                    write_json(key_path, {"address": device["address"], "key": key})
                request["key"] = key
            elif action == "sync":
                request["key"] = self.store.pairing_key(device["address"])
            result = self.runner(request, self.store.directory)
            if diagnostic:
                trace = result.get('diagnostic')
            if result.get("error"):
                raise ValueError(result["error"])
            if action == "pair":
                if not result.get("paired"):
                    raise ValueError("機器がペアリング完了を返しませんでした")
                self.store.pairing_key(device["address"], request["key"])
                key_path.unlink(missing_ok=True)
                message = "ペアリングが完了しました。履歴を同期できます"
            elif action == "sync":
                records = result.get("records", [])
                saved = self.store.add_records(records, "bluetooth") if records else {"inserted": 0, "duplicates": 0}
                invalid = result.get("invalid_records", 0)
                result = dict(saved, invalid_records=invalid, **({'diagnostic': trace} if diagnostic and trace else {}))
                message = f"{saved['inserted']}件追加・{saved['duplicates']}件重複・{invalid}件日時不正"
            else:
                result = dict(result, adapter=adapter, transport=transport)
                message = f"{len(result.get('devices', []))}台検出しました"
            if watch_ticket is not None:
                message = ('待ち受け同期（再試行）: ' if watch_ticket['attempt'] == 2 else '待ち受け同期: ') + message
            self.store.update_job(identifier, "done", message, result, automatic=automatic and action == "sync")
        except Exception as error:
            trace = getattr(error, 'diagnostic', None) or trace
            # The peer is another local service, but do not persist unbounded or malformed diagnostics.
            if diagnostic and isinstance(trace, dict):
                encoded = json.dumps(trace, ensure_ascii=False)
                if len(encoded) > 16000:
                    trace = {'stage': str(trace.get('stage', 'unknown'))[:40], 'truncated': True}
            else:
                trace = None
            missing = str(error) == "機器が見つかりません。機器を通信可能な状態にし、NASへ近づけてください"
            skipped = automatic and action == "sync" and missing
            message = "機器が通信可能でないためスキップしました。次の同期周期に再確認します" if skipped else str(error) or type(error).__name__
            if watch_ticket is not None:
                retry = False
                if watch_ticket['attempt'] == 1 and not self.stop_event.is_set() and not self.store.error:
                    current = self.store.device(device['id'])
                    if self.listen_signature(current) == watch_ticket['signature']:
                        with self.lock:
                            self.listen_delayed[device['id']] = dict(watch_ticket, attempt=2, due=time.monotonic()+60)
                        retry = True
                if skipped:
                    message = '機器が通信可能でないためスキップしました'
                message += '。60秒後に1回だけ再試行を予約しました' if retry else '。今回の待ち受け同期を終了し、次の検知を待ちます'
            self.store.update_job(identifier, "skipped" if skipped else "failed", message,
                                  {'diagnostic': trace} if trace else None)
        finally:
            with self.lock:
                self.pending.discard(pending_key)
            if device:
                self.next_attempt[device["id"]] = time.monotonic() + device["interval"]

    @staticmethod
    def listen_signature(device):
        if not device or device.get('automatic_synced_today') or not (device['auto_sync'] and device['paired'] and device['bindings']
                and device.get('sync_mode') == 'listen' and device['transport'] == 'homehub'):
            return None
        return json.dumps({k: device[k] for k in ('address', 'adapter', 'model', 'bindings', 'transport')}, sort_keys=True)

    def dispatch_listen(self):
        # Timers never hold the Bluetooth adapter or sleep the worker thread.
        devices = {d['id']: d for d in self.store.devices()}
        with self.lock:
            for did, ticket in list(self.listen_delayed.items()):
                if self.listen_signature(devices.get(did)) != ticket['signature']:
                    self.listen_delayed.pop(did, None)
            due = [(did, ticket) for did, ticket in self.listen_delayed.items() if ticket['due'] <= time.monotonic()]
        for did, ticket in due:
            try:
                self.submit('sync', did, automatic=True, watch_ticket=ticket)
            except ValueError:
                # Busy queue is retried without resetting the 60-second timer.
                pass

    def listen(self):
        if time.monotonic() < self.next_watch:
            return
        self.next_watch = time.monotonic() + 2
        devices = [d for d in self.store.devices() if d['auto_sync'] and d['paired'] and d['bindings']
                   and not d.get('automatic_synced_today')
                   and d.get('sync_mode', 'interval') == 'listen' and d['transport'] == 'homehub']
        if not devices and not self.watch_configured:
            self.listen_ready = False
            self.listen_error = None
            return
        try:
            adapters = {d['adapter'] for d in devices}
            if len(adapters) > 1:
                raise ValueError('共通Bluetoothの待ち受けは同じアダプターに設定してください')
            adapter = next(iter(adapters), getattr(self, 'listen_adapter', 'hci0'))
            result = bridge_request(HOMEHUB_SOCKET, {'adapter': adapter, 'addresses': sorted({d['address'] for d in devices})}, endpoint='/watch')
            if not result.get('supported'):
                raise ValueError('QnapHomeHubを0.3.5以降へ更新してください')
            self.watch_configured = bool(devices)
            self.listen_adapter = adapter
            self.listen_ready = bool(result.get('ready'))
            self.listen_error = result.get('error')
            events = {e['address']: e['at'] for e in result.get('events', [])}
            self.listen_seen = {k: v for k, v in self.listen_seen.items() if k in {d['id'] for d in devices}}
            for device in devices:
                event = events.get(device['address'])
                if event is None or event <= self.listen_seen.get(device['id'], 0):
                    continue
                # Consume newer advertisements during waiting/cooldown; never extend a reservation.
                self.listen_seen[device['id']] = event
                with self.lock:
                    if (device['id'] in self.listen_delayed or device['id'] in self.pending
                            or time.monotonic() < self.next_attempt.get(device['id'], 0)):
                        continue
                    self.listen_delayed[device['id']] = {'due': time.monotonic()+60, 'attempt': 1,
                                                        'signature': self.listen_signature(device)}
        except Exception as error:
            self.listen_ready = False
            self.listen_error = '待ち受けに接続できません。HomeHub 0.3.5以降を確認してください: ' + str(error)
            self.next_watch = time.monotonic() + 10

    def schedule(self):
        today = sync_day()
        if today != self.sync_day:
            # Midnight releases the daily gate even if the previous cooldown crosses midnight.
            self.next_attempt.clear()
            self.next_scan = 0
            self.next_watch = 0
            self.sync_day = today
        self.dispatch_listen()
        self.listen()
        if time.monotonic() < self.next_scan:
            return
        eligible = []
        for device in self.store.devices():
            if device.get("automatic_synced_today") or device.get("sync_mode", "interval") != "interval":
                continue
            if not (device["auto_sync"] and device["paired"] and device["bindings"] and (device["transport"] == "homehub" or device["exclusive"])):
                continue
            if time.monotonic() >= self.next_attempt.get(device["id"], 0):
                eligible.append(device)
        groups = sorted({(d["transport"], d["adapter"]) for d in eligible})
        if not groups:
            return
        transport, adapter = groups[self.scan_group % len(groups)]
        self.scan_group += 1
        group = [d for d in eligible if (d["transport"], d["adapter"]) == (transport, adapter)]
        # Even absent devices must wait for their configured interval, not scan every 2s.
        try:
            result = self.runner({"action": "scan", "adapter": adapter, "transport": transport}, self.store.directory)
            if result.get("error"):
                raise ValueError(result["error"])
            addresses = {d["address"].upper() for d in result.get("devices", [])}
            self.watch_error = None
            for device in group:
                try:
                    if device["address"] in addresses:
                        self.submit("sync", device["id"], automatic=True)
                    else:
                        with self.lock:
                            if device["id"] in self.pending:
                                continue
                            job = self.store.create_job(device["id"], "sync")
                        self.store.update_job(job, "skipped", "検索で機器が見つからないためスキップしました。次の同期周期に再確認します")
                except ValueError:
                    # A concurrent manual request or registration removal takes precedence.
                    pass
        except Exception as error:
            self.watch_error = str(error) or type(error).__name__
        finally:
            for device in group:
                self.next_attempt[device["id"]] = time.monotonic() + device["interval"]
            self.next_scan = time.monotonic() + 2

    def loop(self):
        while not self.stop_event.is_set():
            if self.store.error:
                self.stop_event.wait(2)
                continue
            try:
                item = self.queue.get(timeout=2)
            except queue.Empty:
                try:
                    self.schedule()
                except Exception:
                    # A transient DB error must not permanently stop the worker.
                    self.stop_event.wait(5)
                continue
            try:
                self.store.require_available()
                self.process(item)
            except Exception as error:
                self.watch_error = str(error)
            finally:
                self.queue.task_done()

    def listener_status(self):
        with self.lock:
            waiting = [{'device_id': did, 'attempt': ticket['attempt'],
                        'seconds': max(0, int(ticket['due'] - time.monotonic() + 0.999))}
                       for did, ticket in self.listen_delayed.items()]
            busy = bool(self.pending)
        return {'ready': self.listen_ready, 'error': self.listen_error,
                'configured': self.watch_configured, 'busy': busy, 'waiting': waiting,
                'completed_today': [d['id'] for d in self.store.devices() if d['auto_sync'] and d.get('automatic_synced_today')]}

    def diagnostics(self):
        root = self.store.directory.parent
        bridge = root / "run/ble.sock"
        status = {"mode": "unavailable", "bridge": False,
                  "dbus": Path("/run/dbus/system_bus_socket").exists(),
                  "bleak": bool(importlib.util.find_spec("bleak")) or (root / "python/bleak").is_dir(),
                  "worker_alive": self.thread.is_alive(), "collection_paused": bool(self.store.error),
                  "data_path": str(self.store.path),
                  "watch_error": self.watch_error, "listener": self.listener_status(),
                  "hardware_verified": False}
        devices = [] if self.store.error else self.store.devices()
        shared = not devices or any(d['transport'] == 'homehub' for d in devices)
        if shared:
            bridge = HOMEHUB_SOCKET
        if bridge.exists():
            try:
                status.update(bridge_request(bridge))
                status["mode"] = "homehub" if bridge == HOMEHUB_SOCKET else "container"
                status["bridge"] = True
            except (OSError, ValueError, http.client.HTTPException) as error:
                status["bridge_error"] = str(error)
        elif not shared:
            status['mode'] = 'native'
        status['expected_mode'] = 'homehub' if shared else 'direct'
        status['socket_path'] = str(bridge)
        status['guidance'] = []
        if shared and not status['bridge']:
            status['guidance'].append('共通Bluetoothサービスに接続できません。QnapHomeHub 0.3.0以降のradioサービスの稼働とソケットの共有・権限を確認してください。NAS本体へのPython BLE・D-Bus追加は不要です。')
        elif status['mode'] == 'native' and not (status['dbus'] and status['bleak']):
            status['guidance'].append('直接接続に必要なPython BLEまたはD-Busがありません。専用Bluetoothコンテナのセットアップを確認してください。')
        if status.get('bluezError'):
            status['guidance'].append('共通radioのBlueZ復帰に失敗しています。HomeHubのradioログを確認してください。')
        if self.store.error:
            status['guidance'].append('データ保護のため収集を停止しています。バックアップから復旧後、機器設定を確認してください。')
        return status
