"""Opt-in, bounded RAM-only diagnostics for the Shortcuts receiver."""
from collections import deque
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
        self.entries = deque(maxlen=20)
        self.session = None
        self.deadline = 0
        self.timer = None

    def _expire(self, session):
        with self.lock:
            if self.session == session:
                self.entries.clear()
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
            if action == 'clear':
                self.entries.clear()
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
            entry = {'received_at': datetime.now(timezone.utc).isoformat(), 'status': 'receiving', 'result': {},
                     'body_bytes': None, 'declared_bytes': declared_bytes, 'body_preview': '', 'truncated': False,
                     '_started': time.monotonic()}
            self.entries.append(entry)
            return entry

    def record(self, session, status, response, preview='', size=None, truncated=False, entry=None):
        with self.lock:
            if not session or session != self.begin():
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
            else:
                self.entries.append(dict(done, received_at=datetime.now(timezone.utc).isoformat()))

    def snapshot(self):
        with self.lock:
            active = bool(self.begin())
            return {'enabled': active, 'remaining_seconds': max(0, int(self.deadline - time.monotonic())) if active else 0,
                    'max_entries': 20, 'preview_bytes': 8192,
                    'entries': [self._public(e) for e in self.entries]}

    @staticmethod
    def _public(entry):
        e = copy.deepcopy({k: v for k, v in entry.items() if k != '_started'})
        if entry.get('status') == 'receiving' and '_started' in entry:
            e['elapsed_ms'] = round((time.monotonic() - entry['_started']) * 1000)
        return e


def for_server(server):
    with INIT_LOCK:
        if not hasattr(server, 'health_debug'):
            server.health_debug = HealthDebug()
        return server.health_debug
