import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock

import ble_protocol as p
from ble_worker import Session
from collectors import CollectorManager
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


if __name__ == '__main__':
    unittest.main()
