import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from site_runtime.rate_limit import PublicRateLimiter


class SharedRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'limits.db'

    def limiter(self, **values):
        return PublicRateLimiter(self.path, chat_limit=values.get('limit', 3),
            appointment_limit=3, window_seconds=values.get('window', 60),
            max_clients=values.get('max_clients', 100), clock=values.get('clock', lambda: 1000))

    def test_limit_and_metrics_survive_new_instance(self):
        first = self.limiter(limit=2)
        self.assertEqual(first.check('chat', '203.0.113.4'), (True, 0))
        second = self.limiter(limit=2)
        self.assertEqual(second.check('chat', '203.0.113.4'), (True, 0))
        self.assertEqual(first.check('chat', '203.0.113.4'), (False, 60))
        report = PublicRateLimiter(self.path, chat_limit=2, appointment_limit=3,
            window_seconds=60, clock=lambda: 1000).metrics()
        self.assertEqual(report['storage'], 'sqlite_shared')
        self.assertEqual(report['routes']['chat']['allowed'], 2)
        self.assertEqual(report['routes']['chat']['limited'], 1)
        self.assertEqual(report['routes']['chat']['active_clients'], 1)

    def test_window_expiry_is_shared(self):
        now = [1000]
        first = self.limiter(limit=1, clock=lambda: now[0])
        self.assertTrue(first.check('chat', 'client')[0])
        self.assertFalse(self.limiter(limit=1, clock=lambda: now[0]).check('chat', 'client')[0])
        now[0] = 1060
        self.assertTrue(first.check('chat', 'client')[0])

    def test_concurrent_instances_enforce_one_atomic_limit(self):
        limit, total = 7, 20
        self.limiter(limit=limit)
        barrier, results, lock = threading.Barrier(total), [], threading.Lock()
        def request():
            current = self.limiter(limit=limit)
            barrier.wait()
            allowed = current.check('chat', 'shared-client')[0]
            with lock: results.append(allowed)
        threads = [threading.Thread(target=request) for _ in range(total)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(results), limit)
        self.assertEqual(len(results), total)

    def test_client_identifiers_are_not_stored(self):
        client = 'private-client-address'
        limiter = self.limiter()
        limiter.check('chat', client)
        with closing(sqlite3.connect(self.path)) as database:
            stored = database.execute('SELECT client_hash FROM rate_limit_requests').fetchone()[0]
        self.assertNotEqual(stored, client)
        self.assertNotIn(client, self.path.read_bytes().decode('latin1'))

    def test_routes_have_independent_limits_and_bounded_clients(self):
        limiter = self.limiter(limit=1, max_clients=2)
        self.assertTrue(limiter.check('chat', 'one')[0])
        self.assertTrue(limiter.check('appointments', 'one')[0])
        self.assertTrue(limiter.check('chat', 'two')[0])
        with closing(sqlite3.connect(self.path)) as database:
            count = database.execute('SELECT COUNT(*) FROM (SELECT 1 FROM rate_limit_requests GROUP BY route,client_hash)').fetchone()[0]
        self.assertLessEqual(count, 2)
