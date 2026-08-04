"""`data/commentary.db` — the Commentary Line store (ADR 0026).

A standalone SQLite database, written with the stdlib driver and nothing else.
It deliberately does not import `football.models` or `sqlmodel`: this store shares
no schema with `football.db` and must not grow a dependency on it. Everything
outside this package is reached only by `fixture_link.py`, and only to *verify* an
operator-supplied `--fixture-id` — read-only, never opened without one. Two sources
answer that check now (`football.db` and the Published Store, ADR 0029); the
invariant is the read-only, verification-only *kind* of the contact, not its count.

**This store is the system of record and is NOT disposable.** That breaks the
invariant of ADR 0002, knowingly. Every other store here rebuilds from the raw
cache because its transform is free and deterministic — pure parsing. Half of
this one's content is not: a Commentary Line's Category is either *asserted* by
ESPN (reproducible) or *inferred* by a language model (costs money, and re-running
will not reproduce it exactly). Dropping this file loses those labels for good;
`.gitignore` says so out loud.

Two consequences follow directly, and both are load-bearing:

1. **Ingest skips a match it already has.** With no classify cache, the DB *is*
   the cache. A re-run costs nothing and cannot clobber labels by accident, which
   is what makes "refused because live — re-run it later" a practical workflow: a
   whole sweep can be re-run freely and only new matches cost anything.
   Overwriting requires `--reclassify` and is explicit.
2. **Schema changes migrate; they cannot drop-and-rebuild.** There is no
   `DROP TABLE and re-ingest` here, because re-ingesting means re-paying and
   re-labelling.

`field_position` is stored as **opaque JSON text** — `{"x":..,"y":..,"goal_y":..}`
or null — not as numeric columns. That is a safeguard, not laziness: coordinates
exist only on scoring lines and cover just 56% of even those, non-uniformly, so
numeric columns would invite a goal map silently missing two goals in five. As
JSON they are preserved but inert; unpacking them is a deliberate act. See ADR 0026.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "commentary.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS narrated_match (
    game_id            TEXT PRIMARY KEY,
    fixture_id         INTEGER,
    league             TEXT,
    date               TEXT,
    venue              TEXT,
    status             TEXT,
    status_name        TEXT,
    narration_coverage TEXT NOT NULL
        CHECK (narration_coverage IN ('narrative', 'events_only')),
    home_team          TEXT,
    home_team_id       TEXT,
    home_score         TEXT,
    away_team          TEXT,
    away_team_id       TEXT,
    away_score         TEXT,
    model              TEXT,
    source_url         TEXT,
    ingested_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commentary_line (
    game_id        TEXT NOT NULL REFERENCES narrated_match(game_id) ON DELETE CASCADE,
    sequence       INTEGER NOT NULL,
    minute         TEXT,
    clock_seconds  REAL,
    team           TEXT,
    category       TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN ('espn_keyevent', 'llm')),
    text           TEXT NOT NULL,
    field_position TEXT,
    PRIMARY KEY (game_id, sequence)
);

CREATE INDEX IF NOT EXISTS ix_line_category ON commentary_line(category);
CREATE INDEX IF NOT EXISTS ix_line_team ON commentary_line(team);
CREATE INDEX IF NOT EXISTS ix_match_fixture ON narrated_match(fixture_id);
CREATE INDEX IF NOT EXISTS ix_match_coverage ON narrated_match(narration_coverage);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the store, with the schema applied."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


def has_match(connection: sqlite3.Connection, game_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM narrated_match WHERE game_id = ?", (str(game_id),)
        ).fetchone()
        is not None
    )


def delete_match(connection: sqlite3.Connection, game_id: str) -> int:
    """Remove a match and its lines. Only `--reclassify` should reach this."""
    lines = connection.execute(
        "DELETE FROM commentary_line WHERE game_id = ?", (str(game_id),)
    ).rowcount
    connection.execute("DELETE FROM narrated_match WHERE game_id = ?", (str(game_id),))
    return lines


def write_match(
    connection: sqlite3.Connection,
    doc: dict,
    *,
    narration_coverage: str,
    fixture_id: int | None = None,
    source_url: str | None = None,
) -> int:
    """Insert one Narrated Match and its Commentary Lines. Returns lines written.

    Assumes the caller has already established the match is absent (or deleted it
    via `--reclassify`) — this store never silently overwrites labels.
    """
    with connection:  # one transaction: a match and its lines land together
        connection.execute(
            """
            INSERT INTO narrated_match (
                game_id, fixture_id, league, date, venue, status, status_name,
                narration_coverage, home_team, home_team_id, home_score,
                away_team, away_team_id, away_score, model, source_url, ingested_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(doc["game_id"]),
                fixture_id,
                doc.get("league"),
                doc.get("date"),
                doc.get("venue"),
                doc.get("status"),
                doc.get("status_name"),
                narration_coverage,
                doc["home"].get("team"),
                doc["home"].get("team_id"),
                doc["home"].get("score"),
                doc["away"].get("team"),
                doc["away"].get("team_id"),
                doc["away"].get("score"),
                doc.get("model"),
                source_url,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO commentary_line (
                game_id, sequence, minute, clock_seconds, team,
                category, source, text, field_position
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    str(doc["game_id"]),
                    e["sequence"],
                    e["minute"],
                    e["clock_seconds"],
                    e["team"],
                    e["category"],
                    e["source"],
                    e["text"],
                    json.dumps(e["field_position"], ensure_ascii=False)
                    if e.get("field_position")
                    else None,
                )
                for e in doc["events"]
            ],
        )
    return len(doc["events"])
