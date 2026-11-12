"""Shared database helpers used by every service."""
import os
import socket
import time
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://brew:brew@localhost:5432/brewchain")
POD_NAME = socket.gethostname()          # in Kubernetes this is the pod name

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=int(os.environ.get("DB_POOL_SIZE", "3")),
    kwargs={"row_factory": dict_row},
    open=False,
)


def start():
    """Open the pool and create tables. Retries while the database is starting."""
    for attempt in range(30):
        try:
            pool.open(wait=True, timeout=10)
            with pool.connection() as conn:
                # Only one pod at a time may run the schema (advisory lock)
                conn.execute("SELECT pg_advisory_xact_lock(20261011)")
                conn.execute(Path(__file__).with_name("schema.sql").read_text())
            print(f"[{POD_NAME}] database ready")
            return
        except Exception as err:  # database not up yet
            print(f"[{POD_NAME}] waiting for database ({attempt + 1}/30): {err}")
            time.sleep(3)
    raise SystemExit("Could not connect to the database")


def stop():
    pool.close()
