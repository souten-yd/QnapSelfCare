"""Explicit migration of the standard QnapHomeHub Compose deployment."""
import json
import copy
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid

from collectors import bridge_request
from durability import DataUnavailable, file_lock, sync_dir, write_json

ROOT = Path(__file__).resolve().parent
HOMEHUB = Path('/share/Container/QnapHomeHub')
IMAGE = 'ghcr.io/souten-yd/qnaphomehub'
REVISION = 'aed424cf572295ac2636d1cebae6b69c98606498'
ACTIVE = {'checking', 'pulling', 'backup', 'applying', 'verifying', 'rollback'}


def read_json(path):
    return json.loads(Path(path).read_text())


def canonical(content):
    return content.replace('\r\n', '\n').strip()


def atomic_copy(source, target):
    target = Path(target)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(Path(source).read_bytes())
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, target)
            sync_dir(target.parent)
        finally:
            temporary.unlink(missing_ok=True)


def docker_binary():
    found = shutil.which('docker')
    if found:
        return found
    try:
        path = subprocess.check_output(['/sbin/getcfg', 'container-station', 'Install_Path',
            '-f', '/etc/config/qpkg.conf'], text=True, timeout=3).strip()
        binary = Path(path) / 'bin/docker'
        if path and binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
    except (OSError, subprocess.SubprocessError):
        pass
    raise ValueError('Container StationのDockerが見つかりません。Container Stationを起動してください')


