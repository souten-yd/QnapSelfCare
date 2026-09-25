"""Verified scheduled backups and offline recovery for QnapSelfCare."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import zipfile

from durability import (CorruptDatabase, DataUnavailable, check_database, file_lock,
                        snapshot, sync_dir, write_json)

DEFAULTS = {'enabled': True, 'interval_hours': 168, 'keep': 14}
BACKUP_NAME = re.compile(r'selfcare-\d{8}T\d{6}Z-[a-f0-9]{8}\.zip')
CONFIG_NAME = re.compile(r'config/pair-[A-Za-z0-9_-]{1,64}\.json')
MAX_SIZE = 2 * 1024 ** 3
MIN_FREE = 64 * 1024 ** 2


def read_object(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError('設定・状態ファイルがJSONオブジェクトではありません')
    return value


def settings(value):
    if (set(value) != set(DEFAULTS) or type(value['enabled']) is not bool
            or type(value['interval_hours']) is not int or value['interval_hours'] not in (6, 12, 24, 168)
            or type(value['keep']) is not int or not 2 <= value['keep'] <= 90):
        raise ValueError('間隔は6・12・24・168時間、保存世代は2〜90で指定してください')
    return dict(value)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def manifest(archive):
    with zipfile.ZipFile(archive) as source:
        info = source.getinfo('manifest.json')
        if info.file_size > 1024 * 1024:
            raise ValueError('バックアップ情報が大きすぎます')
        data = json.loads(source.read(info))
    if (not isinstance(data, dict) or data.get('format') != 'QnapSelfCare-snapshot-1'
            or type(data.get('created_at')) not in (int, float) or not 0 < data['created_at'] < 10**11
            or data.get('reason') not in ('automatic', 'manual')
            or not isinstance(data.get('files'), dict)):
        raise ValueError('SelfCareの完全バックアップではありません')
    counts = data.get('counts')
    if (not isinstance(counts, dict) or set(counts) != {'users', 'records'}
            or any(type(v) is not int or v < 0 for v in counts.values())):
        raise ValueError('バックアップの件数情報が不正です')
    return data


def unpack_verified(archive, destination):
    """Whitelist names and sizes; never trust ZIP paths or extract symlinks."""
    data = manifest(archive)
    destination = Path(destination)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        names = [i.filename for i in entries]
        if (len(names) > 2048 or len(set(names)) != len(names)
                or set(names) != set(data['files']) | {'manifest.json'}
                or 'measurements.sqlite3' not in data['files']
                or sum(i.file_size for i in entries) > MAX_SIZE):
            raise ValueError('バックアップの内容・サイズが不正です')
        if shutil.disk_usage(destination).free < sum(i.file_size for i in entries) + MIN_FREE:
            raise OSError('バックアップの検証に必要な空き容量が不足しています')
        for name, metadata in data['files'].items():
            if name != 'measurements.sqlite3' and not CONFIG_NAME.fullmatch(name):
                raise ValueError('バックアップに許可されないパスがあります')
            info = source.getinfo(name)
            if (not isinstance(metadata, dict) or metadata.get('size') != info.file_size
                    or not re.fullmatch(r'[a-f0-9]{64}', str(metadata.get('sha256', '')))):
                raise ValueError('バックアップ情報と内容が一致しません')
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source.open(info) as stream, output.open('xb') as target:
                os.chmod(output, 0o600)
                shutil.copyfileobj(stream, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if digest(output) != metadata['sha256']:
                raise ValueError('バックアップのSHA-256が一致しません。復元できません')
            if name != 'measurements.sqlite3':
                value = read_object(output)
                if not re.fullmatch(r'[a-fA-F0-9]{32}', str(value.get('key', ''))):
                    raise ValueError('バックアップのペアリング設定が不正です')
    counts = check_database(destination / 'measurements.sqlite3', full=True, timeout=30)
    if counts != data.get('counts'):
        raise ValueError('バックアップの記録件数が一致しません')
    return data


class ProtectionManager:
    def __init__(self, store):
        self.store = store
        self.root = store.directory.parent
        self.directory = self.root / 'backups'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.config_path = self.directory / 'settings.json'
        self.state_path = self.directory / 'status.json'
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.busy = False
        self.requested = None
        self.config_error = None
        self.config = dict(DEFAULTS)
        try:
            self.config = settings(read_object(self.config_path))
        except FileNotFoundError:
            write_json(self.config_path, self.config)
        except (OSError, ValueError) as error:
            self.config_error = 'バックアップ設定を読めません。設定を保存し直してください: ' + str(error)
        try:
            self.state = read_object(self.state_path)
            for key in ('last_success', 'last_attempt'):
                value = self.state.get(key, 0)
                if type(value) not in (int, float) or not 0 <= value < 10**11:
                    raise ValueError('バックアップ日時が不正です')
            verification = self.state.get('verification')
            if verification is not None and (not isinstance(verification, dict)
                    or type(verification.get('ok')) is not bool
                    or type(verification.get('checked_at')) not in (int, float)
                    or not BACKUP_NAME.fullmatch(str(verification.get('name', '')))):
                raise ValueError('バックアップ検査の結果が不正です')
        except FileNotFoundError:
            self.state = {}
        except (OSError, ValueError):
            self.state = {'error': '前回のバックアップ状態を読めません。新しいバックアップを作成します'}
        if self.state.get('phase') in ('running', 'verifying'):
            self.state.update(phase='failed', error='前回のバックアップが中断されました。過去の保存世代は保持しています')
        self.thread = threading.Thread(target=self.loop, daemon=True, name='selfcare-backup')

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread.ident:
            self.thread.join(timeout=3)

    def save_settings(self, value):
        value = settings(value)
        with self.lock:
            write_json(self.config_path, value)
            self.config, self.config_error = value, None
        self.wake.set()
        return self.status()

    def list_backups(self):
        result = []
        for path in sorted(self.directory.glob('selfcare-*.zip'), reverse=True):
            if not BACKUP_NAME.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                data = manifest(path)
                result.append({'name': path.name, 'created_at': data['created_at'],
                               'size': path.stat().st_size, 'counts': data.get('counts'),
                               'readable': True, 'reason': data['reason']})
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                result.append({'name': path.name, 'readable': False})
        return result

    def status(self):
        with self.lock:
            backups = self.list_backups()
            # A completed archive remains discoverable if power failed before status.json was replaced.
            last = max([0] + [b['created_at'] for b in backups if b['readable']])
            interval = self.config['interval_hours'] * 3600
            next_at = max(last + interval if last <= time.time() else 0,
                          self.state.get('last_attempt', 0) + 3600 if self.state.get('error') else 0)
            return {'settings': self.config, 'config_error': self.config_error,
                    'busy': self.busy, 'phase': self.state.get('phase', 'idle'),
                    'last_success': last or None, 'last_attempt': self.state.get('last_attempt'),
                    'next_at': next_at if self.config['enabled'] and not self.config_error else None,
                    'error': self.state.get('error'), 'warning': self.state.get('warning'),
                    'verification': self.state.get('verification'),
                    'directory': str(self.directory), 'backups': backups,
                    'recovery_required': bool(self.store.error), 'database_error': self.store.error}

    def request_backup(self):
        self.store.require_available()
        with self.lock:
            if self.busy or self.requested:
                raise DataUnavailable('バックアップが実行中です')
            self.requested = 'manual'
            self.busy = True
        self.wake.set()
        return self.status()

    def request_verification(self):
        with self.lock:
            if self.busy or self.requested:
                raise DataUnavailable('バックアップ処理が実行中です')
            backups = [b for b in self.list_backups() if b['readable']]
            if not backups:
                raise ValueError('検査できるバックアップがありません')
            latest = max(backups, key=lambda b: b['created_at'])
            self.requested = ('verify', latest['name'])
            self.busy = True
        self.wake.set()
        return self.status()

    def verify_backup(self, name):
        with self.operation:
            with self.lock:
                self.busy = True
                self.state['phase'] = 'verifying'
            result = {'name': name, 'checked_at': time.time(), 'ok': False}
            try:
                write_json(self.state_path, self.state)
                with self.download(name):
                    pass
                result['ok'] = True
            except Exception as error:
                result['error'] = str(error) or type(error).__name__
            finally:
                with self.lock:
                    self.state.update(phase='idle', verification=result)
                    self.busy = False
                    try:
                        write_json(self.state_path, self.state)
                    except OSError:
                        self.state['error'] = 'バックアップ検査の結果を保存できません。保存先を確認してください'
            return result

    def loop(self):
        while not self.stop.is_set():
            with self.lock:
                reason, self.requested = self.requested, None
            status = self.status()
            if not reason and status['next_at'] is not None and time.time() >= status['next_at']:
                reason = 'automatic'
            if isinstance(reason, tuple):
                self.verify_backup(reason[1])
            elif reason:
                self.run_backup(reason)
            self.wake.wait(30)
            self.wake.clear()

    def run_backup(self, reason='manual'):
        if not self.operation.acquire(blocking=False):
            raise DataUnavailable('バックアップが実行中です')
        try:
            with self.lock:
                self.busy = True
                self.state.update(phase='running', last_attempt=time.time(), error=None, warning=None)
            write_json(self.state_path, self.state)
            self.store.require_available()
            with file_lock(self.directory / 'backup.lock'), file_lock(self.root / 'updates/update.lock'):
                name = self.create_archive(reason)
                with self.lock:
                    self.state.update(phase='success', last_success=time.time(), error=None)
                write_json(self.state_path, self.state)
                try:
                    self.prune()
                except Exception as error:
                    self.state['warning'] = 'バックアップは保存済みですが世代整理を保留しました: ' + str(error)
                return name
        except Exception as error:
            if isinstance(error, CorruptDatabase):
                # A damaged destination/archive must not mislabel a healthy live DB.
                try:
                    check_database(self.store.path, full=True)
                except CorruptDatabase as source_error:
                    self.store.protect(str(source_error))
                except (OSError, sqlite3.DatabaseError):
                    pass
            with self.lock:
                self.state.update(phase='failed', error=str(error) or type(error).__name__)
            return None
        finally:
            with self.lock:
                self.busy = False
                try:
                    write_json(self.state_path, self.state)
                except OSError:
                    self.state['error'] = 'バックアップ状態を保存できません。保存先の容量・権限を確認してください'
            self.operation.release()

    def create_archive(self, reason):
        estimate = sum(p.stat().st_size for p in self.store.directory.glob('measurements.sqlite3*') if p.is_file())
        if estimate > MAX_SIZE or shutil.disk_usage(self.directory).free < estimate * 4 + MIN_FREE:
            raise OSError('バックアップ用の空き容量が不足、またはDBが2GBの上限を超えています')
        created = time.time()
        name = 'selfcare-' + datetime.fromtimestamp(created, timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8] + '.zip'
        with tempfile.TemporaryDirectory(prefix='.working-', dir=self.directory) as work:
            work = Path(work)
            source = work / 'source'
            source.mkdir(mode=0o700)
            counts = snapshot(self.store.path, source / 'measurements.sqlite3')
            config = self.root / 'config'
            if config.exists():
                (source / 'config').mkdir(mode=0o700)
                for path in config.iterdir():
                    if path.name.startswith('.atomic-'):
                        continue
                    if path.is_symlink() or not path.is_file() or not CONFIG_NAME.fullmatch('config/' + path.name):
                        raise ValueError('config内に未対応のファイルがあります。バックアップを中止しました')
                    try:
                        data = path.read_bytes()
                    except FileNotFoundError:
                        continue  # A completed pairing removes its temporary recovery key.
                    target = source / 'config' / path.name
                    target.write_bytes(data)
                    os.chmod(target, 0o600)
            files = {str(p.relative_to(source)): {'size': p.stat().st_size, 'sha256': digest(p)}
                     for p in source.rglob('*') if p.is_file()}
            metadata = {'format': 'QnapSelfCare-snapshot-1', 'created_at': created,
                        'reason': reason, 'counts': counts, 'files': files}
            pending = work / 'archive.zip'
            with zipfile.ZipFile(pending, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr('manifest.json', json.dumps(metadata))
                for filename in files:
                    output.write(source / filename, filename)
            os.chmod(pending, 0o600)
            verified = work / 'verify'
            verified.mkdir(mode=0o700)
            unpack_verified(pending, verified)
            self.store.require_available()
            with pending.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(pending, self.directory / name)
            sync_dir(self.directory)
        return name

    def prune(self):
        # Only complete, verified files created by this manager are eligible for deletion.
        candidates = [b for b in self.list_backups() if b['readable']]
        candidates.sort(key=lambda b: (b['created_at'], b['name']), reverse=True)
        for backup in candidates[self.config['keep']:]:
            with tempfile.TemporaryDirectory(prefix='.verify-', dir=self.directory) as work:
                unpack_verified(self.directory / backup['name'], work)
            (self.directory / backup['name']).unlink()
        sync_dir(self.directory)

    @contextmanager
    def download(self, name):
        if not BACKUP_NAME.fullmatch(name):
            raise ValueError('バックアップ名が不正です')
        path = self.directory / name
        if path.is_symlink():
            raise ValueError('シンボリックリンクは取得できません')
        # Private copy pins exactly the bytes that are verified, even if retention runs.
        with tempfile.TemporaryDirectory(prefix='.download-', dir=self.directory) as work:
            work = Path(work)
            shutil.copyfile(path, work / 'backup.zip')
            os.chmod(work / 'backup.zip', 0o600)
            unpack_verified(work / 'backup.zip', work)
            with (work / 'backup.zip').open('rb') as stream:
                yield stream

    def diagnostics(self):
        checks = []
        def add(key, label, level, message):
            checks.append(dict(id=key, label=label, level=level, message=message))
        try:
            counts = check_database(self.store.path, full=True)
            if self.store.error:
                add('database', 'データベース', 'error', '保護モードを維持しています: ' + self.store.error)
            else:
                add('database', 'データベース', 'ok', f"整合性・参照関係を確認しました（{counts['records']}件）")
        except Exception as error:
            if isinstance(error, CorruptDatabase):
                self.store.protect(str(error))
            add('database', 'データベース', 'error', str(error))
        try:
            usage = shutil.disk_usage(self.root)
            warning = usage.free < max(256 * 1024 ** 2, usage.total * .05)
            add('disk', '空き容量', 'warning' if warning else 'ok',
                f'空き {usage.free / 1024**3:.2f} GB / 全体 {usage.total / 1024**3:.2f} GB' +
                ('。不要ファイルの整理または保存先の拡張が必要です' if warning else ''))
            for directory in (self.store.directory, self.directory):
                with tempfile.TemporaryFile(dir=directory) as stream:
                    stream.write(b'selfcare-write-check')
                    stream.flush()
                    os.fsync(stream.fileno())
            add('write', '保存先の書き込み', 'ok', 'データ・バックアップ保存先の書き込みと同期を確認しました')
        except OSError as error:
            add('write', '保存先', 'error', '容量・共有フォルダの権限を確認してください: ' + str(error))
        status = self.status()
        if status['config_error'] or status['error']:
            add('backups', '定期バックアップ', 'error', status['config_error'] or status['error'])
        elif not status['settings']['enabled']:
            add('backups', '定期バックアップ', 'warning', '無効です。設定から有効にしてください')
        elif not status['last_success']:
            add('backups', '定期バックアップ', 'warning', 'まだ完了していません。「今すぐバックアップ」から作成できます')
        elif time.time() - status['last_success'] > self.config['interval_hours'] * 3600 + 3600:
            add('backups', '定期バックアップ', 'warning', '予定より1時間以上遅れています。保存先とサービス稼働を確認してください')
        else:
            add('backups', '定期バックアップ', 'ok', f"{len(status['backups'])}世代を保存。作成・取得・復元時に内容を検証します")
        if any(not b['readable'] for b in status['backups']):
            add('archive', 'バックアップファイル', 'error', '読めない保存世代があります。自動削除せず保持しています')
        if status['warning']:
            add('retention', '世代整理', 'warning', status['warning'])
        verification = status['verification']
        if verification:
            add('verification', '保存世代の再検査', 'ok' if verification['ok'] else 'error',
                verification['name'] + (' の整合性・SHA-256は正常です' if verification['ok'] else ': ' + verification.get('error', '検査失敗')))
        return {'checked_at': time.time(), 'checks': checks, 'recovery_required': bool(self.store.error)}


def restore(root, archive):
    """Offline only. Preserve original directories; interrupted restores stay gated."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(root / 'service.lock'), file_lock(root / 'updates/update.lock'):
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=root) as work:
            work = Path(work)
            unpack_verified(archive, work)
            (work / 'data').mkdir(mode=0o700)
            os.replace(work / 'measurements.sqlite3', work / 'data/measurements.sqlite3')
            (work / 'config').mkdir(exist_ok=True, mode=0o700)
            # Resume by invoking restore again after an interruption; earlier originals stay in recovery/.
            recovery = root / 'recovery' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
            recovery.mkdir(parents=True, mode=0o700)
            sync_dir(recovery.parent)
            sync_dir(root)
            write_json(root / 'restore.pending.json', {'archive': str(archive), 'originals': str(recovery)})
            for name in ('data', 'config'):
                if (root / name).exists():
                    os.replace(root / name, recovery / name)
                    sync_dir(recovery)
                    sync_dir(root)
                os.replace(work / name, root / name)
                sync_dir(root / name)
                sync_dir(root)
            # Restored automatic collection is disabled until the user checks the devices.
            with sqlite3.connect(root / 'data/measurements.sqlite3') as db:
                for identifier, raw in db.execute('SELECT id,config FROM devices').fetchall():
                    config = json.loads(raw)
                    config['auto_sync'] = False
                    db.execute('UPDATE devices SET config=? WHERE id=?', (json.dumps(config), identifier))
            check_database(root / 'data/measurements.sqlite3', full=True, timeout=30)
            write_json(root / 'database.initialized.json', {'schema': 1})
            (root / 'database.guard.json').unlink(missing_ok=True)
            (root / 'restore.pending.json').unlink()
            sync_dir(root)
            return str(recovery)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=os.environ.get('SELFCARE_DATA_DIR', '/share/Container/QnapSelfCare'))
    sub = parser.add_subparsers(dest='action', required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('archive')
    recover = sub.add_parser('restore')
    recover.add_argument('archive')
    recover.add_argument('--confirm-replace', action='store_true', required=True,
                         help='replace current DB/config while preserving originals in recovery/')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == 'verify':
            Path(args.data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix='.verify-', dir=args.data_dir) as work:
                result = unpack_verified(args.archive, work)
                print(json.dumps({'verified': True, 'created_at': result['created_at'], 'counts': result['counts']}))
        else:
            print('復元が完了しました。元データの退避先: ' + restore(args.data_dir, args.archive))
        return 0
    except Exception as error:
        print('処理を中止しました: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
