import asyncio
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

    def test_listener_defaults_and_event_cooldown_without_periodic_scan(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            user = store.save_user({'name': 'test'})
            device = store.save_device({'model': 'HBF-228T', 'address': 'AA:BB:CC:DD:EE:FF',
                'bindings': {'1': user['id']}, 'auto_sync': True, 'interval': 100})
            self.assertEqual(device['sync_mode'], 'listen')
            store.pairing_key(device['address'], '11' * 16)
            runner = Mock(return_value={'records': []})
            manager = CollectorManager(store, runner=runner)
            response = {'supported': True, 'ready': True, 'events': [{'address': device['address'], 'at': 1234}]}
            with patch('collectors.bridge_request', return_value=response) as bridge, patch('collectors.time.monotonic', return_value=100):
                manager.schedule()
                runner.assert_not_called()
                self.assertEqual(bridge.call_args.kwargs['endpoint'], '/watch')
                self.assertEqual(manager.queue.qsize(), 1)
                manager.process(manager.queue.get_nowait())
            with patch('collectors.bridge_request', return_value=response), patch('collectors.time.monotonic', return_value=300):
                manager.schedule()
                self.assertTrue(manager.queue.empty())  # Same event is not consumed twice.
            response['events'][0]['at'] = 1235
            with patch('collectors.bridge_request', return_value=response), patch('collectors.time.monotonic', return_value=303):
                manager.schedule()
                self.assertEqual(manager.queue.qsize(), 1)
                manager.process(manager.queue.get_nowait())
            store.save_device(dict(device, auto_sync=False))
            with patch('collectors.bridge_request', return_value={'supported': True, 'ready': False, 'events': []}) as bridge, patch('collectors.time.monotonic', return_value=306):
                manager.schedule()
                self.assertEqual(bridge.call_args.args[1]['addresses'], [])
                self.assertFalse(manager.watch_configured)
            with self.assertRaises(ValueError):
                store.save_device(dict(device, transport='direct', sync_mode='listen'))

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