class Migration:
    def __init__(self, directory=HOMEHUB):
        self.root = Path(directory).resolve()
        self.compose_file = self.root / 'compose.yaml'
        self.docker = docker_binary()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('DOCKER_', 'COMPOSE_'))}
        self.env['QNAP_HOMEHUB_PROJECT_DIR'] = str(self.root)

    def command(self, args, timeout=60):
        result = subprocess.run([self.docker, '--host', 'unix:///var/run/docker.sock', *args],
            env=self.env, cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=timeout, close_fds=True)
        if result.returncode:
            # Do not surface compose config/secrets or arbitrary container logs in the Web API.
            raise RuntimeError('Docker処理に失敗しました: ' + ' '.join(args[:2]) +
                               f'（終了コード {result.returncode}）')
        return result.stdout

    def compose(self, args, filename=None, timeout=60):
        return self.command(['compose', '-p', 'qnaphomehub', '--project-directory', str(self.root),
            '-f', str(filename or self.compose_file), *args], timeout)

    def inspect(self, service):
        name = 'qnaphomehub' if service == 'homehub' else 'qnaphomehub-' + service
        return json.loads(self.command(['inspect', name]))[0]

    def updater_idle(self):
        token = (self.root / 'secrets/homehub_internal_token.txt').read_text().strip()
        request = urllib.request.Request('http://127.0.0.1:8788/status',
            headers={'x-homehub-internal-token': token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            status = json.load(response)
        if status.get('busy') is not False or status.get('phase') in ('applying', 'rollback'):
            raise DataUnavailable('HomeHubの更新処理中です。HomeHubの更新完了後に実行してください')

    def effective(self, filename):
        return json.loads(self.compose(['config', '--format', 'json'], filename))

    def target_compose(self, legacy, shared):
        """Keep site-specific Compose settings while moving Bluetooth ownership to radio."""
        if legacy or shared:
            return (ROOT / 'homehub/compose-shared.yaml').read_bytes()
        current = self.effective(self.compose_file)
        reference = self.effective(ROOT / 'homehub/compose-shared.yaml')
        services = current.get('services', {})
        homehub = services.get('homehub', {})
        updater = services.get('updater', {})
        expected = reference['services']
        environment = homehub.get('environment', {})
        volumes = homehub.get('volumes', [])
        if (not isinstance(environment, dict) or not isinstance(volumes, list)
                or homehub.get('image') != IMAGE + ':server'
                or updater.get('image') != IMAGE + ':updater'
                or homehub.get('network_mode') != 'host'
                or homehub.get('container_name') != 'qnaphomehub'
                or updater.get('container_name') != 'qnaphomehub-updater'
                or environment.get('PORT') != '8787'
                or environment.get('DATA_DIR') != '/data'):
            raise ValueError('HomeHubの接続・保存設定が移行対象と異なります。自動変更を中止しました')
        radio_volume = next(volume for volume in expected['homehub']['volumes'] if volume['target'] == '/radio')
        if any(volume.get('target') == '/radio' and volume != radio_volume
               for volume in volumes):
            raise ValueError('HomeHubのradio保存先が標準と異なります')
        if homehub.get('devices') or homehub.get('cap_add') or any(
                volume.get('target', '').startswith(('/dev', '/var/run/dbus', '/var/lib/bluetooth'))
                for volume in volumes):
            raise ValueError('HomeHubに独自のBluetooth機器設定があります。自動変更を中止しました')
        existing_radio = services.get('radio')
        if existing_radio and existing_radio != expected['radio']:
            raise ValueError('既存radioの設定が標準と異なります。自動変更を中止しました')
        for name in ('switchbot_token', 'switchbot_secret'):
            config = reference['secrets'][name]
            if name in current.get('secrets', {}) and current['secrets'][name] != config:
                raise ValueError('radioが使うsecretsの保存先が標準と異なります')
        for name, service in services.items():
            if name not in ('homehub', 'radio') and (service.get('privileged') or
                    any('/dev/bus/usb' in str(value) for value in service.get('devices', []))):
                raise ValueError('別サービスがBluetooth機器を使用する可能性があります: ' + name)
        already_shared = environment.get('HOMEHUB_SHARED_RADIO') == '1'
        if already_shared and homehub.get('privileged'):
            raise ValueError('HomeHubが共通radioと特権モードを同時使用しています')
        if not already_shared and homehub.get('privileged') is not True:
            raise ValueError('旧HomeHubのBluetooth実行方式が標準と異なります')
        updated = copy.deepcopy(current)
        target = updated['services']['homehub']
        target['privileged'] = False
        target['environment']['HOMEHUB_SHARED_RADIO'] = '1'
        if not any(volume.get('target') == '/radio' for volume in target['volumes']):
            target['volumes'].append(copy.deepcopy(radio_volume))
        updated['services']['radio'] = copy.deepcopy(expected['radio'])
        updated.setdefault('secrets', {}).update({name: copy.deepcopy(reference['secrets'][name])
            for name in ('switchbot_token', 'switchbot_secret')})
        return (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode()

    def preflight(self):
        if os.geteuid() != 0 or platform.machine() not in ('x86_64', 'AMD64'):
            raise ValueError('この移行は管理者権限で動くx86_64 NAS用です')
        if self.compose_file.is_symlink():
            raise ValueError('シンボリックリンクのComposeは自動変更できません')
        content = canonical(self.compose_file.read_text())
        legacy = content == canonical((ROOT / 'homehub/compose-legacy.yaml').read_text())
        shared = content == canonical((ROOT / 'homehub/compose-shared.yaml').read_text())
        for name in ('compose.override.yaml', 'compose.override.yml', 'docker-compose.override.yml', 'docker-compose.override.yaml'):
            if (self.root / name).exists():
                raise ValueError('Composeの追加設定があります。個別確認が必要です')
        self.compose(['config', '--quiet'])
        names = self.command(['ps', '-a', '--format', '{{.Names}}']).splitlines()
        if 'qnapselfcare-bluetooth' in names:
            dedicated = json.loads(self.command(['inspect', 'qnapselfcare-bluetooth']))[0]
            if dedicated['State']['Running']:
                raise ValueError('専用Bluetoothコンテナが稼働しています。qnapselfcare-bluetoothを停止してから移行してください')
        radio_exists = 'qnaphomehub-radio' in names
        effective_shared = shared
        if not (legacy or shared):
            effective_shared = self.effective(self.compose_file)['services']['homehub'].get('environment', {}).get('HOMEHUB_SHARED_RADIO') == '1'
        if not effective_shared and radio_exists:
            raise ValueError('旧構成と既存radioが混在しています。個別確認が必要です')
        services = ['homehub', 'updater'] + (['radio'] if effective_shared and radio_exists else [])
        radio_running = False
        images = {}
        for service in services:
            item = self.inspect(service)
            labels = item['Config'].get('Labels') or {}
            if (labels.get('com.docker.compose.project') != 'qnaphomehub'
                    or labels.get('com.docker.compose.service') != service
                    or Path(labels.get('com.docker.compose.project.working_dir', '')).resolve() != self.root):
                raise ValueError('稼働コンテナとHomeHubの保存先が一致しません')
            configs = labels.get('com.docker.compose.project.config_files', '').split(',')
            if len(configs) != 1 or Path(configs[0]).resolve() != self.compose_file:
                raise ValueError('稼働コンテナが別のComposeを使用しています')
            if not item['State']['Running'] and service != 'radio':
                raise ValueError('HomeHubとupdaterを起動してから移行してください')
            ref = IMAGE + (':updater' if service == 'updater' else ':server')
            if item['Config']['Image'] != ref:
                raise ValueError('標準と異なるイメージ指定です。自動変更を中止しました')
            images[service] = {'id': item['Image'], 'ref': ref}
            if service == 'radio':
                radio_running = item['State']['Running']
            version = item['Config'].get('Labels', {}).get('org.opencontainers.image.version', '')
            match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', version)
            if service == 'homehub' and match and tuple(map(int, match.groups())) > (0, 3, 0):
                raise ValueError('HomeHubが移行対象より新しい版です。ダウングレードを中止しました')
        if 'radio' in images and images['homehub']['id'] != images['radio']['id']:
            raise ValueError('HomeHubとradioのイメージが異なります。個別確認が必要です')
        data_mounts = {m['Destination']: Path(m['Source']).resolve() for m in self.inspect('homehub')['Mounts'] if m['Type'] == 'bind'}
        if data_mounts.get('/data') != self.root / 'data/homehub':
            raise ValueError('HomeHubのデータ保存先が標準と異なります')
        self.updater_idle()
        if (self.root / '.env').is_symlink():
            raise ValueError('環境設定がシンボリックリンクです。個別確認が必要です')
        return {'legacy': not effective_shared, 'images': images, 'radio_exists': radio_exists,
                'radio_running': radio_running,
                'compose_digest': hashlib.sha256(self.compose_file.read_bytes()).hexdigest()}

    def wait_healthy(self, shared, timeout=90):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        last_error = '応答待ち'
        while time.monotonic() < deadline:
            try:
                with opener.open('http://127.0.0.1:8787/api/health', timeout=3) as response:
                    value = json.load(response)
                if shared:
                    radio = bridge_request(self.root / 'data/radio/ble.sock')
                    if not value.get('radio', {}).get('shared') or not value['radio'].get('available'):
                        raise ValueError('HomeHubと共通radioが未接続です')
                    if radio.get('bluezError'):
                        raise ValueError('共通radioのBlueZ起動エラー: ' + str(radio['bluezError'])[:300])
                return
            except (OSError, ValueError, KeyError) as error:
                last_error = str(error)[:400]
                time.sleep(2)
        raise RuntimeError('HomeHub・共通Bluetoothサービスの起動確認に失敗しました: ' + last_error)

    def rollback(self, job, metadata):
        shared_file = job / 'compose-shared.yaml'
        if not {'homehub', 'updater'} <= set(metadata['images']) or not set(metadata['images']) <= {'homehub', 'updater', 'radio'}:
            raise ValueError('復旧対象のイメージ情報が不正です')
        for service, entry in metadata['images'].items():
            expected = IMAGE + (':updater' if service == 'updater' else ':server')
            if entry['ref'] != expected or not entry['id'].startswith('sha256:'):
                raise ValueError('復旧イメージの識別情報が不正です')
        for name, expected in metadata['checksums'].items():
            if name not in ('compose-before.yaml', 'compose-shared.yaml') or hashlib.sha256((job / name).read_bytes()).hexdigest() != expected:
                raise ValueError('退避したComposeの検証に失敗しました。自動復旧を中止しました')
        if set(metadata['checksums']) != {'compose-before.yaml', 'compose-shared.yaml'}:
            raise ValueError('退避したComposeの検証情報が不足しています')
        # Never restart an old Noble owner until the new radio has actually stopped.
        self.compose(['stop', 'radio', 'homehub', 'updater'], shared_file, timeout=60)
        if not metadata['radio_exists']:
            self.compose(['rm', '-f', 'radio'], shared_file)
        for entry in metadata['images'].values():
            self.command(['image', 'tag', entry['id'], entry['ref']])
        atomic_copy(job / 'compose-before.yaml', self.compose_file)
        self.compose(['up', '-d', '--no-build', '--no-deps',
                      *(['radio'] if metadata['radio_running'] else []), 'homehub', 'updater'], timeout=120)
        self.wait_healthy(metadata['radio_running'])

    def run(self, job, phase):
        phase('checking', 'HomeHubの構成・保存先・更新状態を確認しています')
        metadata = self.preflight()
        shutil.copyfile(self.compose_file, job / 'compose-before.yaml')
        content = canonical(self.compose_file.read_text())
        legacy = content == canonical((ROOT / 'homehub/compose-legacy.yaml').read_text())
        shared = content == canonical((ROOT / 'homehub/compose-shared.yaml').read_text())
        (job / 'compose-shared.yaml').write_bytes(self.target_compose(legacy, shared))
        self.compose(['config', '--quiet'], job / 'compose-shared.yaml')
        if shutil.disk_usage(self.root).free < 512 * 1024 ** 2:
            raise ValueError('移行用の空き容量が不足しています（512MiB以上必要）')
        phase('pulling', '共通Bluetooth対応のHomeHubイメージを取得しています')
        for component in ('server', 'updater'):
            self.command(['pull', IMAGE + ':' + component + '-' + REVISION], timeout=900)
        # Pulling must not change the running service aliases before the rollback snapshot.
        current = self.preflight()
        if current != metadata:
            raise ValueError('準備中にHomeHubの状態が変わりました。再実行してください')
        metadata['checksums'] = {name: hashlib.sha256((job / name).read_bytes()).hexdigest()
                                 for name in ('compose-before.yaml', 'compose-shared.yaml')}
        write_json(job / 'original.json', metadata)
        for directory in ('data/homehub', 'data/updater', 'secrets'):
            source = self.root / directory
            if source.is_symlink():
                raise ValueError('設定のシンボリックリンクは自動移行できません')
            if source.exists():
                files = list(source.rglob('*'))
                if any(p.is_symlink() for p in files) or sum(p.stat().st_size for p in files if p.is_file()) > 32 * 1024 ** 2:
                    raise ValueError('設定のバックアップ対象が標準範囲を超えています')
        pending = job.parent / 'pending.json'
        for path in job.iterdir():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
        sync_dir(job)
        write_json(pending, {'job': job.name})
        try:
            phase('backup', 'HomeHubを一時停止し、現在の設定を退避しています')
            self.compose(['stop', 'updater'], timeout=45)
            if self.inspect('homehub')['Image'] != metadata['images']['homehub']['id']:
                raise ValueError('HomeHubの更新が競合しました')
            self.compose(['stop', 'homehub', *(['radio'] if not metadata['legacy'] else [])], timeout=60)
            for directory in ('data/homehub', 'data/updater', 'secrets'):
                if (self.root / directory).exists():
                    shutil.copytree(self.root / directory, job / directory)
            if (self.root / '.env').exists():
                shutil.copyfile(self.root / '.env', job / 'environment-before')
            for path in job.rglob('*'):
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
                if path.is_file():
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
            sync_dir(job)
            phase('applying', '共通Bluetooth構成を適用して起動しています')
            for component in ('server', 'updater'):
                self.command(['image', 'tag', IMAGE + ':' + component + '-' + REVISION, IMAGE + ':' + component])
            atomic_copy(job / 'compose-shared.yaml', self.compose_file)
            self.compose(['up', '-d', '--no-build', '--no-deps', 'radio', 'homehub'], timeout=120)
            phase('verifying', 'HomeHubと共通Bluetoothへの接続を確認しています')
            self.wait_healthy(True)
            self.compose(['up', '-d', '--no-build', '--no-deps', 'updater'], timeout=120)
            pending.unlink()
            sync_dir(job.parent)
        except Exception as error:
            phase('rollback', '移行に失敗したため、元の構成とイメージへ戻しています')
            try:
                self.rollback(job, metadata)
            except Exception as recovery:
                raise RuntimeError(str(error) + '。元の構成への復旧も失敗しました: ' + str(recovery)) from recovery
            pending.unlink()
            sync_dir(job.parent)
            raise RuntimeError(str(error) + '。元の構成への復旧は完了しました') from error


class MigrationManager:
    def __init__(self, data_root, homehub=HOMEHUB):
        self.data_root = Path(data_root)
        self.homehub = Path(homehub)
        self.directory = self.data_root / 'homehub-migration'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_file = self.directory / 'status.json'
        self.pending_file = self.directory / 'pending.json'
        self.lock = threading.Lock()
        self.busy = False
        try:
            self.state = read_json(self.state_file)
        except (OSError, ValueError):
            self.state = {}
        if self.state.get('phase') in ACTIVE:
            self.state.update(phase='failed', message='前回の移行が中断しました。退避先を保持しています')

    def status(self):
        with self.lock:
            return dict(self.state, busy=self.busy, can_restore=self.pending_file.exists(),
                        homehub_path=str(self.homehub))

    def phase(self, phase, message, **extra):
        with self.lock:
            self.state.update(phase=phase, message=message, **extra)
            write_json(self.state_file, self.state)

    def apply(self, action='migrate'):
        if action not in ('migrate', 'restore'):
            raise ValueError('移行または元の構成への復旧を指定してください')
        with self.lock:
            if self.busy:
                raise DataUnavailable('HomeHubの移行処理が実行中です')
            if action == 'migrate' and self.pending_file.exists():
                raise DataUnavailable('前回の移行が中断しています。先に元の構成へ戻してください')
            if action == 'restore' and not self.pending_file.exists():
                raise ValueError('復旧待ちの移行はありません')
            self.busy = True
        # Non-daemon: an ordinary Web shutdown cannot kill a migration mid-transaction.
        threading.Thread(target=self.worker, args=(action,), daemon=False, name='homehub-migration').start()
        return self.status()

    def worker(self, action):
        try:
            with file_lock(self.data_root / 'updates/update.lock'), file_lock(self.directory / 'migration.lock'):
                self.phase('checking', '移行の準備をしています')
                migration = Migration(self.homehub)
                if action == 'restore':
                    identifier = read_json(self.pending_file)['job']
                    if not re.fullmatch('[a-f0-9]{32}', identifier):
                        raise ValueError('退避先の識別子が不正です')
                    job = self.directory / identifier
                    self.phase('rollback', '退避した構成とイメージに戻しています')
                    migration.rollback(job, read_json(job / 'original.json'))
                    self.pending_file.unlink()
                    sync_dir(self.directory)
                    self.phase('restored', '元の構成に戻しました。診断結果を確認してください')
                else:
                    job = self.directory / uuid.uuid4().hex
                    job.mkdir(mode=0o700)
                    self.phase('checking', 'HomeHubの構成を確認しています', backup=str(job))
                    migration.run(job, self.phase)
                    self.phase('success', '共通Bluetooth構成への移行が完了しました。「診断を更新」で接続を確認してください')
        except Exception as error:
            self.phase('failed', str(error) or type(error).__name__)
        finally:
            with self.lock:
                self.busy = False
