"""SQLite 连接调优（db_tuning）测试：busy_timeout 生效 + PRAGMA 注入契约。"""
from django.db import connection
from django.test import TestCase


class SqliteTuningTest(TestCase):
    def test_busy_timeout_matches_options_timeout(self):
        if connection.vendor != 'sqlite':
            self.skipTest('仅 SQLite 环境验证')
        expected = int(float(connection.settings_dict.get('OPTIONS', {})
                             .get('timeout', 5)) * 1000)
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA busy_timeout;')
            timeout = cursor.fetchone()[0]
        self.assertEqual(int(timeout), expected)

    def test_receiver_executes_wal_and_busy_timeout_for_sqlite(self):
        from quant_engine.db_tuning import configure_sqlite_pragmas

        executed = []

        class _FakeConn:
            vendor = 'sqlite'
            settings_dict = {'OPTIONS': {'timeout': 5}}

            class _Cursor:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def execute(self, sql):
                    executed.append(sql)

            def cursor(self):
                return self._Cursor()

        configure_sqlite_pragmas(None, _FakeConn())
        self.assertIn('PRAGMA journal_mode=WAL;', executed)
        self.assertIn('PRAGMA busy_timeout=5000;', executed)

    def test_receiver_ignores_non_sqlite(self):
        from quant_engine.db_tuning import configure_sqlite_pragmas

        class _FakeConn:
            vendor = 'postgresql'

            def cursor(self):
                raise AssertionError('非 SQLite 连接不应执行 PRAGMA')

        configure_sqlite_pragmas(None, _FakeConn())
