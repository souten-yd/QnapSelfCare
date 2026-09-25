"""Persistent, validated health records. No network or Bluetooth side effects."""
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid
import threading

from durability import (CorruptDatabase, DataUnavailable, check_database, corruption, write_json)
import omron_csv
import wellness

MODELS = {"HEM-6232T": 2, "HBF-228T": 4}
METRICS = {"systolic": (1, 350), "diastolic": (1, 250), "pulse": (1, 300),
           "weight": (1, 400), "body_fat": (0, 100), "muscle": (0, 100),
           "visceral_fat": (0, 100), "bmi": (1, 150), "bmr": (1, 10000),
           "body_age": (1, 150)}
CSV_FIELDS = ["user_id", "device_id", "slot", "measured_at", "kind", *METRICS, "note"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def text(value, field, maximum=100):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field}は1〜{maximum}文字で入力してください")
    return value.strip()


def identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError(f"{field}が不正です")
    return value


def integer(value, field, minimum, maximum):
    if isinstance(value, bool):
        raise ValueError(f"{field}が不正です")
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field}が不正です") from None
    if str(result) != str(value) or not minimum <= result <= maximum:
        raise ValueError(f"{field}は{minimum}〜{maximum}の整数です")
    return result


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("測定日時を指定してください")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("測定日時はISO 8601形式で指定してください") from None
    if dt.tzinfo is None:
        raise ValueError("測定日時にタイムゾーンが必要です（例: +09:00）")
    if not 2000 <= dt.year <= 2100:
        raise ValueError("測定年が範囲外です")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, directory, state_directory=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "measurements.sqlite3"
        self.lock = threading.RLock()
        self.error = None
        state_directory = Path(state_directory) if state_directory is not None else self.directory
        self.guard = state_directory / 'database.guard.json'
        self.marker = state_directory / 'database.initialized.json'
        self.pending_restore = state_directory / 'restore.pending.json'
        self._initializing = True
        try:
            if self.pending_restore.exists() or self.guard.exists():
                raise CorruptDatabase('保護モードです。診断を確認し、停止中に検証済みバックアップから復旧してください')
            if self.path.exists():
                check_database(self.path)
            elif self.marker.exists() or any((state_directory / 'backups').glob('selfcare-*.zip')):
                raise CorruptDatabase('以前の測定DBが見つかりません。空のDBは作成しません')
            self._initialize()
            write_json(self.marker, {'schema': 1})
        except (CorruptDatabase, DataUnavailable, sqlite3.DatabaseError, OSError) as error:
            self.error = str(error)
            if isinstance(error, CorruptDatabase) or corruption(error):
                self.protect(self.error)
        finally:
            self._initializing = False

    def protect(self, message):
        with self.lock:
            self.error = message
            try:
                write_json(self.guard, {'message': message, 'detected_at': now()})
            except OSError:
                pass  # In-memory gate still blocks writes when the volume is full/read-only.

    def require_available(self):
        if self.error:
            raise DataUnavailable('保存先を保護しています: ' + self.error)

    def _initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pairing(address TEXT PRIMARY KEY, key TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS measurements(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    device_id TEXT REFERENCES devices(id), slot INTEGER,
                    measured_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    kind TEXT NOT NULL, values_json TEXT NOT NULL,
                    source TEXT NOT NULL, raw TEXT, note TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS measurement_history ON measurements(user_id, measured_at);
                CREATE TABLE IF NOT EXISTS deleted_measurements(fingerprint TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, device_id TEXT, action TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
                    message TEXT NOT NULL DEFAULT '', result TEXT);
                CREATE TABLE IF NOT EXISTS wellness_profiles(
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meal_notes(
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, note TEXT NOT NULL, calories REAL);
                CREATE INDEX IF NOT EXISTS meal_notes_user_time ON meal_notes(user_id,created_at);
                CREATE TABLE IF NOT EXISTS ai_settings(id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL);
                PRAGMA user_version=1;
            ''')
            db.execute("UPDATE jobs SET state='failed',finished_at=?,message=? WHERE state IN ('queued','running')",
                       (now(), "サービス再起動により中断されました。再実行してください"))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        with self.lock:
            self.require_available()
            db = None
            try:
                if not self._initializing and (not self.path.exists() or self.path.stat().st_size == 0):
                    self.protect('測定DBが消失または空になりました。新規作成を停止しました')
                    self.require_available()
                uri = self.path.resolve().as_uri() + ('?mode=rwc' if self._initializing else '?mode=rw')
                db = sqlite3.connect(uri, uri=True, timeout=15)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
            except sqlite3.DatabaseError as error:
                if corruption(error):
                    self.protect('測定DBの破損を検出しました。元のDBを保持しています')
                    raise DataUnavailable(self.error) from error
                raise
            finally:
                if db is not None:
                    db.close()

    def users(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM users ORDER BY name,id")]

    def save_user(self, payload):
        name = text(payload.get("name"), "利用者名", 60)
        uid = identifier(payload.get("id") or uuid.uuid4().hex, "利用者ID")
        with self.connect() as db:
            db.execute("INSERT INTO users VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                       (uid, name))
        return {"id": uid, "name": name}

    def delete_user(self, identifier):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM measurements WHERE user_id=?", (identifier,)).fetchone():
                raise ValueError("記録のある利用者は削除できません")
            for row in db.execute("SELECT config FROM devices"):
                if identifier in json.loads(row[0])["bindings"].values():
                    raise ValueError("機器の利用者割当を先に解除してください")
            db.execute("DELETE FROM users WHERE id=?", (identifier,))

    def wellness_profile(self, user_id):
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                raise ValueError('利用者が見つかりません')
            row = db.execute('SELECT config FROM wellness_profiles WHERE user_id=?', (user_id,)).fetchone()
            return wellness.validate(json.loads(row[0]) if row else {})

    def save_wellness_profile(self, user_id, payload):
        current = self.wellness_profile(user_id)
        if not isinstance(payload, dict) or set(payload) - (set(wellness.DEFAULT) - {'plan_updated_at'}):
            raise ValueError('健康設定の項目が不正です')
        result = wellness.validate(dict(current, **payload))
        if 'plan_text' in payload and result['plan_text'] != current['plan_text']:
            result['plan_updated_at'] = now()
        with self.connect() as db:
            db.execute('INSERT INTO wellness_profiles VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET config=excluded.config',
                       (user_id, json.dumps(result, ensure_ascii=False)))
        return result

    def meals(self, user_id, limit=100):
        self.wellness_profile(user_id)
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM meal_notes WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ?', (user_id, limit))]

    def add_meal(self, user_id, payload):
        self.wellness_profile(user_id)
        if not isinstance(payload, dict) or set(payload) - {'note', 'calories', 'created_at'}:
            raise ValueError('食事記録の項目が不正です')
        note = text(payload.get('note'), '食事メモ', 1000)
        calories = wellness.number(payload.get('calories'), 0, 10000, '目安カロリー', True)
        when = timestamp(payload.get('created_at') or now())
        row = {'id': uuid.uuid4().hex, 'user_id': user_id, 'created_at': when,
               'note': note, 'calories': calories}
        with self.connect() as db:
            db.execute('INSERT INTO meal_notes VALUES(:id,:user_id,:created_at,:note,:calories)', row)
        return row

    def delete_meal(self, user_id, meal_id):
        self.wellness_profile(user_id)
        with self.connect() as db:
            if not db.execute('DELETE FROM meal_notes WHERE id=? AND user_id=?',
                              (identifier(meal_id, '食事記録ID'), user_id)).rowcount:
                raise ValueError('食事記録が見つかりません')

    def wellness_summary(self, user_id):
        profile = self.wellness_profile(user_id)
        # The latest 2000 records suffice for current trends without exporting full history.
        records = self.records(user_id=user_id, limit=2000)['records']
        return wellness.summarize(profile, records, self.meals(user_id, 100))

    def devices(self, include_archived=False):
        with self.connect() as db:
            result = [dict(json.loads(r["config"]), id=r["id"]) for r in db.execute("SELECT * FROM devices")]
            if not include_archived:
                result = [d for d in result if not d.get("archived", False)]
            for device in result:
                device["paired"] = bool(db.execute("SELECT 1 FROM pairing WHERE address=?", (device["address"],)).fetchone())
        return result

    def device(self, identifier):
        return next((d for d in self.devices() if d["id"] == identifier), None)

    def validate_device(self, payload, user_ids):
        model = payload.get("model")
        if model not in MODELS:
            raise ValueError("対応機種はHEM-6232T / HBF-228Tです")
        address = payload.get("address", "").upper()
        if address and not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            raise ValueError("Bluetoothアドレスの形式が不正です")
        adapter = payload.get("adapter", "hci0")
        if not re.fullmatch(r"hci\d{1,2}", adapter):
            raise ValueError("Bluetoothアダプターはhci0等で指定してください")
        bindings = payload.get("bindings", {})
        if not isinstance(bindings, dict):
            raise ValueError("利用者割当が不正です")
        for slot, user in bindings.items():
            integer(slot, "機器の利用者番号", 1, MODELS[model])
            if user not in user_ids:
                raise ValueError("利用者が登録されていません")
        for key in ("auto_sync", "exclusive"):
            if not isinstance(payload.get(key, False), bool):
                raise ValueError(f"{key}が不正です")
        transport = payload.get("transport", "homehub")
        if transport not in ("homehub", "direct"):
            raise ValueError("Bluetooth接続方式が不正です")
        offset = integer(payload.get("utc_offset_minutes", 540), "時差（分）", -720, 840)
        return {"name": text(payload.get("name", model), "機器名", 60), "model": model,
                "address": address, "adapter": adapter, "bindings": bindings, "transport": transport,
                "auto_sync": payload.get("auto_sync", False), "exclusive": payload.get("exclusive", False),
                "interval": integer(payload.get("interval", 300), "同期間隔", 60, 86400),
                "utc_offset_minutes": offset}

    def save_device(self, payload):
        did = identifier(payload.get("id") or uuid.uuid4().hex, "機器ID")
        config = self.validate_device(payload, {u["id"] for u in self.users()})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT config FROM devices WHERE id=?", (did,)).fetchone()
            if old and json.loads(old[0]).get("archived"):
                raise ValueError("削除済み機器です。新規登録してください")
            if old and db.execute("SELECT 1 FROM measurements WHERE device_id=?", (did,)).fetchone():
                old_config = json.loads(old[0])
                if (old_config["model"], old_config["address"]) != (config["model"], config["address"]):
                    raise ValueError("記録済み機器の型番・アドレスは変更できません。別の機器として登録してください")
            for row in db.execute("SELECT id, config FROM devices WHERE id != ?", (did,)):
                if config["address"] and not json.loads(row["config"]).get("archived") and json.loads(row["config"])["address"] == config["address"]:
                    raise ValueError("同じBluetoothアドレスが登録済みです")
            db.execute("INSERT INTO devices VALUES(?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                       (did, json.dumps(config)))
        return self.device(did)

    def delete_device(self, identifier):
        # Archive the registration so measurement provenance and fingerprints survive.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config FROM devices WHERE id=?", (identifier,)).fetchone()
            if not row or json.loads(row[0]).get("archived"):
                return
            if db.execute("SELECT 1 FROM jobs WHERE device_id=? AND state IN ('queued','running')", (identifier,)).fetchone():
                raise ValueError("この機器の処理中です。自動同期をOFFにして処理完了後に削除してください")
            config = json.loads(row[0])
            config.update(archived=True, auto_sync=False)
            db.execute("UPDATE devices SET config=? WHERE id=?", (json.dumps(config), identifier))
            db.execute("DELETE FROM pairing WHERE address=?", (config["address"],))
            # The recovery key must not be reused if this registration is removed.
            (self.directory.parent / "config" / ("pair-" + identifier + ".json")).unlink(missing_ok=True)

    def validate_record(self, record, users, devices, source):
        if not isinstance(record, dict):
            raise ValueError("測定データはオブジェクトで指定してください")
        user = record.get("user_id")
        if user not in users:
            raise ValueError("利用者を登録・選択してください")
        device_id = record.get("device_id") or None
        slot = record.get("slot") or None
        if device_id:
            if device_id not in devices:
                raise ValueError("機器が未登録です")
            device = devices[device_id]
            slot = integer(slot, "利用者番号", 1, MODELS[device["model"]])
        elif slot is not None:
            raise ValueError("利用者番号には機器IDが必要です")
        kind = record.get("kind")
        if kind not in ("blood_pressure", "body_composition"):
            raise ValueError("測定種別が不正です")
        if device_id and ((devices[device_id]["model"] == "HEM-6232T") != (kind == "blood_pressure")):
            raise ValueError("機器と測定種別が一致しません")
        values = record.get("values", {})
        if not isinstance(values, dict):
            raise ValueError("測定値が不正です")
        allowed = {"systolic", "diastolic", "pulse"} if kind == "blood_pressure" else set(METRICS) - {"systolic", "diastolic", "pulse"}
        if set(values) - allowed:
            raise ValueError("測定種別に対応しない項目があります")
        normalized = {}
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError(f"{name}は有限の数値で指定してください")
            low, high = METRICS[name]
            if not low <= value <= high:
                raise ValueError(f"{name}が保存可能な範囲外です")
            normalized[name] = round(float(value), 4)
        required = {"systolic", "diastolic", "pulse"} if kind == "blood_pressure" else {"weight"}
        if not required <= normalized.keys():
            raise ValueError("必要な測定値が不足しています")
        if kind == "blood_pressure" and normalized["systolic"] <= normalized["diastolic"]:
            raise ValueError("最高血圧は最低血圧より大きい値にしてください")
        note = record.get("note", "")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("メモは1000文字以内です")
        raw = record.get("raw")
        if raw is not None and (not isinstance(raw, str) or len(raw) > 4096):
            raise ValueError("元データが不正です")
        result = dict(user_id=user, device_id=device_id, slot=slot, measured_at=timestamp(record.get("measured_at")),
                      kind=kind, values=normalized, source=text(source, "取込元", 40), raw=raw, note=note)
        identity = {k: result[k] for k in ("user_id", "device_id", "slot", "measured_at", "kind", "values")}
        result["fingerprint"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return result

    def add_records(self, records, source="manual"):
        if not isinstance(records, list) or not 1 <= len(records) <= 10000:
            raise ValueError("一度の取込は1〜10000件です")
        users = {u["id"] for u in self.users()}
        devices = {d["id"]: d for d in self.devices()}
        normalized = []
        for index, record in enumerate(records):
            try:
                normalized.append(self.validate_record(record, users, devices, source))
            except ValueError as error:
                raise ValueError(f"{index + 1}件目: {error}") from None
        with self.connect() as db:
            inserted = sum(self._insert(db, record) for record in normalized)
        return {"inserted": inserted, "duplicates": len(records) - inserted}

    @staticmethod
    def _insert(db, record):
        if db.execute("SELECT 1 FROM deleted_measurements WHERE fingerprint=?", (record["fingerprint"],)).fetchone():
            return 0
        cursor = db.execute('''INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(fingerprint) DO NOTHING''',
            (record.get("id") or uuid.uuid4().hex, record["fingerprint"], record["user_id"], record["device_id"], record["slot"],
             record["measured_at"], record.get("received_at") or now(), record["kind"], json.dumps(record["values"]), record["source"], record["raw"], record["note"]))
        return cursor.rowcount

    @staticmethod
    def unpack(row):
        record = dict(row)
        record["values"] = json.loads(record.pop("values_json"))
        record.pop("fingerprint", None)
        return record

    def records(self, user_id=None, kind=None, since=None, until=None, limit=200, offset=0):
        terms, params = [], []
        for field, value in (("user_id", user_id), ("kind", kind)):
            if value:
                terms.append(f"{field}=?")
                params.append(value)
        for op, value in ((">=", since), ("<=", until)):
            if value:
                terms.append(f"measured_at {op} ?")
                params.append(timestamp(value))
        where = " WHERE " + " AND ".join(terms) if terms else ""
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM measurements" + where, params).fetchone()[0]
            rows = db.execute("SELECT * FROM measurements" + where + " ORDER BY measured_at DESC,id DESC LIMIT ? OFFSET ?",
                              params + [limit, offset])
            return {"total": count, "records": [self.unpack(row) for row in rows]}

    def delete_record(self, identifier):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO deleted_measurements SELECT fingerprint FROM measurements WHERE id=?", (identifier,))
            db.execute("DELETE FROM measurements WHERE id=?", (identifier,))

    def edit_record(self, identifier, payload):
        record = self.validate_record(payload, {u["id"] for u in self.users()},
                                      {d["id"]: d for d in self.devices(include_archived=True)}, "edited")
        with self.connect() as db:
            old = db.execute("SELECT * FROM measurements WHERE id=?", (identifier,)).fetchone()
            if not old:
                raise ValueError("記録が見つかりません")
            duplicate = db.execute("SELECT id FROM measurements WHERE fingerprint=? AND id!=?", (record["fingerprint"], identifier)).fetchone()
            if duplicate:
                raise ValueError("同じ測定記録が既にあります")
            if old["fingerprint"] != record["fingerprint"]:
                db.execute("INSERT OR IGNORE INTO deleted_measurements VALUES(?)", (old["fingerprint"],))
            db.execute("UPDATE measurements SET fingerprint=?,user_id=?,device_id=?,slot=?,measured_at=?,kind=?,values_json=?,source='edited',note=? WHERE id=?",
                       (record["fingerprint"], record["user_id"], record["device_id"], record["slot"], record["measured_at"],
                        record["kind"], json.dumps(record["values"]), record["note"], identifier))
        return {"updated": True}

    def export_csv(self, **filters):
        records = self.records(limit=1000000, **filters)["records"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, CSV_FIELDS)
        writer.writeheader()
        for record in records:
            row = {k: record.get(k, "") for k in CSV_FIELDS}
            row.update(record["values"])
            for key, value in row.items():
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    row[key] = "'" + value
            writer.writerow(row)
        return "\ufeff" + output.getvalue()

    def import_csv(self, content, user_id=None):
        if not isinstance(content, str) or len(content) > 8 * 1024 * 1024:
            raise ValueError("CSVは8MB以内です")
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
        if not reader.fieldnames or not {"measured_at", "kind"} <= set(reader.fieldnames):
            raise ValueError("QnapSelfCare形式のCSVが必要です。テンプレートをダウンロードしてください")
        records = []
        for row in reader:
            if len(records) >= 10000:
                raise ValueError("一度の取込は10000件までです")
            values = {}
            for key in METRICS:
                if row.get(key):
                    try:
                        values[key] = float(row[key])
                    except ValueError:
                        raise ValueError(f"CSV {reader.line_num}行目の{key}が数値ではありません") from None
            records.append(dict(user_id=user_id or row.get("user_id"), device_id=row.get("device_id"),
                                slot=row.get("slot"), measured_at=row.get("measured_at"), kind=row.get("kind"),
                                values=values, note=row.get("note") or ""))
        return self.add_records(records, "csv")

    def omron_history(self, content, user_id, apply=False, preview_token=None):
        """Preview or atomically add historical OMRON CSV measurements for one user."""
        users = {u['id'] for u in self.users()}
        if user_id not in users:
            raise ValueError('取込先の利用者を選択してください')
        kind, rows = omron_csv.parse(content, user_id)
        token = hashlib.sha256((user_id + '\0' + content).encode()).hexdigest()
        if apply and preview_token != token:
            raise ValueError('ファイルまたは利用者が変更されました。取込前の確認をやり直してください')
        normalized = []
        for index, row in enumerate(rows, 2):
            try:
                normalized.append(self.validate_record(row, users, {}, 'omron_csv'))
            except ValueError as error:
                raise ValueError(f'CSV {index}行目: {error}') from None
        seen = set()
        inserted = 0
        primary = ('systolic', 'diastolic', 'pulse') if kind == 'blood_pressure' else ('weight',)
        with self.connect() as db:
            for row in normalized:
                fingerprint = row['fingerprint']
                identity = (row['measured_at'], *(row['values'][key] for key in primary))
                same = db.execute('SELECT values_json FROM measurements WHERE user_id=? AND kind=? AND measured_at=?',
                                  (user_id, kind, row['measured_at'])).fetchall()
                duplicate = identity in seen or bool(db.execute(
                    'SELECT 1 FROM deleted_measurements WHERE fingerprint=?', (fingerprint,)).fetchone())
                if not duplicate:
                    duplicate = any(all(json.loads(existing[0]).get(key) == row['values'][key] for key in primary)
                                    for existing in same)
                seen.add(identity)
                if duplicate:
                    continue
                if apply:
                    inserted += self._insert(db, row)
                else:
                    inserted += 1
        return {'kind': kind, 'model': omron_csv.MODELS[kind], 'total': len(rows),
                'importable': inserted, 'duplicates': len(rows) - inserted,
                'first': min(r['measured_at'] for r in normalized),
                'last': max(r['measured_at'] for r in normalized), 'preview_token': token}

    def backup(self):
        with self.connect() as db:
            db.execute("BEGIN")
            return {"format": "QnapSelfCare", "schema": 1, "created_at": now(),
                    "users": [dict(r) for r in db.execute("SELECT * FROM users")],
                    "devices": [dict(json.loads(r["config"]), id=r["id"]) for r in db.execute("SELECT * FROM devices")],
                    "wellness_profiles": [dict(json.loads(r['config']), user_id=r['user_id'])
                                          for r in db.execute('SELECT * FROM wellness_profiles')],
                    "meal_notes": [dict(r) for r in db.execute('SELECT * FROM meal_notes')],
                    "ai_settings": (json.loads(value[0]) if (value := db.execute('SELECT config FROM ai_settings WHERE id=1').fetchone()) else None),
                    "deleted": [r[0] for r in db.execute("SELECT fingerprint FROM deleted_measurements")],
                    "records": [self.unpack(r) for r in db.execute("SELECT * FROM measurements")]}

    def restore(self, backup):
        if not isinstance(backup, dict) or backup.get("format") != "QnapSelfCare" or backup.get("schema") != 1:
            raise ValueError("対応するバックアップ形式ではありません")
        users, devices, records = (backup.get(k) for k in ("users", "devices", "records"))
        if not all(isinstance(v, list) for v in (users, devices, records)) or len(records) > 100000:
            raise ValueError("バックアップが不正、または100000件を超えています")
        with self.connect() as db:
            if db.execute("SELECT 1 FROM users").fetchone() or db.execute("SELECT 1 FROM devices").fetchone():
                raise ValueError("復元は利用者・機器・記録が空の保存先で実行してください。既存データへはCSVで追加できます")
            user_ids = set()
            for user in users:
                uid = identifier(user.get("id"), "利用者ID")
                db.execute("INSERT INTO users VALUES(?,?)", (uid, text(user.get("name"), "利用者名", 60)))
                user_ids.add(uid)
            device_map = {}
            for device in devices:
                did = identifier(device.get("id"), "機器ID")
                config = self.validate_device(device, user_ids)
                config["auto_sync"] = False
                if not isinstance(device.get("archived", False), bool):
                    raise ValueError("削除済み機器の状態が不正です")
                if device.get("archived"):
                    config["archived"] = True
                db.execute("INSERT INTO devices VALUES(?,?)", (did, json.dumps(config)))
                device_map[did] = config
            for record in records:
                normalized = self.validate_record(record, user_ids, device_map, record.get("source", "backup"))
                normalized["id"] = identifier(record.get("id"), "記録ID")
                normalized["received_at"] = timestamp(record.get("received_at"))
                self._insert(db, normalized)
            for fingerprint in backup.get("deleted", []):
                if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                    raise ValueError("削除記録の形式が不正です")
                db.execute("INSERT OR IGNORE INTO deleted_measurements VALUES(?)", (fingerprint,))
            for item in backup.get('wellness_profiles', []):
                if not isinstance(item, dict) or item.get('user_id') not in user_ids:
                    raise ValueError('健康設定の利用者が不正です')
                value = wellness.validate({key: item[key] for key in item if key != 'user_id'})
                db.execute('INSERT INTO wellness_profiles VALUES(?,?)',
                           (item['user_id'], json.dumps(value, ensure_ascii=False)))
            for item in backup.get('meal_notes', []):
                if not isinstance(item, dict) or item.get('user_id') not in user_ids:
                    raise ValueError('食事記録の利用者が不正です')
                mid = identifier(item.get('id'), '食事記録ID')
                meal = text(item.get('note'), '食事メモ', 1000)
                calories = wellness.number(item.get('calories'), 0, 10000, '目安カロリー', True)
                db.execute('INSERT INTO meal_notes VALUES(?,?,?,?,?)',
                           (mid, item['user_id'], timestamp(item.get('created_at')), meal, calories))
            if backup.get('ai_settings') is not None:
                from wellness_ai import validate as validate_ai
                db.execute('INSERT INTO ai_settings VALUES(1,?)', (json.dumps(validate_ai(backup['ai_settings'])),))
        return {"restored": len(records), "users": len(users), "devices": len(devices)}

    def pairing_key(self, address, key=None):
        with self.connect() as db:
            if key is not None:
                db.execute("INSERT INTO pairing VALUES(?,?) ON CONFLICT(address) DO UPDATE SET key=excluded.key", (address, key))
            row = db.execute("SELECT key FROM pairing WHERE address=?", (address,)).fetchone()
            return row[0] if row else None

    def jobs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,device_id,action,state,created_at,finished_at,message,result FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 30")]

    def create_job(self, device_id, action):
        identifier = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if device_id:
                device = db.execute("SELECT config FROM devices WHERE id=?", (device_id,)).fetchone()
                if not device or json.loads(device[0]).get("archived"):
                    raise ValueError("機器が未登録または削除済みです")
            db.execute("INSERT INTO jobs(id,device_id,action,state,created_at) VALUES(?,?,?,'queued',?)",
                       (identifier, device_id, action, now()))
            db.execute("DELETE FROM jobs WHERE state NOT IN ('queued','running') AND id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 200)")
        return identifier

    def update_job(self, identifier, state, message="", result=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,finished_at=?,message=?,result=? WHERE id=?",
                       (state, now() if state in ("done", "failed", "skipped") else None, message[:2000], json.dumps(result) if result is not None else None, identifier))
