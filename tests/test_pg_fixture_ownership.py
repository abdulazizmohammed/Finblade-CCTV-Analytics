"""The test suite must never stop a Postgres it did not start.

WHAT WENT WRONG. pgserver.get_server() does not attach to a cluster, it OWNS
one: when the owning process exits it issues a fast shutdown. tests/pgfixture.py
pointed it at the same .pgdata that scripts/pg_dev.sh uses, so running the suite
killed the developer's server. The symptom appeared minutes later and nowhere
near the cause — the API refusing to boot with

    psycopg_pool.PoolTimeout: pool initialization incomplete after 15.0 sec

Borrowing a running server and owning one are different things to do to a
machine somebody is working on, and only one of them is acceptable here.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import pgfixture
from scripts import pg_conn


class _NoServer:
    """Stand in for the probe. Records that it was asked."""

    def __init__(self, running):
        self.running = running
        self.asked = False

    def __call__(self, *a, **kw):
        self.asked = True
        return self.running


class TestFixtureOwnership(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None)
                     for k in ("FINBLADE_TEST_DSN", "DATABASE_URL")}
        self._probe = pgfixture._server_already_running
        self._own = pgfixture._own_a_cluster
        # NEVER let a test reach the real thing. Taking ownership registers an
        # exit hook that stops the postmaster, so a test that calls it kills the
        # developer's server — which is what the first version of this file did.
        self.owned = []
        pgfixture._own_a_cluster = lambda: (self.owned.append(1),
                                            "postgresql://spawned/db")[1]

    def tearDown(self):
        pgfixture._server_already_running = self._probe
        pgfixture._own_a_cluster = self._own
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_a_running_server_is_borrowed_not_started(self):
        probe = _NoServer(True)
        pgfixture._server_already_running = probe
        self.assertEqual(pgfixture.LOCAL_DSN, pgfixture.dsn())
        self.assertTrue(probe.asked)
        self.assertEqual([], self.owned, "it started a cluster anyway")

    def test_a_cluster_is_only_started_when_nothing_is_running(self):
        """The fallback has to stay reachable — a CI box with no server still
        needs one — but it must be the LAST resort. Asserted through a stub;
        calling the real one is what caused the outage this file is about."""
        pgfixture._server_already_running = _NoServer(False)
        self.assertEqual("postgresql://spawned/db", pgfixture.dsn())
        self.assertEqual([1], self.owned)

    def test_an_explicit_dsn_still_wins(self):
        os.environ["FINBLADE_TEST_DSN"] = "postgresql://someone@elsewhere/db"
        pgfixture._server_already_running = _NoServer(True)
        self.assertEqual("postgresql://someone@elsewhere/db", pgfixture.dsn())
        self.assertEqual([], self.owned)

    def test_database_url_still_wins(self):
        os.environ["DATABASE_URL"] = "postgresql://someone@elsewhere/db"
        pgfixture._server_already_running = _NoServer(True)
        self.assertEqual("postgresql://someone@elsewhere/db", pgfixture.dsn())
        self.assertEqual([], self.owned)

    def test_no_test_in_this_suite_may_start_a_cluster(self):
        """The rule, stated as a test. tests/pgfixture.py resolves its DSN once
        at import; if that import ever takes ownership, every subsequent run
        stops the developer's server at exit."""
        import inspect
        src = inspect.getsource(pgfixture.dsn)
        # The call, not the word — the docstring discusses pgserver at length,
        # and it should. What must not appear is the call that takes ownership.
        self.assertNotIn("get_server", src,
                         "dsn() must delegate ownership to _own_a_cluster(), "
                         "not perform it, or a stub cannot intercept it")


class TestScriptOwnership(unittest.TestCase):
    """scripts/pg_conn.py has the same hazard — every Postgres script goes
    through it, so a one-second diagnostic could stop the server too."""

    def setUp(self):
        self._env = os.environ.pop("DATABASE_URL", None)
        self._probe = pg_conn._server_already_running

    def tearDown(self):
        pg_conn._server_already_running = self._probe
        if self._env is not None:
            os.environ["DATABASE_URL"] = self._env

    def test_a_running_server_is_borrowed(self):
        pg_conn._server_already_running = lambda *a, **k: True
        self.assertEqual(pg_conn.LOCAL_DSN, pg_conn.dsn())

    def test_an_explicit_dsn_beats_the_probe(self):
        pg_conn._server_already_running = lambda *a, **k: True
        self.assertEqual("postgresql://x@y/z", pg_conn.dsn("postgresql://x@y/z"))


class TestTheProbeItself(unittest.TestCase):
    def test_a_closed_port_reads_as_not_running(self):
        # 9 is discard; nothing listens on it in this environment.
        self.assertFalse(pgfixture._server_already_running(port=9, timeout=0.2))

    def test_it_does_not_raise_on_a_dead_host(self):
        """The probe runs before every suite. It must fail closed, not throw."""
        self.assertIs(False, pgfixture._server_already_running(
            host="127.0.0.1", port=1, timeout=0.2))


if __name__ == "__main__":
    unittest.main()
