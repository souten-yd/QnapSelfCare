"""Opt-in, bounded RAM-only diagnostics for the Shortcuts receiver."""
import base64
from datetime import datetime, timezone
import copy
import threading
import time
import uuid

INIT_LOCK = threading.Lock()


class HealthDebug:
    def __init__(self, duration=600):
        self.lock = threading.RLock()
        self.duration = duration
        self.entries = []
        self.total_bytes = 0
        self.max_bytes = 128 * 1024 * 1024
        self.max_entries = 10000
        self.omitted = 0
        self.session = None
        self.deadline = 0
        self.timer = None

    def _expire(self, session):
        with self.lock:
            if self.session == session:
                self.session = None
                self.deadline = 0

    def begin(self):
        with self.lock:
            if time.monotonic() >= self.deadline:
                self._expire(self.session)
            return self.session

    def control(self, action):
        with self.lock:
            if action not in ('start', 'stop', 'clear'):
                raise ValueError('actionはstart/stop/clearです')
            self.entries.clear()
            self.total_bytes = 0
            self.omitted = 0
            if action == 'clear':
                # Invalidate requests already in flight without changing the deadline.
                if self.session:
                    self.session = uuid.uuid4().hex
                    if self.timer:
                        self.timer.cancel()
                    self.timer = threading.Timer(max(0, self.deadline - time.monotonic()), self._expire, (self.session,))
                    self.timer.daemon = True
                    self.timer.start()
            else:
                if self.timer:
                    self.timer.cancel()
                self._expire(self.session)
                if action == 'start':
                    self.session = uuid.uuid4().hex
                    self.deadline = time.monotonic() + self.duration
                    self.timer = threading.Timer(self.duration, self._expire, (self.session,))
                    self.timer.daemon = True
                    self.timer.start()
            return self.snapshot()

    def start_request(self, session, declared_bytes=None):
        """Log an import as soon as it arrives, so a slow upload or slow save shows as 'receiving'
        instead of looking like the iPhone never sent anything. record() completes the same entry."""
        with self.lock:
            if not session or session != self.begin():
                return None
            if len(self.entries) >= self.max_entries:
                self.omitted += 1
                return None
            entry = {'received_at': datetime.now(timezone.utc).isoformat(), 'status': 'receiving', 'result': {},
                     'body_bytes': None, 'declared_bytes': declared_bytes, 'body_preview': '', 'truncated': False,
                     '_started': time.monotonic()}
            self.entries.append(entry)
            return entry

    def capture(self, session, entry, raw):
        with self.lock:
            if not session or not any(e is entry for e in self.entries):
                return
            if self.total_bytes + len(raw) > self.max_bytes:
                entry['body_capture'] = 'omitted_capacity'
                self.omitted += 1
                return
            entry['_raw'] = raw
            entry['body_capture'] = 'complete'
            self.total_bytes += len(raw)

    def record(self, session, status, response, preview='', size=None, truncated=False, entry=None):
        with self.lock:
            if not session or session != self.session:
                # Requests admitted before the deadline may finish afterwards.
                if entry is None or not any(e is entry for e in self.entries):
                    return
            # No arbitrary headers, URLs, query strings, credentials or client IPs.
            result = {k: response[k] for k in ('inserted', 'duplicates', 'received', 'counts', 'start_at', 'end_at', 'processing_ms') if k in response}
            if 'error' in response:
                result['error'] = str(response['error'])[:1000]
            done = {'status': status, 'result': result, 'body_bytes': size,
                    'body_preview': preview[:8192], 'truncated': truncated}
            if entry is not None and any(e is entry for e in self.entries):
                done['elapsed_ms'] = round((time.monotonic() - entry.pop('_started', time.monotonic())) * 1000)
                entry.update(done)
            elif session == self.begin() and len(self.entries) < self.max_entries:
                self.entries.append(dict(done, received_at=datetime.now(timezone.utc).isoformat()))

    def snapshot(self, full=False):
        with self.lock:
            active = bool(self.begin())
            return {'enabled': active, 'remaining_seconds': max(0, int(self.deadline - time.monotonic())) if active else 0,
                    'max_entries': self.max_entries, 'preview_bytes': 8192,
                    'total_entries': len(self.entries), 'captured_bytes': self.total_bytes,
                    'max_capture_bytes': self.max_bytes, 'omitted': self.omitted,
                    'complete': self.omitted == 0 and all(e.get('status') != 'receiving' for e in self.entries),
                    'entries': [self._public(e, full) for e in (self.entries if full else self.entries[-20:])]}

    @staticmethod
    def _public(entry, full=False):
        e = copy.deepcopy({k: v for k, v in entry.items() if not k.startswith('_')})
        if full and '_raw' in entry:
            try:
                e['body'] = entry['_raw'].decode('utf-8')
            except UnicodeDecodeError:
                e['body_base64'] = base64.b64encode(entry['_raw']).decode('ascii')
                e['body_encoding'] = 'base64'
        if entry.get('status') == 'receiving' and '_started' in entry:
            e['elapsed_ms'] = round((time.monotonic() - entry['_started']) * 1000)
        return e


def for_server(server):
    with INIT_LOCK:
        if not hasattr(server, 'health_debug'):
            server.health_debug = HealthDebug()
        return server.health_debug
