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
    uv run python -m football.publish.pg                # Liga MX + MLS (the default pair)
    uv run python -m football.publish.pg 262 253 71     # ... plus Brasileirao
    uv run python -m football.publish.pg --all          # every tracked Competition
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import psycopg
from sqlmodel import SQLModel, create_engine

from commentary.store import DB_PATH as COMMENTARY_DB
from commentary.store import SCHEMA as COMMENTARY_SCHEMA

from .. import config, fetch
from ..build import parse, scope, venues
from .. import models  # noqa: F401 — importing models registers the schema
from ..client import CachedClient
from ..status import FINAL

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

    Reuses football.build.scope's per-Competition cache probe, so a partially collected
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


# --- Delta publish (ADR 0028) ----------------------------------------------
# The frequent intraday path: apply only the Finals whose data changed since we last
# published them, against the live `public` tables in one transaction — instead of
# re-parsing the whole history and swapping it wholesale. The wholesale `publish()` above
# stays the manual reset (first migration, venue-registry loss, dropping retracted rows).

# The per-fixture watermark: which Fixtures are in the Published Store, and the ledger
# `collected` date they were published at. Kept OUT of FOOTBALL_TABLES so the ten published
# tables stay schema-identical to football.db (ADR 0027); it is publish metadata, not data.
STAMP_TABLE = "publish_fixture"

# What a delta touches. Fact tables are delete+reinserted per changed Fixture (Event.event_index
# is a per-fixture surrogate, stable within the fixture we replace). Core dimensions are upserted
# by primary key — sound now that Venue.id is registry-stable (ADR 0028), like team/player/
# competition provider ids always were. The career directory (teamprofile/playerteam) is
# deliberately NOT here: it is FK-referenced by nothing (PlayerTeam.team_id is not a key), it is
# lag-tolerant by design (CONTEXT.md), and it is ~10x a delta's row volume — so it is refreshed
# only by the wholesale publish.
DELTA_FACTS = ["fixture", "event", "squadentry", "teammatchstat"]
DELTA_DIMENSIONS = [("competition", "id"), ("team", "id"), ("player", "id"), ("venue", "id")]


def _ledger_collected() -> dict[int, str]:
    """`fixture_id -> last-collected date`, from the Refresh ledger (ADR 0018).

    A Fixture is in the ledger exactly once it is Final and its per-fixture data is collected,
    and its date advances on a Coverage re-heal — so this is the authoritative "this Fixture's
    data changed on this date" signal the delta keys on.
    """
    from refresh.core import LEDGER_FILE
    if not LEDGER_FILE.exists():
        return {}
    data = json.loads(LEDGER_FILE.read_text())
    return {int(fid): e["collected"] for fid, e in data.items() if e.get("collected")}


def _current_season_targets(comps: list[dict]) -> list[tuple[int, str, int]]:
    """(league_id, name, season) for each Competition's current (latest cached) Season only.

    The delta parses just the frontier: past Seasons are immutable (ADR 0018) and already in
    Postgres, so re-parsing them is waste. Coverage heals live inside the 14-day probe window,
    i.e. always within the current Season, so restricting here still catches them. Both
    Tournaments of a split Season (Apertura + Clausura share one year) come in together.
    """
    targets: list[tuple[int, str, int]] = []
    for comp in comps:
        cached = scope._cached_targets(comp)
        if not cached:
            continue
        current = max(season for _l, _n, season in cached)
        targets += [t for t in cached if t[2] == current]
    return targets


def _ensure_stamp_table(pg: psycopg.Connection) -> None:
    pg.execute(f"CREATE TABLE IF NOT EXISTS public.{STAMP_TABLE} "
               "(fixture_id bigint PRIMARY KEY, collected text NOT NULL)")


def _sq_cols(sq: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in sq.execute(f"PRAGMA table_info({table})")]


#: Serial number for staging tables, so two stagings of the same table never collide.
#: `_apply_delta` is one transaction and its temp tables are `ON COMMIT DROP`, so nothing
#: is dropped until that transaction commits — and it stages `fixture` twice, once for the
#: changed Finals and again inside `_replace_scheduled`. A name derived from the table alone
#: made the second `CREATE TEMP TABLE` collide with the first (DuplicateTable). Latent for as
#: long as it was: it needs a non-empty `changed` set, since that is what creates the first
#: `_stg_fixture`, so a night with no new Final never reached it.
_STG_SEQ = itertools.count()


