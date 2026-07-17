"""Publish the **Published Store** — a Postgres replica of selected Competitions plus the
whole ESPN commentary store (ADR 0027).

A zero-API, re-runnable rebuild: re-parse the chosen Competitions out of the shared raw
cache into one throwaway SQLite store, clone `data/commentary.db` alongside it, load both
into Postgres and **swap them over `public` in a single transaction**. Re-running makes
Postgres match what we hold locally; it is a *replace*, never a merge (see below).

The two halves are scoped differently, on purpose:
  - the **football** tables hold EXACTLY the Competitions asked for — the selection defines
    the store, so an omitted Competition is removed;
  - the **commentary** tables (`narrated_match`, `commentary_line`) are copied in FULL,
    unscoped, because a Narrated Match is ESPN's unit and is not Competition-scoped at all
    (CONTEXT.md): most have no Fixture, and those that do may name one outside the published
    subset. Publishing only "the commentary for these Competitions" would drop the majority
    of the store's own subject matter.
The consequence is a real, documented property of this store: `narrated_match.fixture_id`
may be non-null and still resolve to nothing, because that Fixture's Competition was not
published. The bridge is optional by design and is never a foreign key.

Why one build over ALL the competitions rather than concatenating the existing scoped
`data/liga-mx.db` + `data/major-league-soccer.db` (ADR 0011): `Venue.id` is a **surrogate**,
enumerated 1..N in sorted (name, city) order *per build* (parse._build_venues). Two
independently-scoped stores therefore both number their venues from 1 — every id collides
with a different stadium. Building both Competitions in one pass enumerates venues across
the union, so the ids are globally consistent by construction.

Why replace and not upsert: two keys here are surrogates re-derived on every build —
`Venue.id` (above) and `Event.event_index` (a row's position in the fixture's response
array, stable only while the raw cache is). An ON CONFLICT DO UPDATE would rewrite their
meanings under rows still pointing at the old numbering, and would never delete rows the
provider has retracted. Wholesale replacement is the only sound refresh.

Run:
    uv run python -m football.publish_pg                # Liga MX + MLS (the default pair)
    uv run python -m football.publish_pg 262 253 71     # ... plus Brasileirao
    uv run python -m football.publish_pg --all          # every tracked Competition
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import psycopg
from sqlmodel import SQLModel, create_engine

from commentary.store import DB_PATH as COMMENTARY_DB
from commentary.store import SCHEMA as COMMENTARY_SCHEMA

from . import config, parse, scope
from . import models  # noqa: F401 — importing models registers the schema

# The Competitions a bare run publishes: Liga MX (262) + Major League Soccer (253).
DEFAULT_LEAGUE_IDS = [262, 253]

# The staging schema the load lands in before the swap. Dropped on success and on a
# re-run (a crashed run leaves it behind; it is never read, only rebuilt).
STAGING = "football_next"

# Load order matters: every table lands after the ones its foreign keys point at.
# teamprofile/playerteam carry provider team ids that are deliberately NOT foreign keys
# (CONTEXT.md: Career Stint / Team Profile), so they only depend on `player`.
FOOTBALL_TABLES = [
    "competition", "team", "venue", "player",      # dimensions, no inbound FKs
    "teamprofile", "playerteam",                   # career directory (FK: player)
    "fixture",                                     # FK: competition, team, venue
    "squadentry", "event", "teammatchstat",        # FK: fixture, team, player
]

# The ESPN commentary store (ADR 0026), cloned from data/commentary.db. Ordered
# parent-first: commentary_line REFERENCES narrated_match(game_id).
COMMENTARY_TABLES = ["narrated_match", "commentary_line"]


def _redact(url: str) -> str:
    """A URL safe to print — the password replaced with ***."""
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:***@", url)


def _competitions(league_ids: list[int] | None, use_all: bool) -> list[dict]:
    """The Competition configs to publish, or exit with guidance for an unknown id."""
    if use_all:
        return list(config.COMPETITIONS)
    return [scope._competition(lid) for lid in (league_ids or DEFAULT_LEAGUE_IDS)]


def _targets(comps: list[dict]) -> list[tuple[int, str, int]]:
    """(league_id, name, season) for every cached season across ALL the Competitions.

    Reuses football.scope's per-Competition cache probe, so a partially collected
    Competition publishes the seasons it does have and reports the rest — the same
    behaviour a scoped build has (ADR 0011).
    """
    targets: list[tuple[int, str, int]] = []
    for comp in comps:
        print(f"  probing {comp['name']} (id {comp['league_id']}) …")
        targets.extend(scope._cached_targets(comp))
    return targets


def _copy_table(sq: sqlite3.Connection, pg: psycopg.Connection, table: str) -> int:
    """Stream one table SQLite -> Postgres staging via COPY. Returns the row count.

    Column names are read from the SQLite table, so the COPY is robust to any column-order
    divergence between the two stores (the same reason web.publish reads PRAGMA table_info).
    """
    cols = [r[1] for r in sq.execute(f"PRAGMA table_info({table})")]
    collist = ", ".join(f'"{c}"' for c in cols)
    n = 0
    with pg.cursor().copy(f'COPY {STAGING}.{table} ({collist}) FROM STDIN') as cp:
        for row in sq.execute(f"SELECT {collist} FROM {table}"):
            cp.write_row(row)
            n += 1
    return n


def _load(sqlite_path: Path, url: str, commentary_path: Path | None) -> dict[str, int]:
    """Create the staging schema, COPY every table into it, then swap it over public.

    Two sources, because the Published Store's two halves are produced by different
    pipelines: the football tables come from the freshly re-parsed `sqlite_path`, and the
    commentary tables are cloned from `data/commentary.db` (an ESPN store built by
    `commentary.ingest`, not derivable from the API-Football raw cache). Both land in the
    one staging schema and ride the one swap, so the whole store turns over atomically.
    """
    counts: dict[str, int] = {}
    tables = list(FOOTBALL_TABLES)

    # 1) A fresh staging schema. The football tables' DDL is generated from
    #    football.models (the same definitions parse.build uses); the commentary tables'
    #    from commentary.store.SCHEMA — its SQLite DDL is valid Postgres verbatim, so it
    #    is reused as-is rather than transcribed. That also preserves, for free, the rule
    #    that narrated_match.fixture_id carries no REFERENCES: it is a *bridge, never a
    #    key* (CONTEXT.md), and in this store it frequently dangles (see _commentary_note).
    with psycopg.connect(url, connect_timeout=30, autocommit=True) as pg:
        pg.execute(f"DROP SCHEMA IF EXISTS {STAGING} CASCADE")
        pg.execute(f"CREATE SCHEMA {STAGING}")
        if commentary_path:
            pg.execute(f"SET search_path TO {STAGING}")
            pg.execute(COMMENTARY_SCHEMA)
            tables += COMMENTARY_TABLES

    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        connect_args={"options": f"-csearch_path={STAGING}"},
    )
    SQLModel.metadata.create_all(engine)   # unqualified CREATEs land in STAGING
    engine.dispose()

    # 2) Load. One transaction for the whole load + swap: a crash at any point rolls
    #    back to the previous Published Store rather than leaving a partial one.
    sq = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    cm = sqlite3.connect(f"file:{commentary_path}?mode=ro", uri=True) if commentary_path else None
    try:
        with psycopg.connect(url, connect_timeout=30) as pg:
            for t in tables:
                src = cm if t in COMMENTARY_TABLES else sq
                t0 = time.monotonic()
                counts[t] = _copy_table(src, pg, t)
                print(f"  {t:<14} {counts[t]:>9,}  ({time.monotonic() - t0:.1f}s)")

            # 3) The blue-green swap (ADR 0027). Postgres DDL is transactional, so a reader
            #    sees the OLD store right up to COMMIT and the new one after — never a
            #    half-loaded table. We move the tables INTO public rather than renaming the
            #    public schema itself: public is owned by azure_pg_admin and carries the
            #    default GRANTs to role PUBLIC, which promoting a staging schema would lose.
            for t in reversed(tables):        # children first — FKs into public tables
                pg.execute(f"DROP TABLE IF EXISTS public.{t} CASCADE")
            for t in tables:
                pg.execute(f"ALTER TABLE {STAGING}.{t} SET SCHEMA public")
            pg.execute(f"DROP SCHEMA {STAGING} CASCADE")
            pg.commit()
    finally:
        sq.close()
        if cm:
            cm.close()
    return counts


def _commentary_note(commentary_path: Path | None) -> None:
    """Announce what the commentary half will contribute, before the load."""
    if not commentary_path:
        print("  ⚠ data/commentary.db not found — publishing football tables only.\n"
              "    Any narrated_match/commentary_line already in Postgres is left untouched.")
        return
    con = sqlite3.connect(f"file:{commentary_path}?mode=ro", uri=True)
    try:
        matches = con.execute("select count(*) from narrated_match").fetchone()[0]
        lines = con.execute("select count(*) from commentary_line").fetchone()[0]
    finally:
        con.close()
    print(f"  + commentary: {matches} Narrated Match(es), {lines:,} Commentary Line(s) "
          f"— every row, unscoped by Competition")


def _report_bridges(url: str) -> None:
    """Report how the Narrated Match -> Fixture bridges landed in the published store.

    Measured against Postgres after the swap, which is the only honest place: whether a
    bridge resolves depends on which Competitions were just published. Three outcomes, all
    expected (CONTEXT.md: the Fixture id is a *bridge, never the key*):
      - no link at all  — ESPN narrates competitions we do not collect; the normal case;
      - linked, resolves — the Narrated Match's Fixture is in the published subset;
      - linked, dangles — the Fixture exists in football.db but its Competition was not
        published. The row is still real and wanted; the join just returns nothing there.
    A dangling bridge is silent in SQL, so it is printed rather than left to be discovered.
    """
    with psycopg.connect(url, connect_timeout=30) as pg:
        if not pg.execute("select to_regclass('public.narrated_match')").fetchone()[0]:
            return
        unlinked, resolves, dangles = pg.execute("""
            select count(*) filter (where m.fixture_id is null),
                   count(*) filter (where m.fixture_id is not null and f.id is not null),
                   count(*) filter (where m.fixture_id is not null and f.id is null)
            from narrated_match m left join fixture f on f.id = m.fixture_id
        """).fetchone()
    print(f"  Narrated Match → Fixture bridges: {resolves} resolve, {dangles} dangle "
          f"(Fixture not in the published Competitions), {unlinked} unlinked")
    if dangles:
        print("    ↳ expected: the bridge is optional and never a foreign key — those "
              "narrations are still fully present, with their Commentary Lines.")


def publish(league_ids: list[int] | None = None, use_all: bool = False) -> dict[str, int]:
    """Rebuild the Published Store from the raw cache. Returns per-table row counts."""
    url = config.load_pg_url()
    comps = _competitions(league_ids, use_all)
    names = ", ".join(c["name"] for c in comps)
    commentary_path = COMMENTARY_DB if COMMENTARY_DB.exists() else None

    print(f"Publishing {len(comps)} Competition(s) -> {_redact(url)}")
    print(f"  {names}")
    _commentary_note(commentary_path)
    print()

    targets = _targets(comps)
    if not targets:
        raise SystemExit(
            "No cached fixtures for any requested Competition. Collect one first with:\n"
            f"    uv run python -m football.orchestrate {comps[0]['league_id']}"
        )
    print(f"\n  {len(targets)} (competition, season) target(s) in cache\n")

    # A throwaway SQLite store: the Published Store's contents are defined by this one
    # multi-Competition build (venue ids enumerated across the union), and nothing reads
    # the file afterwards. Its own build lock lives in the temp dir, so this never blocks
    # — nor is blocked by — a data/football.db build (ADR 0011's per-db_path lock).
    with tempfile.TemporaryDirectory(prefix="football-pg-") as tmpdir:
        staged = Path(tmpdir) / "staged.db"
        print("▶ Re-parsing from the raw cache (zero API) …")
        parse.build(db_path=staged, targets=targets)
        size_mb = staged.stat().st_size / 1e6
        print(f"\n▶ Loading {size_mb:.0f} MB into Postgres …")
        counts = _load(staged, url, commentary_path)

    _report_bridges(url)
    return counts


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.publish_pg",
        description="Publish selected Competitions to the Postgres Published Store (ADR 0027).",
    )
    ap.add_argument("league_ids", type=int, nargs="*",
                    help=f"provider league ids (default: {' '.join(map(str, DEFAULT_LEAGUE_IDS))} "
                         "— Liga MX, Major League Soccer)")
    ap.add_argument("--all", action="store_true",
                    help="publish every tracked Competition instead")
    args = ap.parse_args(argv)
    if args.league_ids and args.all:
        raise SystemExit("Pass league ids or --all, not both.")

    t0 = time.monotonic()
    counts = publish(args.league_ids, args.all)
    print(f"\nPublished Store updated — {sum(counts.values()):,} rows "
          f"in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()
