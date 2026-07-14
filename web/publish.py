"""Publish `web/serve.db` — the Viewer's read-optimized serving store (ADR 0023).

A zero-API DB→DB copy: open the authoritative `data/football.db` read-only and copy a
small, schema-identical slice into a fresh `serve.db` — everything the Viewer renders
and nothing else. It writes `serve.db.tmp` and then **atomically renames** it over
`serve.db`, so a reader never sees a half-written store (the blue-green swap of ADR
0023). This is distinct from `football.scope` (which re-parses a Competition from the
*raw cache*, ADR 0011): publish never re-parses and never hits the API — it clones from
the already-built authoritative store, exactly "cloned, not pulled".

Contents (schema-identical to `football.db`, via `football.models`):
  - full clone: competition, team, venue, player, playerteam, teamprofile — the *small*
    dimension tables (the 388 MB of football.db is almost all squadentry/event history),
    so cloning them whole is cheap and makes any player/competition renderable;
  - windowed:   fixture (date in [now-BEFORE, now+AFTER)) plus the event rows for exactly
    those fixtures. squadentry / teammatchstat are intentionally NOT copied — the Viewer's
    per-match view is headline + event timeline only (ADR 0022/0023).

Run:
    uv run python -m web.publish                    # default window: -3 .. +10 days
    uv run python -m web.publish --before 3 --after 10
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import SQLModel, create_engine

from football import config, models  # noqa: F401 — importing models registers the schema

SERVE_DIR = Path(__file__).resolve().parent
SERVE_DB = SERVE_DIR / "serve.db"

DEFAULT_BEFORE = 3   # days of recent (already-played) fixtures to include
DEFAULT_AFTER = 10   # days of upcoming fixtures to include

# Copied in full — the small dimension tables. fixture/event are windowed below;
# squadentry/teammatchstat are deliberately omitted (not rendered by the Viewer).
FULL_TABLES = ["competition", "team", "venue", "player", "playerteam", "teamprofile"]


def _cols(con: sqlite3.Connection, table: str) -> str:
    """Comma-joined column names of a destination table, so INSERT…SELECT is robust
    to any future column-order divergence between the two stores."""
    return ", ".join(r[1] for r in con.execute(f"PRAGMA table_info({table})"))


def publish(before: int = DEFAULT_BEFORE, after: int = DEFAULT_AFTER,
            db_path: Path | None = None, serve_db: Path = SERVE_DB) -> tuple[dict, tuple[str, str]]:
    """Rebuild serve_db from db_path (football.db). Returns (row counts, (start, end))."""
    src_path = Path(db_path) if db_path else config.DB_PATH
    if not src_path.exists():
        raise FileNotFoundError(
            f"Source store {src_path} not found — build football.db first (Console → Build)."
        )

    # Guard: don't read football.db while parse.build() is mid-rebuild — it holds an
    # exclusive flock on the .build.lock. A shared, non-blocking lock fails fast if so,
    # rather than publishing a half-rebuilt store.
    lock_f = open(src_path.with_suffix(".build.lock"), "a")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        lock_f.close()
        raise SystemExit(
            "A football.db build is in progress (build lock held) — re-run publish "
            "once it finishes."
        )

    try:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=before)).strftime("%Y-%m-%d %H:%M:%S")
        end = (now + timedelta(days=after)).strftime("%Y-%m-%d %H:%M:%S")

        tmp = serve_db.with_name(serve_db.name + ".tmp")
        if tmp.exists():
            tmp.unlink()

        # A fresh, schema-identical store from the same models parse.py builds football.db with.
        engine = create_engine(f"sqlite:///{tmp}")
        SQLModel.metadata.create_all(engine)
        engine.dispose()

        counts: dict[str, int] = {}
        con = sqlite3.connect(tmp)
        try:
            con.execute("ATTACH DATABASE ? AS src", (str(src_path),))
            for t in FULL_TABLES:
                cols = _cols(con, t)
                cur = con.execute(f"INSERT INTO {t} ({cols}) SELECT {cols} FROM src.{t}")
                counts[t] = cur.rowcount
            fcols = _cols(con, "fixture")
            cur = con.execute(
                f"INSERT INTO fixture ({fcols}) SELECT {fcols} FROM src.fixture "
                "WHERE date >= ? AND date < ?",
                (start, end),
            )
            counts["fixture"] = cur.rowcount
            # events for exactly the windowed fixtures (unqualified `fixture` = the dest
            # table we just populated with the window).
            ecols = _cols(con, "event")
            cur = con.execute(
                f"INSERT INTO event ({ecols}) SELECT {ecols} FROM src.event "
                "WHERE fixture_id IN (SELECT id FROM fixture)"
            )
            counts["event"] = cur.rowcount
            con.commit()
            con.execute("DETACH DATABASE src")
        finally:
            con.close()

        os.replace(tmp, serve_db)   # atomic swap — readers never see a partial store
        return counts, (start, end)
    finally:
        lock_f.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m web.publish",
        description="Publish web/serve.db from data/football.db (ADR 0023).",
    )
    ap.add_argument("--before", type=int, default=DEFAULT_BEFORE,
                    help=f"days of recent fixtures to include (default {DEFAULT_BEFORE})")
    ap.add_argument("--after", type=int, default=DEFAULT_AFTER,
                    help=f"days of upcoming fixtures to include (default {DEFAULT_AFTER})")
    args = ap.parse_args(argv)

    t0 = datetime.now(timezone.utc)
    counts, (start, end) = publish(args.before, args.after)
    dt_s = (datetime.now(timezone.utc) - t0).total_seconds()

    print(f"Published {SERVE_DB}")
    print(f"  window (UTC): {start}  ..  {end}")
    for t, n in counts.items():
        print(f"  {t:<12} {n:>8,}")
    print(f"  done in {dt_s:.1f}s")


if __name__ == "__main__":
    main()
