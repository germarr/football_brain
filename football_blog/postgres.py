"""Process-wide Postgres singleton. psycopg 3, autocommit read-only.

Connects via **`FOOTBALL_DATABASE_URL`**, never the bare `PG*` vars — the same
rule `football.publish_pg` follows, and for the same reason (see
`football.config.load_pg_url`): this project's `.env` defines `PGHOST/PGPORT/
PGUSER/PGPASSWORD/PGDATABASE` **twice**, once for the YouTube store
(`content_analysis`) and once for the Published Store (`football_prod`). The
football block is deliberately last, so shell tools — which take last-wins —
resolve to `football_prod`.

`config._load_project_env` is **first**-wins (a real environment variable must
beat the file), so reading the bare `PG*` vars from here resolved them to
`content_analysis`, and every query in `loader.py` failed with
`relation "narrated_match" does not exist`. Reading one unambiguous,
purpose-named variable removes the ordering question entirely.

The `_assert_published_store` check on first connect is what makes that
non-regressable: pointed at the wrong database it fails immediately, naming the
database it actually reached, instead of surfacing eight queries later.
"""
from typing import Optional

import psycopg

from football.config import load_pg_url

_conn: Optional[psycopg.Connection] = None


def _assert_published_store(conn: psycopg.Connection) -> None:
    """Refuse a connection that is not the Published Store (ADR 0027).

    `fixture` is the table every loader query starts from; if it is absent we are
    not in `football_prod` and no amount of retrying will help.
    """
    if conn.execute("SELECT to_regclass('public.fixture')").fetchone()[0]:
        return
    dbname, user = conn.execute("SELECT current_database(), current_user").fetchone()
    raise RuntimeError(
        f"Connected to database {dbname!r} as {user!r}, but it has no `public.fixture` "
        f"table — this is not the Published Store.\n"
        f"    Check FOOTBALL_DATABASE_URL in the project-root .env: it must point at "
        f"the football database, not the YouTube one.\n"
        f"    If the store has never been published, run: "
        f"uv run python -m football.publish_pg"
    )


def get_conn() -> psycopg.Connection:
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    _conn = psycopg.connect(load_pg_url(), connect_timeout=30, autocommit=True)
    try:
        _assert_published_store(_conn)
    except Exception:
        _conn.close()
        _conn = None
        raise
    return _conn


def close_conn() -> None:
    global _conn
    if _conn is not None and not _conn.closed:
        _conn.close()
    _conn = None
