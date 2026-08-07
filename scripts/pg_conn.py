"""Get a connection to the local test Postgres, starting it if it is not up.

Every script that touches Postgres goes through here, because the server does
not survive between commands on this box: the WSL distro shuts down when idle
and takes the postmaster with it. `pgserver.get_server` is idempotent — it
starts a cluster or attaches to a running one — so calling it every time is
both correct and cheap.

On a real deployment this is unnecessary; Postgres is a service and the DSN
comes from DATABASE_URL. That path is honoured first.
"""
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PGLIB = os.path.join(REPO, ".pgtest")
PGDATA = os.path.join(REPO, ".pgdata")
DSN_FILE = os.path.join(REPO, "scripts", "logs", "pg_dsn.txt")

if PGLIB not in sys.path:
    sys.path.insert(0, PGLIB)


def dsn(explicit: str = None) -> str:
    """DSN for the test cluster, starting it if needed.

    DATABASE_URL wins — that is a real server someone configured, and starting
    a throwaway cluster next to it would be surprising.
    """
    if explicit:
        return explicit
    env = os.environ.get("DATABASE_URL")
    if env:
        return env

    import pgserver
    os.makedirs(PGDATA, exist_ok=True)
    server = pgserver.get_server(PGDATA)
    uri = server.get_uri()
    os.makedirs(os.path.dirname(DSN_FILE), exist_ok=True)
    with open(DSN_FILE, "w") as fh:
        fh.write(uri + "\n")
    return uri


def connect(explicit: str = None, autocommit: bool = True):
    import psycopg
    return psycopg.connect(dsn(explicit), autocommit=autocommit)
