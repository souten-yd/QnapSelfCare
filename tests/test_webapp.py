from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import Mock, patch

import webapp
from storage import Store


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
        self.server.store = Store(self.tmp.name)
        self.server.collector = Mock()
        self.server.collector.diagnostics.return_value = {'mode': 'homehub'}
        self.server.updates = Mock()
        self.server.updates.status.return_value = {'phase': 'idle', 'busy': False, 'supported': True}
        self.server.updates.apply.return_value = {'phase': 'queued', 'busy': True, 'id': 'test'}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2); self.tmp.cleanup()

    def request(self, path, body=None, method=None, header=True):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'}
        if header: headers['X-SelfCare-Request'] = '1'
        return urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=3)

    def test_open_dashboard_and_status(self):
        with self.request('/') as response:
            self.assertIn('QnapSelfCare', response.read().decode())
        with self.request('/api/status') as response:
            data = json.load(response)
        self.assertEqual(data['record_count'], 0)
        self.assertFalse(data['collection_enabled'])

    def test_save_history_export_edit_and_delete(self):
        with self.request('/api/users', {'name': 'Test'}) as response:
            user = json.load(response)
        body = {'user_id': user['id'], 'kind': 'body_composition', 'measured_at': '2026-09-25T09:00:00+09:00', 'values': {'weight': 70}}
        with self.request('/api/records', body) as response:
            self.assertEqual(json.load(response)['inserted'], 1)
        with self.request('/api/records') as response:
            record = json.load(response)['records'][0]
        body['values']['weight'] = 71
        with self.request('/api/records/' + record['id'], body) as response:
            self.assertTrue(json.load(response)['updated'])
        with self.request('/api/export.csv') as response:
            self.assertIn('71.0', response.read().decode())
        with self.request('/api/records/' + record['id'], {}, 'DELETE') as response:
            self.assertTrue(json.load(response)['deleted'])

    def test_cross_site_form_cannot_mutate(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/users', {'name': 'Bad'}, header=False)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.server.store.users(), [])

    def test_invalid_import_rolls_back(self):
        user = self.server.store.save_user({'name':'Test'})
        with self.assertRaises(HTTPError) as error:
            self.request('/api/import', {'records': [{'user_id':user['id'], 'kind':'body_composition', 'measured_at':'invalid', 'values':{'weight':70}}]})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.server.store.records()['total'], 0)

    def test_adapter_detection_is_read_only(self):
        root = Path(self.tmp.name)
        (root / 'hci1').mkdir(); (root / 'hci0').mkdir()
        self.assertEqual(webapp.bluetooth_adapters(root), ['hci0', 'hci1'])

    def test_update_status_and_check_do_not_install(self):
        with self.request('/api/update/status') as response:
            self.assertEqual(json.load(response)['phase'], 'idle')
        with patch.object(webapp.updater, 'latest', return_value={'version': 'v0.3.2', 'url': 'release'}):
            with self.request('/api/update') as response:
                self.assertTrue(json.load(response)['available'])
        self.server.updates.apply.assert_not_called()
        with self.request('/update.js') as response:
            self.assertIn('api/update/apply', response.read().decode())

    def test_update_apply_accepts_only_selected_version_with_header(self):
        for body, header, status in (({'version':'v0.3.2'}, False, 403),
                                     ({'version':'v0.3.2','url':'https://evil.example/x'}, True, 400)):
            with self.assertRaises(HTTPError) as error:
                self.request('/api/update/apply', body, header=header)
            self.assertEqual(error.exception.code, status)
        self.server.updates.apply.assert_not_called()
        with self.request('/api/update/apply', {'version':'v0.3.2'}) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.load(response)['phase'], 'queued')
        self.server.updates.apply.assert_called_once_with('v0.3.2')

    def test_busy_update_returns_conflict(self):
        self.server.updates.apply.side_effect = webapp.UpdateConflict('busy')
        with self.assertRaises(HTTPError) as error:
            self.request('/api/update/apply', {'version':'v0.3.2'})
        self.assertEqual(error.exception.code, 409)

if __name__ == '__main__':
    unittest.main()
