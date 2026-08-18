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


LOCAL_DSN = "postgresql://postgres@127.0.0.1:5432/postgres"


def _server_already_running(host="127.0.0.1", port=5432, timeout=0.4):
    """Is something already listening on the local cluster's port?

    pgserver.get_server() OWNS the cluster it returns and fast-shuts it when the
    process exits. Called against the same .pgdata that scripts/pg_dev.sh
    started, a one-second script silently kills a server the developer is using.
    Attach to what is already there instead.
    """
    import socket
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def dsn(explicit: str = None) -> str:
    """DSN for the test cluster, starting it if needed.

    DATABASE_URL wins — that is a real server someone configured, and starting
    a throwaway cluster next to it would be surprising. A cluster that is
    already running wins next, for the same reason and more sharply: taking
    ownership of it means stopping it on the way out.
    """
    if explicit:
        return explicit
    env = os.environ.get("DATABASE_URL")
    if env:
        return env
    if _server_already_running():
        return LOCAL_DSN

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
