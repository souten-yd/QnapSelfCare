import asyncio
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from unittest.mock import AsyncMock

import ble_protocol as p
from ble_worker import Session
from collectors import BluetoothFailure, CollectorManager, bridge_request
from storage import Store


class ProtocolTests(unittest.TestCase):
    def test_hem_fixture(self):
        raw = bytes.fromhex('50691a482727078c000000000000')
        record = p.decode('HEM-6232T', raw)
        self.assertEqual(record['values'], {'systolic': 130, 'diastolic': 80, 'pulse': 72})
        self.assertEqual(record['measured_at'], '2026-09-25T07:30:12+09:00')

    def test_hbf_fixture(self):
        raw = bytearray(32)
        raw[2:14] = bytes.fromhex('3e8a64004b1a399e2809c9cc')
        raw[26:28] = bytes.fromhex('5780')
        record = p.decode('HBF-228T', raw)
        self.assertEqual(record['measured_at'], '2026-09-25T07:30:12+09:00')
        self.assertEqual(record['values'], {'weight': 70, 'body_fat': 25, 'visceral_fat': 10, 'bmr': 1600, 'muscle': 30, 'bmi': 23, 'body_age': 40})
        self.assertIsNone(p.decode('HBF-228T', bytes(32)))

    def test_corrupt_or_wrong_address_response_rejected(self):
        frame = bytes([10, 129, 0, 2, 192, 2, 10, 11, 0])
        frame += bytes([p.checksum(frame)])
        self.assertEqual(p.response(frame, 1, 0x2c0, 2), bytes([10, 11]))
        with self.assertRaises(ValueError):
            p.response(frame, 1, 0x2e8, 2)
        with self.assertRaises(ValueError):
            p.response(frame[:-1] + bytes([0]), 1, 0x2c0, 2)
        with self.assertRaises(ValueError):
            p.command(0xc0, 0, 16)

    def test_session_ack_is_control_frame_not_sixteen_bytes_of_measurements(self):
        self.assertEqual(p.command(0, length=16).hex(), '0800000000100018')
        self.assertEqual(p.response(bytes.fromhex('0880000000100098'), 0, 0, 16), b'')
        self.assertEqual(p.response(bytes.fromhex('088f000000000087'), 15, 0, 0), b'')
        with self.assertRaisesRegex(ValueError, 'コマンドを拒否'):
            p.response(bytes.fromhex('088f000000000186'), 0, 0, 16)
        with self.assertRaises(ValueError):
            p.response(bytes.fromhex('08810002c020006b'), 1, 0x2c0, 32)

    def test_session_ack_accepts_device_info_without_accepting_truncation(self):
        # User capture begins 18 80 00 00 00 10; payload here is synthetic.
        info = bytes(range(16))
        frame = bytes.fromhex('188000000010') + info + b'\x00'
        frame += bytes([p.checksum(frame)])
        self.assertEqual(p.response(frame, 0, 0, 16), info)
        for payload in (info[:-1], info + b'\x00'):
            bad = bytes([len(payload) + 8, 0x80, 0, 0, 0, 16]) + payload + b'\x00'
            bad += bytes([p.checksum(bad)])
            with self.assertRaises(ValueError):
                p.response(bad, 0, 0, 16)
        bad = frame[:5] + b'\x0f' + frame[6:-1]
        bad += bytes([p.checksum(bad)])
        with self.assertRaises(ValueError):
            p.response(bad, 0, 0, 16)

    def test_cuff_pairing_authenticates_programmed_key_before_session_open(self):
        async def exercise():
            characteristic = Mock(properties=['write-without-response'])
            client = Mock(is_connected=True, services=Mock())
            client.services.get_characteristic.return_value = characteristic
            client.start_notify = AsyncMock()
            trace = {'stage': 'starting', 'slots': []}
            session = Session(client, trace)
            authenticated = False
            operations = []
            async def write(char, packet, response):
                nonlocal authenticated
                if char == p.UNLOCK:
                    operations.append(('unlock', packet[0]))
                    self.assertTrue(response)
                    if packet[0] == 1:
                        self.assertEqual(packet[1:], b'\xab' * 16)
                        authenticated = True
                    session.unlocks.put_nowait(bytes([packet[0] | 0x80, 0]))
                else:
                    self.assertFalse(response)
                    operations.append(('command', packet[1]))
                    if packet[1] == 0:
                        self.assertTrue(authenticated, 'cuff requires auth after key programming')
                        start = bytes.fromhex('188000000010') + bytes(range(16)) + b'\x00'
                        start += bytes([p.checksum(start)])
                        session.notify(0, start[:16])
                        session.notify(1, start[16:])
                    else:
                        session.notify(0, bytes.fromhex('088f000000000087'))
            client.write_gatt_char = AsyncMock(side_effect=write)
            with patch('ble_worker.asyncio.sleep', new=AsyncMock()):
                await session.subscribe()
                await session.pair(b'\xab' * 16, 'HEM-6232T')
            self.assertEqual([c.args[0] for c in client.start_notify.await_args_list], p.RX + [p.UNLOCK])
            self.assertEqual(operations, [('unlock', 2), ('unlock', 0), ('unlock', 1), ('command', 0), ('command', 15)])
            self.assertNotIn('ab' * 16, str(trace))
            self.assertEqual(trace['responses'][-2]['status'], 'valid')
        asyncio.run(exercise())

    def test_session_open_retries_once_without_reprogramming_key(self):
        async def exercise():
            char = Mock(properties=['write'])
            client = Mock(is_connected=True, services=Mock())
            client.services.get_characteristic.return_value = char
            trace = {'stage': 'starting', 'slots': []}
            session = Session(client, trace)
            async def write(*args, **kwargs):
                if client.write_gatt_char.await_count == 2:
                    session.notify(0, bytes.fromhex('0880000000100098'))
            client.write_gatt_char = AsyncMock(side_effect=write)
            calls = 0
            async def wait(awaitable, seconds):
                nonlocal calls
                calls += 1
                if calls == 1:
                    awaitable.close()
                    raise asyncio.TimeoutError
                return await awaitable
            with patch('ble_worker.asyncio.wait_for', side_effect=wait), patch('ble_worker.asyncio.sleep', new=AsyncMock()):
                self.assertEqual(await session.request(0, length=16), b'')
            self.assertEqual(client.write_gatt_char.await_count, 2)
            self.assertEqual(trace['responses'][0]['error'], 'timeout')
            self.assertEqual(trace['responses'][1]['status'], 'valid')
            self.assertEqual(trace['responses'][1]['attempt'], 2)
        asyncio.run(exercise())

    def test_session_open_stops_on_disconnect_or_after_two_timeouts(self):
        async def exercise(connected, expected_attempts):
            client = Mock(is_connected=connected, services=Mock())
            client.services.get_characteristic.return_value = Mock(properties=['write'])
            client.write_gatt_char = AsyncMock()
            session = Session(client, {'slots': []})
            async def timeout(awaitable, seconds):
                awaitable.close()
                raise asyncio.TimeoutError
            with patch('ble_worker.asyncio.wait_for', side_effect=timeout), patch('ble_worker.asyncio.sleep', new=AsyncMock()):
                with self.assertRaisesRegex(TimeoutError, f'{expected_attempts}回試行'):
                    await session.request(0, length=16)
            self.assertEqual(client.write_gatt_char.await_count, expected_attempts)
        asyncio.run(exercise(False, 1))
        asyncio.run(exercise(True, 2))

    def test_fragment_reassembly_out_of_order(self):
        async def exercise():
            session = Session(AsyncMock())
            frame = bytes([40]) + bytes(39)
            session.notify(2, frame[32:]); session.notify(0, frame[:16]); session.notify(1, frame[16:32])
            self.assertEqual(await session.frames.get(), frame)
        asyncio.run(exercise())

    def test_collector_persists_sync_and_rejects_concurrent_job(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            user = store.save_user({'name': 'test'})
            device = store.save_device({'model': 'HEM-6232T', 'address': 'AA:BB:CC:DD:EE:FF', 'bindings': {'1': user['id']}})
            store.pairing_key(device['address'], '11' * 16)
            rec = dict(p.decode('HEM-6232T', bytes.fromhex('50691a482727078c000000000000')),
                       user_id=user['id'], device_id=device['id'], slot=1)
            manager = CollectorManager(store, runner=lambda request, directory: {'records': [rec]})
            job = manager.submit('sync', device['id'])
            with self.assertRaises(ValueError):
                manager.submit('sync', device['id'])
            manager.process(manager.queue.get_nowait())
            self.assertEqual(store.records()['total'], 1)
            self.assertEqual(store.jobs()[0]['state'], 'done')

    def test_listener_delay_retry_and_cooldown_without_periodic_scan(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            user = store.save_user({'name': 'test'})
            device = store.save_device({'model': 'HEM-6232T', 'address': 'AA:BB:CC:DD:EE:FF',
                'bindings': {'1': user['id']}, 'auto_sync': True, 'interval': 300})
            self.assertEqual(device['sync_mode'], 'listen')
            store.pairing_key(device['address'], '11' * 16)
            runner = Mock(side_effect=BluetoothFailure('read timeout'))
            manager = CollectorManager(store, runner=runner)
            response = {'supported': True, 'ready': True, 'events': [{'address': device['address'], 'at': 1234}]}
            with patch('collectors.bridge_request', return_value=response) as bridge, patch('collectors.time.monotonic', return_value=100) as clock:
                manager.schedule()
                runner.assert_not_called()
                self.assertEqual(bridge.call_args.kwargs['endpoint'], '/watch')
                self.assertTrue(manager.queue.empty())
                self.assertEqual(manager.listener_status()['waiting'][0]['seconds'], 60)
                clock.return_value = 159
                response['events'][0]['at'] = 1235
                manager.schedule()
                self.assertTrue(manager.queue.empty())
                self.assertEqual(manager.listener_status()['waiting'][0]['seconds'], 1)
                clock.return_value = 160
                manager.schedule()
                self.assertEqual(manager.queue.qsize(), 1)
                manager.process(manager.queue.get_nowait())
                self.assertEqual(runner.call_count, 1)
                self.assertEqual(store.jobs()[0]['state'], 'failed')
                self.assertIn('60秒後', store.jobs()[0]['message'])
                # Retry needs no new advertisement and bypasses the 300-second normal cooldown.
                response['events'] = []
                clock.return_value = 219
                manager.schedule()
                self.assertTrue(manager.queue.empty())
                self.assertEqual(manager.listener_status()['waiting'][0]['attempt'], 2)
                clock.return_value = 220
                manager.schedule()
                manager.process(manager.queue.get_nowait())
                self.assertEqual(runner.call_count, 2)
                self.assertFalse(manager.listen_delayed)
                clock.return_value = 280
                manager.schedule()
                self.assertTrue(manager.queue.empty())
                self.assertEqual(runner.call_count, 2)  # Never a third blind retry.
                # Same-burst advertisements are suppressed after the bounded
                # retry. A 90-second quiet gap opens a new HEM delayed-sync cycle.
                response['events'] = [{'address': device['address'], 'at': 91235}]
                clock.return_value = 282
                manager.schedule()
                self.assertTrue(manager.listen_delayed)
                self.assertEqual(manager.listener_status()['waiting'][0]['attempt'], 1)
                clock.return_value = 342
                runner.side_effect = None
                runner.return_value = {'records': []}
                manager.schedule()
                manager.process(manager.queue.get_nowait())
                self.assertFalse(manager.listen_delayed)  # Success, including 0 records, ends cycle.
                self.assertEqual(runner.call_count, 3)
                store.save_device(dict(device, auto_sync=False))
                clock.return_value = 590
                manager.schedule()
                self.assertEqual(bridge.call_args.args[1]['addresses'], [])
                self.assertFalse(manager.watch_configured)
            with self.assertRaises(ValueError):
                store.save_device(dict(device, transport='direct', sync_mode='listen'))

    def test_listener_reservations_cancel_and_manual_sync_is_immediate(self):
        for cancel in ('off', 'delete', 'mode', 'binding', 'manual', 'queued_off'):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as root:
                store = Store(root)
                user = store.save_user({'name': 'test'})
                device = store.save_device({'model': 'HEM-6232T', 'address': 'AA:BB:CC:DD:EE:FF',
                    'bindings': {'1': user['id']}, 'auto_sync': True})
                store.pairing_key(device['address'], '11' * 16)
                runner = Mock(return_value={'records': []})
                manager = CollectorManager(store, runner=runner)
                response = {'supported': True, 'ready': True, 'events': [{'address': device['address'], 'at': 1234}]}
                with patch('collectors.bridge_request', return_value=response), patch('collectors.time.monotonic', return_value=100) as clock:
                    manager.schedule()
                    if cancel == 'queued_off':
                        clock.return_value = 160
                        manager.schedule()
                    if cancel in ('off', 'queued_off'):
                        store.save_device(dict(device, auto_sync=False))
                    elif cancel == 'delete':
                        store.delete_device(device['id'])
                    elif cancel == 'mode':
                        store.save_device(dict(device, sync_mode='interval'))
                    elif cancel == 'binding':
                        store.save_device(dict(device, bindings={}))
                    else:
                        manager.submit('sync', device['id'])
                        self.assertFalse(manager.listen_delayed)
                        manager.process(manager.queue.get_nowait())
                        self.assertEqual(runner.call_count, 1)
                    if cancel == 'queued_off':
                        manager.process(manager.queue.get_nowait())
                        self.assertEqual(store.jobs()[0]['state'], 'skipped')
                    clock.return_value = 200
                    manager.dispatch_listen()
                    self.assertFalse(manager.listen_delayed)
                    if cancel != 'manual':
                        runner.assert_not_called()

    def test_listener_missing_device_retry_can_succeed(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            user = store.save_user({'name': 'test'})
            device = store.save_device({'model': 'HEM-6232T', 'address': 'AA:BB:CC:DD:EE:FF',
                'bindings': {'1': user['id']}, 'auto_sync': True})
            store.pairing_key(device['address'], '11' * 16)
            runner = Mock(side_effect=[BluetoothFailure('機器が見つかりません。機器を通信可能な状態にし、NASへ近づけてください',
                {'stage': 'discovery'}), {'records': [], 'diagnostic': {'stage': 'read'}}])
            manager = CollectorManager(store, runner=runner)
            response = {'supported': True, 'ready': True, 'events': [{'address': device['address'], 'at': 1234}]}
            with patch('collectors.bridge_request', return_value=response), patch('collectors.time.monotonic', return_value=100) as clock:
                manager.schedule()
                self.assertEqual(manager.listener_status()['waiting'][0]['seconds'], 60)
                clock.return_value = 160
                manager.schedule()
                manager.process(manager.queue.get_nowait())
                self.assertEqual(store.jobs()[0]['state'], 'skipped')
                self.assertTrue(runner.call_args.args[0]['diagnostic'])
                self.assertEqual(json.loads(store.jobs()[0]['result'])['diagnostic']['stage'], 'discovery')
                self.assertTrue(manager.listen_delayed)
                clock.return_value = 220
                manager.schedule()
                manager.process(manager.queue.get_nowait())
                self.assertTrue(any(j['state'] == 'done' and '再試行' in j['message'] for j in store.jobs()))
                self.assertEqual(json.loads(store.jobs()[0]['result'])['diagnostic']['stage'], 'read')
                self.assertFalse(manager.listen_delayed)

    def test_hbf_listener_starts_immediately_without_timed_retry(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            user = store.save_user({'name': 'test'})
            device = store.save_device({'model': 'HBF-228T', 'address': 'AA:BB:CC:DD:EE:FF',
                'bindings': {'1': user['id']}, 'auto_sync': True, 'interval': 300})
            store.pairing_key(device['address'], '11' * 16)
            missing = '機器が見つかりません。機器を通信可能な状態にし、NASへ近づけてください'
            runner = Mock(side_effect=[BluetoothFailure(missing, {'stage': 'discovery', 'elapsed_since_advert_ms': 900}),
                                       BluetoothFailure(missing, {'stage': 'connection', 'connect_ms': 8001}),
                                       {'records': [], 'diagnostic': {'stage': 'completed'}}])
            manager = CollectorManager(store, runner=runner)
            advert = 1_790_000_000_000
            response = {'supported': True, 'ready': True, 'events': [{'address': device['address'], 'at': advert}]}
            with patch('collectors.bridge_request', return_value=response), \
                    patch('collectors.time.monotonic', return_value=100) as clock, \
                    patch('collectors.time.time', return_value=advert / 1000 + 1.5):
                manager.schedule()
                # Queued in the same tick as the detection; no 10-second wait.
                self.assertEqual(manager.queue.qsize(), 1)
                self.assertFalse(manager.listen_delayed)
                manager.process(manager.queue.get_nowait())
                request = runner.call_args.args[0]
                self.assertEqual(request['advert_at'], advert)
                job = store.jobs()[0]
                self.assertEqual(job['state'], 'skipped')
                self.assertIn('次の新しいBluetooth広告', job['message'])
                self.assertNotIn('60秒後', job['message'])
                listen = json.loads(job['result'])['diagnostic']['listen']
                self.assertEqual(listen, {'attempt': 1, 'burst_attempt': 1, 'advert_at': advert, 'request_after_advert_ms': 1500})
                self.assertFalse(manager.listen_delayed)
                # A missed scale does not wait out the 300-second minimum interval:
                # the next advertisement (next measurement) starts at once.
                clock.return_value = 105
                manager.schedule()
                self.assertEqual(manager.queue.qsize(), 0)  # Same event is already consumed.
                response['events'][0]['at'] = advert + 5000
                clock.return_value = 108  # Listener polls every 2 seconds.
                manager.schedule()
                self.assertEqual(manager.queue.qsize(), 1)
                manager.process(manager.queue.get_nowait())
                self.assertEqual(store.jobs()[0]['state'], 'skipped')
                self.assertIn('次の新しいBluetooth広告', store.jobs()[0]['message'])
                self.assertFalse(manager.listen_delayed)
                # A connection-stage miss also keeps the HBF retry window open
                # for the next fresh advertisement instead of imposing cooldown.
                response['events'][0]['at'] = advert + 10000
                clock.return_value = 111
                manager.schedule()
                self.assertEqual(manager.queue.qsize(), 1)
                manager.process(manager.queue.get_nowait())
                self.assertEqual(store.jobs()[0]['state'], 'done')
                completed_listen = json.loads(store.jobs()[0]['result'])['diagnostic']['listen']
                self.assertEqual((completed_listen['attempt'], completed_listen['burst_attempt']), (1, 3))
                self.assertEqual(runner.call_count, 3)

    def test_hbf_listener_bounds_failed_retries_and_reopens_only_next_probe_window(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            user = store.save_user({'name': 'test'})
            device = store.save_device({'model': 'HBF-228T', 'address': 'AA:BB:CC:DD:EE:FF',
                'bindings': {'1': user['id']}, 'auto_sync': True, 'interval': 300})
            store.pairing_key(device['address'], '11' * 16)
            missing = '機器が見つかりません。機器を通信可能な状態にし、NASへ近づけてください'
            runner = Mock(side_effect=BluetoothFailure(missing, {'stage': 'connection'}))
            manager = CollectorManager(store, runner=runner)
            advert = 1_790_000_000_000
            response = {'supported': True, 'ready': True, 'events': [{'address': device['address'], 'at': advert}]}
            with patch('collectors.bridge_request', return_value=response), \
                    patch('collectors.time.monotonic', return_value=100) as clock, \
                    patch('collectors.time.time', return_value=advert / 1000 + 1):
                for index, seconds in enumerate((0, 10, 20), start=1):
                    response['events'][0]['at'] = advert + seconds * 1000
                    clock.return_value = 100 + seconds
                    manager.schedule()
                    self.assertEqual(manager.queue.qsize(), 1)
                    manager.process(manager.queue.get_nowait())
                    job = store.jobs()[0]
                    listen = json.loads(job['result'])['diagnostic']['listen']
                    self.assertEqual(listen['burst_attempt'], index)
                self.assertEqual(runner.call_count, 3)
                self.assertIn('3回接続できなかった', store.jobs()[0]['message'])

                # Continued advertising in the same probe window does not create
                # an unbounded connection loop after the third miss.
                for seconds in (30, 45, 60, 300, 599):
                    response['events'][0]['at'] = advert + seconds * 1000
                    clock.return_value = 100 + seconds
                    manager.schedule()
                    self.assertTrue(manager.queue.empty())
                self.assertEqual(runner.call_count, 3)

                # At ten minutes a single new safety probe window opens. Its
                # failures are bounded independently instead of every advert
                # being treated as a new burst.
                response['events'][0]['at'] = advert + 600000
                clock.return_value = 700
                manager.schedule()
                self.assertEqual(manager.queue.qsize(), 1)
                manager.process(manager.queue.get_nowait())
                self.assertEqual(runner.call_count, 4)
                listen = json.loads(store.jobs()[0]['result'])['diagnostic']['listen']
                self.assertEqual(listen['burst_attempt'], 1)

                response['events'][0]['at'] = advert + 605000
                clock.return_value = 705
                manager.schedule()
                self.assertEqual(manager.queue.qsize(), 1)

    def test_worker_connects_to_cached_bluez_device_without_scanning(self):
        import sys
        import types
        import ble_worker

        class Reply:
            message_type = 'ok'
            body = []

        class Bus:
            async def connect(self):
                return self
            async def call(self, message):
                return Reply()
            def disconnect(self):
                pass

        dbus = types.ModuleType('dbus_fast')
        dbus.BusType = types.SimpleNamespace(SYSTEM=1)
        dbus.MessageType = types.SimpleNamespace(ERROR='error')
        dbus.Message = lambda **kwargs: kwargs
        dbus.Variant = lambda *args: args
        dbus_aio = types.ModuleType('dbus_fast.aio')
        dbus_aio.MessageBus = lambda **kwargs: Bus()

        class BleakError(Exception):
            pass

        def modules(connect_error=None):
            bleak = types.ModuleType('bleak')
            bleak.BleakScanner = types.SimpleNamespace(find_device_by_address=AsyncMock(return_value=None))
            client = AsyncMock()
            client.connect.side_effect = connect_error
            client.services = Mock()
            client.services.get_service.return_value = None  # Stop right after connecting.
            bleak.BleakClient = Mock(return_value=client)
            exc = types.ModuleType('bleak.exc')
            exc.BleakError = BleakError
            backends = types.ModuleType('bleak.backends')
            device_mod = types.ModuleType('bleak.backends.device')
            device_mod.BLEDevice = lambda address, name, details, rssi: types.SimpleNamespace(address=address, details=details)
            return {'dbus_fast': dbus, 'dbus_fast.aio': dbus_aio, 'bleak': bleak, 'bleak.exc': exc,
                    'bleak.backends': backends, 'bleak.backends.device': device_mod}, bleak, client

        request = {'action': 'sync', 'adapter': 'hci0', 'advert_at': 1_790_000_000_000,
                   'device': {'address': 'AA:BB:CC:DD:EE:FF', 'model': 'HBF-228T'}}
        agent = AsyncMock(return_value=None)
        cached = AsyncMock(return_value=('/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF', {
            'Address': 'AA:BB:CC:DD:EE:FF', 'AddressType': 'public', 'Name': 'BLEsmart_0001000B',
            'Paired': True, 'Bonded': True, 'Trusted': True, 'Blocked': False,
            'Connected': False, 'ServicesResolved': False, 'Connectable': True, 'RSSI': -44}))
        fake, bleak, client = modules()
        trace = {'stage': 'starting', 'slots': []}
        with patch.dict(sys.modules, fake), patch.object(ble_worker, 'register_agent', agent), \
                patch.object(ble_worker, 'cached_device', cached), \
                patch('ble_worker.time.time', return_value=1_790_000_002.0):
            with self.assertRaisesRegex(ValueError, '対応するOmronサービス'):
                asyncio.run(ble_worker.operate(request, trace))
        bleak.BleakScanner.find_device_by_address.assert_not_awaited()
        target = bleak.BleakClient.call_args.args[0]
        self.assertEqual(target.details['path'], '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF')
        client.disconnect.assert_awaited()
        self.assertEqual(trace['discovery_method'], 'bluez_cache')
        self.assertEqual(trace['elapsed_since_advert_ms'], 2000)
        self.assertEqual(trace['bluez_cached']['address_type'], 'public')
        self.assertTrue(trace['bluez_cached']['connectable'])
        self.assertEqual(trace['bluez_cached']['rssi'], -44)
        self.assertEqual(trace['connection_timeout_s'], 8)
        self.assertTrue(trace['connected_transition'])
        self.assertIn('connect_ms', trace)

        # No connectable advertisement during the connect wait is reported as "not found".
        fake, bleak, client = modules(asyncio.TimeoutError())
        trace = {'stage': 'starting', 'slots': []}
        with patch.dict(sys.modules, fake), patch.object(ble_worker, 'register_agent', agent), \
                patch.object(ble_worker, 'cached_device', cached):
            with self.assertRaisesRegex(ValueError, '^機器が見つかりません'):
                asyncio.run(ble_worker.operate(request, trace))
        self.assertEqual(trace['stage'], 'connection')
        self.assertEqual(trace['connection_timeout_s'], 8)
        self.assertEqual(trace['connect_error']['type'], 'TimeoutError')

        # Without a BlueZ object the worker falls back to discovery.
        fake, bleak, client = modules()
        trace = {'stage': 'starting', 'slots': []}
        with patch.dict(sys.modules, fake), patch.object(ble_worker, 'register_agent', agent), \
                patch.object(ble_worker, 'cached_device', AsyncMock(return_value=(None, {}))):
            with self.assertRaisesRegex(ValueError, '^機器が見つかりません'):
                asyncio.run(ble_worker.operate(request, trace))
        bleak.BleakScanner.find_device_by_address.assert_awaited_once()
        self.assertEqual((trace['stage'], trace['discovery_method']), ('discovery', 'scan'))
        self.assertIn('discovery_ms', trace)

    def test_auto_sync_absence_waits_interval_and_errors_stay_errors(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            user = store.save_user({'name': 'test'})
            device = store.save_device({'model': 'HBF-228T', 'address': 'AA:BB:CC:DD:EE:FF',
                'bindings': {'1': user['id']}, 'auto_sync': True, 'sync_mode': 'interval', 'interval': 100})
            store.pairing_key(device['address'], '11' * 16)
            runner = Mock(return_value={'devices': []})
            manager = CollectorManager(store, runner=runner)
            with patch('collectors.time.monotonic', return_value=100):
                manager.schedule()
            self.assertEqual(store.jobs()[0]['state'], 'skipped')
            self.assertIsNotNone(store.jobs()[0]['finished_at'])
            self.assertTrue(manager.queue.empty())
            with patch('collectors.time.monotonic', return_value=199):
                manager.schedule()
            self.assertEqual(runner.call_count, 1)
            runner.return_value = {'devices': [{'address': device['address']}]}
            with patch('collectors.time.monotonic', return_value=200):
                manager.schedule()
            runner.side_effect = BluetoothFailure('機器が見つかりません。機器を通信可能な状態にし、NASへ近づけてください')
            manager.process(manager.queue.get_nowait())
            self.assertEqual(store.jobs()[0]['state'], 'skipped')
            manager.submit('sync', device['id'])
            manager.process(manager.queue.get_nowait())
            self.assertEqual(store.jobs()[0]['state'], 'failed')
            runner.side_effect = BluetoothFailure('read timeout')
            manager.submit('sync', device['id'], automatic=True)
            manager.process(manager.queue.get_nowait())
            self.assertEqual(store.jobs()[0]['state'], 'failed')
            manager.next_attempt.clear()
            manager.next_scan = 0
            runner.side_effect = BluetoothFailure('radio unavailable')
            manager.schedule()
            self.assertEqual(manager.watch_error, 'radio unavailable')

    def test_direct_access_requires_exclusive_adapter(self):
        with tempfile.TemporaryDirectory() as root:
            manager = CollectorManager(Store(root))
            with self.assertRaises(ValueError):
                manager.submit('scan', transport='direct')

    def test_scan_result_keeps_adapter_and_transport_for_registration(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            manager = CollectorManager(store, runner=lambda request, directory: {'devices': [
                {'address': 'AA:BB:CC:DD:EE:FF', 'name': 'BLEsmart_0001000B', 'rssi': -40}]})
            manager.submit('scan', adapter='hci1', transport='homehub')
            manager.process(manager.queue.get_nowait())
            import json
            result = json.loads(store.jobs()[0]['result'])
            self.assertEqual((result['adapter'], result['transport']), ('hci1', 'homehub'))

    def test_diagnostic_sync_counts_slots_and_keeps_only_bounded_samples(self):
        async def exercise():
            raw = bytearray(32)
            raw[2:14] = bytes.fromhex('3e8a64004b1a399e2809c9cc')
            raw[26:28] = bytes.fromhex('5780')
            invalid = bytearray(32)
            invalid[26:28] = bytes.fromhex('5780')  # nonzero weight, impossible month
            memory = bytes(raw) + bytes(invalid) * 3 + bytes(32 * 26)
            trace = {'stage': 'starting', 'slots': []}
            session = Session(AsyncMock(), trace)
            async def read(opcode, address, length):
                return memory[address - p.PROFILES['HBF-228T']['bases'][0]:address - p.PROFILES['HBF-228T']['bases'][0] + length]
            session.request = read
            result = await session.records({'model': 'HBF-228T', 'bindings': {'1': 'private-user-id'},
                                            'utc_offset_minutes': 540, 'id': 'device-id'})
            detail = trace['slots'][0]
            self.assertEqual((detail['valid'], detail['invalid'], detail['empty']), (1, 3, 26))
            self.assertEqual(len(detail['invalid_samples']), 2)
            self.assertEqual(detail['valid_samples'][0]['values']['weight'], 70)
            self.assertEqual(result['invalid_records'], 3)
            self.assertNotIn('private-user-id', str(trace))
        asyncio.run(exercise())

    def test_diagnostic_failure_persists_stage_without_pairing_key(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            user = store.save_user({'name': 'test'})
            device = store.save_device({'model': 'HBF-228T', 'address': 'AA:BB:CC:DD:EE:FF',
                                        'bindings': {'1': user['id']}})
            store.pairing_key(device['address'], '22' * 16)
            def failed(request, _):
                self.assertTrue(request['diagnostic'])
                raise BluetoothFailure('応答なし', {'stage': 'read', 'last_command': {'address': 704}})
            manager = CollectorManager(store, runner=failed)
            manager.submit('sync', device['id'], diagnostic=True)
            manager.process(manager.queue.get_nowait())
            job = store.jobs()[0]
            self.assertEqual(job['state'], 'failed')
            self.assertEqual(__import__('json').loads(job['result'])['diagnostic']['stage'], 'read')
            self.assertNotIn('22' * 16, str(job))
            with self.assertRaises(ValueError):
                manager.submit('scan', diagnostic=True)

    def test_pairing_diagnostic_records_status_only_and_propagates_bridge_failure(self):
        async def exercise():
            trace = {'stage': 'starting', 'slots': []}
            session = Session(AsyncMock(), trace)
            session.unlocks.put_nowait(bytes.fromhex('820f') + bytes(14))
            with self.assertRaises(ValueError):
                await session.unlock(2, bytes.fromhex('ab' * 16), 0x82)
            self.assertEqual(trace['stage'], 'pairing_mode')
            self.assertEqual(trace['unlock_status'], '820f')
            self.assertEqual(trace['expected_unlock_status'], '8200')
            self.assertNotIn('ab' * 16, str(trace))
        asyncio.run(exercise())
        response = Mock(status=400)
        response.read.return_value = b'{"error":"pairing failed","diagnostic":{"stage":"pairing_mode"}}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('collectors.UnixHTTPConnection', return_value=connection):
            with self.assertRaises(BluetoothFailure) as caught:
                bridge_request('/tmp/dummy', {'action': 'pair'})
        self.assertEqual(caught.exception.diagnostic, {'stage': 'pairing_mode'})

    def test_diagnostic_keeps_bounded_device_responses_and_timeout_fragments(self):
        async def exercise():
            trace = {'stage': 'starting', 'slots': []}
            client = AsyncMock()
            client.services = Mock()
            client.services.get_characteristic.return_value = Mock(properties=['write'])
            client.is_connected = True
            session = Session(client, trace)
            frame = bytes([10, 129, 0, 2, 192, 2, 10, 11, 0])
            frame += bytes([p.checksum(frame)])
            for _ in range(9):
                session.frames.put_nowait(frame)
                async def deliver():
                    await asyncio.sleep(0)
                    session.frames.put_nowait(frame)
                asyncio.create_task(deliver())
                self.assertEqual(await session.request(1, 0x2c0, 2), b'\x0a\x0b')
            self.assertEqual(len(trace['responses']), 8)
            self.assertEqual(trace['responses'][-1]['raw_hex'], frame.hex())
            self.assertEqual(trace['responses'][-1]['status'], 'valid')
            async def timeout_with_fragment(awaitable, seconds):
                awaitable.close()
                session.channels[0] = bytes.fromhex('200102')
                raise asyncio.TimeoutError
            with patch('ble_worker.asyncio.wait_for', side_effect=timeout_with_fragment):
                with self.assertRaises(asyncio.TimeoutError):
                    await session.request(1, 0x2c0, 2)
            self.assertEqual(trace['responses'][-1]['error'], 'timeout')
            self.assertEqual(trace['responses'][-1]['fragments'], {'0': '200102'})
            self.assertNotIn('key', str(trace))
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