def _stage(sq: sqlite3.Connection, pg: psycopg.Connection, table: str,
           where_col: str | None = None, where_vals: list[int] | None = None) -> tuple[str, list[str], int]:
    """COPY rows of a temp-SQLite `table` (optionally filtered to `where_col IN where_vals`)
    into a fresh Postgres TEMP table shaped like `public.<table>`, dropped at COMMIT. Returns
    (temp_name, columns, rows_staged). Column names come from SQLite, robust to column-order
    divergence (the same reason _copy_table and web.publish read PRAGMA table_info).

    The temp name carries a serial (see `_STG_SEQ`) rather than being `_stg_<table>`: callers
    only ever use the name this returns, so uniqueness costs nothing and staging the same
    table twice in one transaction is legal. Reusing the name via `DROP TABLE IF EXISTS`
    would also run, but it would silently inherit a table staged for another purpose."""
    cols = _sq_cols(sq, table)
    collist = ", ".join(f'"{c}"' for c in cols)
    stg = f"_stg_{table}_{next(_STG_SEQ)}"
    pg.execute(f"CREATE TEMP TABLE {stg} (LIKE public.{table}) ON COMMIT DROP")
    sql, params = f"SELECT {collist} FROM {table}", ()
    if where_col is not None:
        if not where_vals:
            return stg, cols, 0
        sql += f" WHERE {where_col} IN ({','.join('?' for _ in where_vals)})"
        params = tuple(where_vals)
    n = 0
    with pg.cursor().copy(f'COPY {stg} ({collist}) FROM STDIN') as cp:
        for row in sq.execute(sql, params):
            cp.write_row(row)
            n += 1
    return stg, cols, n


def _upsert_dimension(sq: sqlite3.Connection, pg: psycopg.Connection, table: str, pk: str) -> int:
    """Upsert every row of a temp dimension table into public, ON CONFLICT(pk) DO UPDATE."""
    stg, cols, n = _stage(sq, pg, table)
    if n:
        collist = ", ".join(f'"{c}"' for c in cols)
        updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != pk)
        pg.execute(f'INSERT INTO public.{table} ({collist}) SELECT {collist} FROM {stg} '
                   f'ON CONFLICT ("{pk}") DO UPDATE SET {updates}')
    return n


def _insert_changed(sq: sqlite3.Connection, pg: psycopg.Connection, table: str,
                    key_col: str, ids: list[int], *, on_conflict: str = "") -> int:
    """Insert the changed fixtures' rows of a fact table (already deleted from public)."""
    stg, cols, n = _stage(sq, pg, table, key_col, ids)
    if n:
        collist = ", ".join(f'"{c}"' for c in cols)
        pg.execute(f'INSERT INTO public.{table} ({collist}) SELECT {collist} FROM {stg} '
                   f'{on_conflict}')
    return n


