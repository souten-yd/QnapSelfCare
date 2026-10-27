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

    def record(self, session, status, response, preview='', size=None, truncated=False):
        with self.lock:
            if not session or session != self.begin():
                return
            # No arbitrary headers, URLs, query strings, credentials or client IPs.
            result = {k: response[k] for k in ('inserted', 'duplicates', 'received', 'counts', 'start_at', 'end_at', 'processing_ms') if k in response}
            if 'error' in response:
                result['error'] = str(response['error'])[:1000]
            self.entries.append({'received_at': datetime.now(timezone.utc).isoformat(),
                                 'status': status, 'result': result, 'body_bytes': size,
                                 'body_preview': preview[:8192], 'truncated': truncated})

    def snapshot(self):
        with self.lock:
            active = bool(self.begin())
            return {'enabled': active, 'remaining_seconds': max(0, int(self.deadline - time.monotonic())) if active else 0,
                    'max_entries': 20, 'preview_bytes': 8192,
                    'entries': copy.deepcopy(list(self.entries))}


def for_server(server):
    with INIT_LOCK:
        if not hasattr(server, 'health_debug'):
            server.health_debug = HealthDebug()
        return server.health_debug
