import copy
import json
import tempfile
import unittest
from storage import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.user = self.store.save_user({"name": "Test"})
        self.record = {"user_id": self.user["id"], "kind": "blood_pressure", "measured_at": "2026-09-25T07:30:12+09:00",
                       "values": {"systolic": 130, "diastolic": 80, "pulse": 72}, "note": "test"}

    def test_restart_and_timezone_independent_deduplication(self):
        self.assertEqual(self.store.add_records([self.record])["inserted"], 1)
        other = dict(self.record, measured_at="2026-09-24T22:30:12Z")
        self.assertEqual(self.store.add_records([other], "api")["duplicates"], 1)
        self.assertEqual(Store(self.tmp.name).records()["total"], 1)

    def test_batch_is_atomic_and_rejects_nan(self):
        invalid = copy.deepcopy(self.record)
        invalid["values"]["pulse"] = float("nan")
        with self.assertRaises(ValueError):
            self.store.add_records([self.record, invalid])
        self.assertEqual(self.store.records()["total"], 0)

    def test_users_are_not_merged(self):
        user2 = self.store.save_user({"name": "Other"})
        self.store.add_records([self.record, dict(self.record, user_id=user2["id"])])
        self.assertEqual(self.store.records(user_id=self.user["id"])["total"], 1)

    def test_csv_roundtrip_and_formula_escaping(self):
        self.record["note"] = "=1+1"
        self.store.add_records([self.record])
        csv = self.store.export_csv()
        self.assertIn("'=1+1", csv)
        self.assertEqual(self.store.import_csv(csv)["duplicates"], 1)

    def test_edit_and_delete_are_not_undone_by_bluetooth_retry(self):
        self.store.add_records([self.record])
        record = self.store.records()["records"][0]
        revised = copy.deepcopy(self.record)
        revised["values"]["pulse"] = 73
        self.store.edit_record(record["id"], revised)
        self.assertEqual(self.store.add_records([self.record], "bluetooth")["inserted"], 0)
        self.store.delete_record(record["id"])
        self.assertEqual(self.store.add_records([revised], "bluetooth")["inserted"], 0)
        self.assertEqual(self.store.records()["total"], 0)

    def test_backup_restore_is_atomic_and_preserves_values(self):
        self.store.add_records([self.record])
        backup = self.store.backup()
        with tempfile.TemporaryDirectory() as target:
            restored = Store(target)
            invalid = copy.deepcopy(backup)
            invalid["records"][0]["measured_at"] = "bad"
            with self.assertRaises(ValueError):
                restored.restore(invalid)
            self.assertEqual(restored.users(), [])
            restored.restore(backup)
            self.assertEqual(restored.records()["records"][0]["values"], self.record["values"])
            self.assertEqual(restored.records()["records"], self.store.records()["records"])
            with self.assertRaises(ValueError):
                restored.restore(backup)

    def test_slots_and_ids_are_validated(self):
        with self.assertRaises(ValueError):
            self.store.save_device({"id": "../../bad", "model": "HEM-6232T"})
        with self.assertRaises(ValueError):
            self.store.save_device({"model": "HEM-6232T", "bindings": {"3": self.user["id"]}})
        with self.assertRaises(ValueError):
            self.store.add_records([dict(self.record, measured_at="2026-09-25T07:30:12")])

    def test_keys_are_not_exported(self):
        self.store.pairing_key("AA:BB:CC:DD:EE:FF", "secret")
        self.assertNotIn("secret", json.dumps(self.store.backup()))


if __name__ == '__main__':
    unittest.main()
