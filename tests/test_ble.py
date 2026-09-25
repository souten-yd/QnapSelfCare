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