def _replace_scheduled(sq: sqlite3.Connection, pg: psycopg.Connection) -> int:
    """Replace the current Season's **non-Final** Fixtures wholesale (ADR 0028 amendment).

    The ledger-keyed delta above knows only Finals, so a *scheduled* Fixture was never
    inserted, re-dated or removed — its rows in the Published Store dated from whenever a
    wholesale `publish()` was last run by hand, and nothing in `scripts/nightly.sh` runs
    one. **Match Previews** read upcoming Fixtures from this store (ADR 0040), and a
    knockout round drawn after a group stage ends is a set of brand-new non-Final rows
    that would never have arrived.

    Delete-then-reinsert rather than upsert, because one statement then covers re-dating,
    cancellation, provider retraction *and* newly-drawn Fixtures alike, and it is
    idempotent. Two things bound the blast radius:

      - **Scope is the current Season only** — the (league, season) pairs actually present
        in the staging parse. Historical non-Final rows (a Leagues Cup game cancelled in
        2022, say) have no fresh data behind them and are left exactly as they are.
      - **A Final is never deleted.** The `NOT (status = ANY(FINAL))` filter is applied to
        what *Postgres* holds, not to what staging holds, so a row this store considers
        Final survives even if the staging parse disagrees; the reinsert then skips it via
        ON CONFLICT rather than failing. That keeps ADR 0028's real invariant intact.

    Child rows are deleted with the Fixtures but never reinserted: a non-Final Fixture has
    no per-match data to collect (CONTEXT.md), so staging carries none for it either.
    """
    scope_pairs = sq.execute("SELECT DISTINCT league_id, season FROM fixture").fetchall()
    doomed: list[int] = []
    for lid, season in scope_pairs:
        doomed += [r[0] for r in pg.execute(
            "SELECT id FROM public.fixture WHERE league_id = %s AND season = %s "
            "AND NOT (status = ANY(%s))", (lid, season, list(FINAL))).fetchall()]
    if doomed:
        for t in ("teammatchstat", "squadentry", "event"):
            pg.execute(f"DELETE FROM public.{t} WHERE fixture_id = ANY(%s)", (doomed,))
        pg.execute("DELETE FROM public.fixture WHERE id = ANY(%s)", (doomed,))

    placeholders = ",".join("?" for _ in FINAL)
    fresh = [r[0] for r in sq.execute(
        f"SELECT id FROM fixture WHERE status NOT IN ({placeholders})", FINAL)]
    return _insert_changed(sq, pg, "fixture", "id", fresh,
                           on_conflict="ON CONFLICT (id) DO NOTHING")


def _copy_commentary_full(pg: psycopg.Connection, commentary_path: Path) -> dict[str, int]:
    """Replace the commentary half wholesale, inside the delta transaction — it has no
    per-fixture watermark and is tiny (~200 KB), so it is always copied whole and unscoped,
    the same invariant the wholesale publish holds (ADR 0027)."""
    counts: dict[str, int] = {}
    cm = sqlite3.connect(f"file:{commentary_path}?mode=ro", uri=True)
    try:
        for t in reversed(COMMENTARY_TABLES):       # children first (FK order)
            pg.execute(f"DELETE FROM public.{t}")
        for t in COMMENTARY_TABLES:                 # parents first
            cols = _sq_cols(cm, t)
            collist = ", ".join(f'"{c}"' for c in cols)
            n = 0
            with pg.cursor().copy(f'COPY public.{t} ({collist}) FROM STDIN') as cp:
                for row in cm.execute(f"SELECT {collist} FROM {t}"):
                    cp.write_row(row)
                    n += 1
            counts[t] = n
    finally:
        cm.close()
    return counts


def _rebuild_stamps(url: str) -> None:
    """Baseline the delta watermark after a wholesale publish (ADR 0028): stamp every
    published Fixture the ledger knows as Final with its collected date, so the next delta
    treats them as already-published and applies only what changes afterwards."""
    ledger = _ledger_collected()
    with psycopg.connect(url, connect_timeout=30) as pg:
        pg.execute(f"DROP TABLE IF EXISTS public.{STAMP_TABLE}")
        _ensure_stamp_table(pg)
        ids = [r[0] for r in pg.execute("SELECT id FROM public.fixture")]
        stamps = [(fid, ledger[fid]) for fid in ids if fid in ledger]
        if stamps:
            with pg.cursor().copy(f"COPY public.{STAMP_TABLE} (fixture_id, collected) FROM STDIN") as cp:
                for row in stamps:
                    cp.write_row(row)
        pg.commit()
    print(f"  stamped {len(stamps):,} published Final(s) into {STAMP_TABLE} (delta baseline)")


