"""Private, durable files and bounded, read-only SQLite validation."""
from contextlib import contextmanager, closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time


class DataUnavailable(RuntimeError):
    pass


class CorruptDatabase(ValueError):
    pass


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.atomic-', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
            sync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DataUnavailable('処理が実行中です。SelfCareの停止または処理の完了を待ってください') from None
        yield
    finally:
        os.close(fd)


def corruption(error):
    code = getattr(error, 'sqlite_errorcode', 0) & 255
    # Python 3.8 does not expose sqlite_errorcode.
    return code in (11, 26) or any(s in str(error).lower() for s in
        ('database disk image is malformed', 'file is not a database'))


SCHEMA = {
    'users': {'id', 'name'}, 'devices': {'id', 'config'}, 'pairing': {'address', 'key'},
    'measurements': {'id', 'fingerprint', 'user_id', 'device_id', 'slot', 'measured_at',
                     'received_at', 'kind', 'values_json', 'source', 'raw', 'note'},
    'deleted_measurements': {'fingerprint'},
    'jobs': {'id', 'device_id', 'action', 'state', 'created_at', 'finished_at', 'message', 'result'},
}


def check_database(path, full=False, timeout=10):
    """Never create a missing database. Timeout/lock/IO errors are not corruption."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise CorruptDatabase('測定DBが見つからないか、空のファイルになっています')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            deadline = time.monotonic() + timeout
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.execute('BEGIN')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise CorruptDatabase('対応するDB形式ではありません。元データを保持して復旧してください')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, columns in SCHEMA.items():
                if table not in tables or not columns <= {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}:
                    raise CorruptDatabase('測定DBの必須テーブル・列が不足しています')
            result = db.execute('PRAGMA ' + ('integrity_check' if full else 'quick_check') + '(1)').fetchone()
            if result != ('ok',):
                raise CorruptDatabase('測定DBの整合性検査で異常を検出しました')
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise CorruptDatabase('測定DBの利用者・機器と記録の参照関係に異常があります')
            # JSON is interpreted by the UI and collectors; SQLite integrity alone cannot validate it.
            for value, in db.execute('SELECT config FROM devices'):
                if not isinstance(json.loads(value), dict):
                    raise CorruptDatabase('機器設定のJSON形式が不正です')
            for value, in db.execute('SELECT values_json FROM measurements'):
                if time.monotonic() > deadline:
                    raise TimeoutError('DB検査が時間内に終了しませんでした。容量・負荷を確認してください')
                if not isinstance(json.loads(value), dict):
                    raise CorruptDatabase('測定値のJSON形式が不正です')
            return {'records': db.execute('SELECT COUNT(*) FROM measurements').fetchone()[0],
                    'users': db.execute('SELECT COUNT(*) FROM users').fetchone()[0]}
    except (json.JSONDecodeError, TypeError):
        raise CorruptDatabase('保存されたJSONデータが不正です') from None
    except sqlite3.DatabaseError as error:
        if corruption(error):
            raise CorruptDatabase('測定DBの破損を検出しました') from error
        raise


def snapshot(source, destination, timeout=120):
    """Online backup includes committed WAL records; never copy a live DB file."""
    source, destination = Path(source), Path(destination)
    check_database(source)
    deadline = time.monotonic() + timeout
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('DBバックアップが時間内に終了しませんでした')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        with closing(sqlite3.connect(destination)) as target:
            db.backup(target, pages=256, progress=progress, sleep=0.1)
            target.execute('PRAGMA journal_mode=DELETE')
    os.chmod(destination, 0o600)
    result = check_database(destination, full=True, timeout=30)
    with destination.open('rb') as stream:
        os.fsync(stream.fileno())
    sync_dir(destination.parent)
    return result
