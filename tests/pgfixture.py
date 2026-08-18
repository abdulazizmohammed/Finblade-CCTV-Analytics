"""A real Postgres store for tests, isolated per caller.

SQLite used to fill this role: a durable backend you could create in a temp file
and throw away. With it removed, tests that genuinely need durability — anything
asserting a value survives a new store instance — need a real server.

Each caller gets its own SCHEMA rather than its own database. Creating a
database costs a template copy and serialises against other connections;
creating a schema is instant, and search_path makes it invisible to everyone
else. Dropping it CASCADE afterwards leaves nothing behind.

Skips cleanly when no server is configured. That is a real weakening compared
with SQLite, which needed nothing — so tests that do NOT need durability should
use InMemoryStore and keep running everywhere.
"""

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DDL = os.path.join(REPO, "services", "api", "ddl_pg.sql")

_counter = 0


def dsn():
    """A DSN for a scratch Postgres, or None to skip.

    Prefers FINBLADE_TEST_DSN, then DATABASE_URL, then the local cluster that
    scripts/pg_dev.sh runs. Never invents one.
    """
    for var in ("FINBLADE_TEST_DSN", "DATABASE_URL"):
        if os.environ.get(var):
            return os.environ[var]
    pglib = os.path.join(REPO, ".pgtest")
    pgdata = os.path.join(REPO, ".pgdata")
    if not os.path.isdir(pglib) or not os.path.isdir(pgdata):
        return None
    if pglib not in sys.path:
        sys.path.insert(0, pglib)
    try:
        import pgserver
        return pgserver.get_server(pgdata).get_uri()
    except Exception:                                   # noqa: BLE001
        return None


DSN = dsn()
skip_without_pg = unittest.skipIf(DSN is None, "no Postgres available")


def _scoped(base, schema):
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options=-csearch_path%3D{schema}"


def make_store(prefix="t"):
    """(store, teardown) — a PostgresStore on a fresh schema with the full DDL.

    Call teardown() to drop the schema. The store is a normal PostgresStore, so
    anything asserted through it is asserted against the shipping code path.
    """
    global _counter
    import psycopg
    from services.api.postgres_store import PostgresStore

    _counter += 1
    schema = f"{prefix}_{os.getpid()}_{_counter}"

    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.execute(f"CREATE SCHEMA {schema}")

    scoped = _scoped(DSN, schema)
    # apply_schema=False: PostgresStore would apply the DDL to whatever
    # search_path resolves to, and we want it inside the scratch schema only.
    with psycopg.connect(scoped, autocommit=True) as c:
        c.execute(open(DDL).read())

    store = PostgresStore(scoped, apply_schema=False)

    def teardown():
        try:
            store.close()
        except Exception:                               # noqa: BLE001
            pass
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    return store, teardown


def reopen(store):
    """A SECOND store on the same schema — the 'did it survive a restart' case.

    SQLite tests did this by constructing SQLiteStore on the same path twice.
    The equivalent here is a new pool against the same search_path.
    """
    from services.api.postgres_store import PostgresStore
    return PostgresStore(store.dsn, apply_schema=False)


# ---------------------------------------------------------------------------
# Drop-in for the SQLiteStore(path) pattern these tests were written around.
#
# Their semantics were "same path, same database" — construct on a temp path,
# construct again on the same path to prove a value survived. store_for keeps
# that contract by mapping a path string to a stable SCHEMA, so the port is a
# symbol substitution rather than a rewrite of every setUp.
#
# Raises SkipTest with no server, so every test using it skips rather than
# erroring on a machine without Postgres. That is worse than SQLite, which
# needed nothing — it is the cost of one backend, and it is why tests that do
# not need durability should stay on InMemoryStore.
_SCHEMAS = {}


def store_for(path):
    """A PostgresStore keyed by `path`. Same path, same data."""
    import hashlib
    import atexit
    import psycopg
    from services.api.postgres_store import PostgresStore

    if DSN is None:
        raise unittest.SkipTest("no Postgres available")

    key = str(path)
    schema = _SCHEMAS.get(key)
    if schema is None:
        digest = hashlib.sha1(key.encode()).hexdigest()[:12]
        schema = f"fx_{os.getpid()}_{digest}"
        _SCHEMAS[key] = schema
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.execute(f"CREATE SCHEMA {schema}")
        with psycopg.connect(_scoped(DSN, schema), autocommit=True) as c:
            c.execute(open(DDL).read())
        atexit.register(_drop, schema)

    return PostgresStore(_scoped(DSN, schema), apply_schema=False)


def _drop(schema):
    try:
        import psycopg
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    except Exception:                                   # noqa: BLE001
        pass