def delta_publish(league_ids: list[int] | None = None, use_all: bool = False) -> dict[str, int]:
    """Apply only the new/re-healed Finals to the Published Store (ADR 0028). Additive: a
    provider-retracted Final is removed only by a wholesale `publish()` (an accepted blind
    spot). Requires a prior wholesale publish to baseline the store."""
    url = config.load_pg_url()
    comps = _competitions(league_ids, use_all)
    commentary_path = COMMENTARY_DB if COMMENTARY_DB.exists() else None
    print(f"Delta publish {len(comps)} Competition(s) -> {_redact(url)}")
    print(f"  {', '.join(c['name'] for c in comps)}")

    targets = _current_season_targets(comps)
    if not targets:
        raise SystemExit("No cached current Season for any requested Competition — nothing to delta.")
    ledger = _ledger_collected()

    with tempfile.TemporaryDirectory(prefix="football-pg-delta-") as tmpdir:
        staged = Path(tmpdir) / "delta.db"
        print(f"  parsing current Season(s) {sorted({s for _l, _n, s in targets})} from cache (read-only registry) …")
        parse.build(db_path=staged, targets=targets, register=False)
        sq = sqlite3.connect(f"file:{staged}?mode=ro", uri=True)
        try:
            candidates = [r[0] for r in sq.execute("SELECT id FROM fixture")]
            with psycopg.connect(url, connect_timeout=30) as pg:
                if not pg.execute("SELECT to_regclass('public.fixture')").fetchone()[0]:
                    raise SystemExit("No Published Store yet — run a full publish first:\n"
                                     "    uv run python -m football.publish.pg")
                _ensure_stamp_table(pg)
                stamps = {fid: col for fid, col in
                          pg.execute(f"SELECT fixture_id, collected FROM public.{STAMP_TABLE}").fetchall()}
                # Changed = a candidate current-season Fixture the ledger knows as Final whose
                # collected date is newer than what Postgres holds (absent stamp => never published).
                changed = [fid for fid in candidates
                           if fid in ledger and ledger[fid] > stamps.get(fid, "")]
                counts = _apply_delta(sq, pg, changed, ledger, commentary_path)
                pg.commit()
        finally:
            sq.close()

    print(f"\n  applied {counts.get('fixture', 0):,} changed Final(s), "
          f"refreshed {counts.get('fixture_scheduled', 0):,} scheduled Fixture(s)")
    _report_bridges(url)
    return counts


def _apply_delta(sq: sqlite3.Connection, pg: psycopg.Connection, changed: list[int],
                 ledger: dict[int, str], commentary_path: Path | None) -> dict[str, int]:
    """One transaction: upsert core dimensions, delete+reinsert the changed Finals and their
    child rows, advance their stamps, replace the current Season's scheduled Fixtures, and
    full-copy commentary. TEMP tables drop at COMMIT."""
    counts: dict[str, int] = {}
    # 1) Core dimensions (bounded: one Season) — upsert so a debut player / promoted team /
    #    new venue a changed Fixture references exists before its rows land.
    for table, pk in DELTA_DIMENSIONS:
        counts[table] = _upsert_dimension(sq, pg, table, pk)

    # 2) Changed Finals: delete child rows then the fixture (FK order), then reinsert from temp.
    if changed:
        for t in ("teammatchstat", "squadentry", "event"):
            pg.execute(f"DELETE FROM public.{t} WHERE fixture_id = ANY(%s)", (changed,))
        pg.execute("DELETE FROM public.fixture WHERE id = ANY(%s)", (changed,))
        counts["fixture"] = _insert_changed(sq, pg, "fixture", "id", changed)
        for t in ("event", "squadentry", "teammatchstat"):
            counts[t] = _insert_changed(sq, pg, t, "fixture_id", changed)
        # 3) Advance the watermark for exactly what we just applied.
        pg.cursor().executemany(
            f"INSERT INTO public.{STAMP_TABLE} (fixture_id, collected) VALUES (%s, %s) "
            "ON CONFLICT (fixture_id) DO UPDATE SET collected = EXCLUDED.collected",
            [(fid, ledger[fid]) for fid in changed])
    else:
        counts["fixture"] = 0

    # 3b) Scheduled Fixtures: replace the current Season's non-Final rows wholesale, so
    #     re-datings, cancellations and newly-drawn knockout Fixtures reach the store the
    #     Match Preview builder reads (ADR 0028 amendment, ADR 0040). Runs unconditionally
    #     — a night with zero changed Finals is exactly when a new round gets drawn.
    counts["fixture_scheduled"] = _replace_scheduled(sq, pg)

    # 4) Commentary: always full, unscoped (tiny).
    if commentary_path:
        counts.update(_copy_commentary_full(pg, commentary_path))
    return counts


