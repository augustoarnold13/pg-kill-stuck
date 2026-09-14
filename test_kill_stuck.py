import unittest

from kill_stuck import Connection, should_terminate


def conn(**overrides):
    data = {
        "pid": 1,
        "datname": "conectamente",
        "usename": "app",
        "application_name": "DBeaver",
        "client_addr": "127.0.0.1",
        "state": "active",
        "backend_type": "client backend",
        "query": "SELECT 1",
        "query_age_seconds": 10,
        "xact_age_seconds": 10,
        "state_age_seconds": 10,
        "is_self": False,
    }
    data.update(overrides)
    return Connection(**data)


class ShouldTerminateTest(unittest.TestCase):
    def test_keeps_own_backend(self):
        self.assertFalse(should_terminate(conn(is_self=True, query_age_seconds=600)))

    def test_keeps_background_workers(self):
        self.assertFalse(
            should_terminate(conn(backend_type="autovacuum worker", query_age_seconds=600))
        )

    def test_keeps_idle_select_under_5_minutes(self):
        self.assertFalse(
            should_terminate(conn(state="idle", query="SELECT 1", query_age_seconds=299))
        )

    def test_kills_idle_select_after_5_minutes(self):
        self.assertTrue(
            should_terminate(conn(state="idle", query="SELECT 1", query_age_seconds=1772))
        )

    def test_kills_idle_rollback_after_3_minutes(self):
        self.assertTrue(
            should_terminate(
                conn(state="idle", query="ROLLBACK", query_age_seconds=1121)
            )
        )

    def test_keeps_idle_rollback_under_3_minutes(self):
        self.assertFalse(
            should_terminate(conn(state="idle", query="ROLLBACK", query_age_seconds=179))
        )

    def test_kills_commit_after_3_minutes(self):
        self.assertTrue(
            should_terminate(conn(query="COMMIT", query_age_seconds=181, xact_age_seconds=181))
        )

    def test_keeps_commit_under_3_minutes(self):
        self.assertFalse(
            should_terminate(conn(query="commit;", query_age_seconds=179, xact_age_seconds=179))
        )

    def test_kills_rollback_after_3_minutes(self):
        self.assertTrue(
            should_terminate(
                conn(query="ROLLBACK TO SAVEPOINT x", query_age_seconds=180, xact_age_seconds=180)
            )
        )

    def test_kills_other_active_query_after_3_minutes(self):
        self.assertTrue(
            should_terminate(conn(query="SELECT * FROM heavy", query_age_seconds=181))
        )

    def test_keeps_other_active_query_under_3_minutes(self):
        self.assertFalse(
            should_terminate(conn(query="SELECT * FROM heavy", query_age_seconds=179))
        )

    def test_kills_idle_in_transaction_after_3_minutes_idle(self):
        self.assertTrue(
            should_terminate(
                conn(
                    state="idle in transaction",
                    query="UPDATE users SET name = 'x'",
                    query_age_seconds=10,
                    xact_age_seconds=181,
                    state_age_seconds=181,
                )
            )
        )

    def test_keeps_idle_in_transaction_under_3_minutes_idle(self):
        self.assertFalse(
            should_terminate(
                conn(
                    state="idle in transaction",
                    query="UPDATE users SET name = 'x'",
                    query_age_seconds=10,
                    xact_age_seconds=179,
                    state_age_seconds=179,
                )
            )
        )

    def test_keeps_long_transaction_with_recent_queries(self):
        self.assertFalse(
            should_terminate(
                conn(
                    state="idle in transaction",
                    query="SELECT user_plans.id AS user_plans_id",
                    query_age_seconds=0.15,
                    xact_age_seconds=12 * 60,
                    state_age_seconds=0.15,
                )
            )
        )

    def test_kills_idle_in_transaction_aborted_after_3_minutes(self):
        self.assertTrue(
            should_terminate(
                conn(
                    state="idle in transaction (aborted)",
                    query="INSERT INTO t VALUES (1)",
                    xact_age_seconds=180,
                    state_age_seconds=180,
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