def heal_venues(league_ids: list[int] | None = None, use_all: bool = False) -> dict[str, int]:
    """Fill venue_ids the intraday delta left null, once the nightly build has minted the
    new grounds (ADR 0028) — the surgical alternative to a nightly wholesale publish.

    An unattended delta is read-only against the registry, so a Fixture at a not-yet-registered
    ground publishes with a null venue_id. The nightly full football.db build mints that ground
    into the registry; this then re-derives each published current-Season Fixture's (name, city)
    from the raw cache — the identity is not stored on the null Fixture row — and, for any public
    Fixture still null whose ground is now registered, inserts the Venue row if missing (so the FK
    holds) and sets the Fixture's venue_id. Genuinely venue-less Fixtures (the provider named no
    ground) have no (name, city) and are correctly left null.
    """
    url = config.load_pg_url()
    comps = _competitions(league_ids, use_all)
    registry = venues.load()

    probe = CachedClient(max_live_requests=0)
    fx_key: dict[int, tuple[str, str | None]] = {}          # fixture id -> (name, city)
    provider: dict[tuple[str, str | None], int | None] = {}  # ground -> provider venue id
    for league_id, _name, season in _current_season_targets(comps):
        for fx in fetch.fetch_fixtures(probe, league_id, season):
            key = parse._venue_key(fx)
            if key is None:
                continue
            fx_key[fx["fixture"]["id"]] = key
            provider.setdefault(key, (fx["fixture"].get("venue") or {}).get("id"))

    with psycopg.connect(url, connect_timeout=30) as pg:
        if not pg.execute("SELECT to_regclass('public.fixture')").fetchone()[0]:
            raise SystemExit("No Published Store yet — nothing to heal.")
        null_ids = [r[0] for r in pg.execute(
            "SELECT id FROM public.fixture WHERE venue_id IS NULL AND id = ANY(%s)",
            (list(fx_key),)).fetchall()]
        heals = [(fid, registry[fx_key[fid]]) for fid in null_ids if fx_key[fid] in registry]
        if not heals:
            print("  venue heal: nothing to fill.")
            return {"healed": 0, "venues_inserted": 0}

        # A just-minted ground may not have a Venue row in public yet — insert it first (FK).
        need = {vid: fx_key[fid] for fid, vid in heals}
        have = {r[0] for r in pg.execute("SELECT id FROM public.venue WHERE id = ANY(%s)",
                                         (list(need),)).fetchall()}
        new_rows = [(vid, k[0], k[1], provider.get(k)) for vid, k in need.items() if vid not in have]
        if new_rows:
            pg.cursor().executemany(
                "INSERT INTO public.venue (id, name, city, provider_id) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING", new_rows)
        pg.cursor().executemany(
            "UPDATE public.fixture SET venue_id = %s WHERE id = %s AND venue_id IS NULL",
            [(vid, fid) for fid, vid in heals])
        pg.commit()
    print(f"  venue heal: filled {len(heals)} fixture(s); inserted {len(new_rows)} new Venue row(s).")
    return {"healed": len(heals), "venues_inserted": len(new_rows)}


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
            f"    uv run python -m football.onboard.orchestrate {comps[0]['league_id']}"
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

    _rebuild_stamps(url)   # baseline the delta watermark (ADR 0028)
    _report_bridges(url)
    return counts


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.publish.pg",
        description="Publish selected Competitions to the Postgres Published Store (ADR 0027).",
    )
    ap.add_argument("league_ids", type=int, nargs="*",
                    help=f"provider league ids (default: {' '.join(map(str, DEFAULT_LEAGUE_IDS))} "
                         "— Liga MX, Major League Soccer)")
    ap.add_argument("--all", action="store_true",
                    help="publish every tracked Competition instead")
    ap.add_argument("--heal-venues", action="store_true",
                    help="fill venue_ids the intraday delta left null, once tonight's build "
                         "minted the new grounds (ADR 0028) — no publish; run nightly")
    args = ap.parse_args(argv)
    if args.league_ids and args.all:
        raise SystemExit("Pass league ids or --all, not both.")

    t0 = time.monotonic()
    if args.heal_venues:
        heal_venues(args.league_ids, args.all)
        return
    counts = publish(args.league_ids, args.all)
    print(f"\nPublished Store updated — {sum(counts.values()):,} rows "
          f"in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()
