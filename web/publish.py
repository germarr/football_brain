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
            # Precomputed league standings + leaders (ADR 0025), derived from the FULL
            # data in src (football.db) — serve.db itself only carries the window above.
            counts.update(_build_league_tables(con))
            con.commit()
            con.execute("DETACH DATABASE src")
        finally:
            con.close()

        os.replace(tmp, serve_db)   # atomic swap — readers never see a partial store
        counts["live_cleared"] = _clear_settled_live_rows(serve_db)
        return counts, (start, end)
    finally:
        lock_f.close()


# --------------------------------------------------------------------------- #
# League standings + leaders (ADR 0025)
# --------------------------------------------------------------------------- #
FINAL = ("FT", "AET", "PEN")   # a Final fixture counts toward the table (CONTEXT.md)
LEADER_TOP_N = 5


def _season_label(league_id: int, season: int) -> str:
    """"2025/26" for a straddling league, "2025" for a calendar-year one (ADR 0001/CONTEXT)."""
    if league_id in config.CALENDAR_YEAR_LEAGUES:
        return str(season)
    return f"{season}/{str(season + 1)[-2:]}"


def _standings_rows(finals: list) -> list[dict]:
    """League table from a season's Final, regular-season fixtures — pure function over
    (home_id, home_name, away_id, away_name, home_goals, away_goals) tuples. Reuses
    explore.py's rules: 3/1/0, sort Pts → GD → GF."""
    teams: dict[int, dict] = {}

    def ensure(tid: int, name: str) -> dict:
        return teams.setdefault(tid, {
            "team_id": tid, "name": name,
            "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "Pts": 0,
        })

    for h, hn, a, an, hg, ag in finals:
        th, ta = ensure(h, hn), ensure(a, an)
        th["P"] += 1; ta["P"] += 1
        th["GF"] += hg; th["GA"] += ag; ta["GF"] += ag; ta["GA"] += hg
        if hg > ag:
            th["W"] += 1; th["Pts"] += 3; ta["L"] += 1
        elif ag > hg:
            ta["W"] += 1; ta["Pts"] += 3; th["L"] += 1
        else:
            th["D"] += 1; ta["D"] += 1; th["Pts"] += 1; ta["Pts"] += 1

    rows = list(teams.values())
    for t in rows:
        t["GD"] = t["GF"] - t["GA"]
    rows.sort(key=lambda t: (-t["Pts"], -t["GD"], -t["GF"], t["name"]))
    return rows


def _leaders(con: sqlite3.Connection, fids: list[int]) -> tuple[list, list, bool]:
    """Top-5 scorers + assisters over the given season's fixture ids, by season total
    (grouped per player, primary team shown). Returns (scorers, assists, stats_light) —
    stats_light True when those fixtures have no SquadEntry (fixtures+events-only
    Coverage; CONTEXT.md)."""
    if not fids:
        return [], [], True
    con.execute("drop table if exists _fx")
    con.execute("create temp table _fx (id integer primary key)")
    con.executemany("insert into _fx (id) values (?)", [(i,) for i in fids])
    rows = con.execute(
        "select se.player_id, p.name, t.name, sum(se.goals), sum(se.assists), count(*) "
        "from src.squadentry se "
        "join _fx on _fx.id = se.fixture_id "
        "join src.player p on p.id = se.player_id "
        "join src.team t on t.id = se.team_id "
        "group by se.player_id, se.team_id"
    ).fetchall()
    con.execute("drop table if exists _fx")
    if not rows:
        return [], [], True   # Final fixtures exist but no squad data → stats-light

    players: dict[int, dict] = {}
    for pid, pname, tname, goals, assists, n in rows:
        p = players.setdefault(pid, {"player_id": pid, "name": pname,
                                     "goals": 0, "assists": 0, "teams": {}})
        p["goals"] += goals or 0
        p["assists"] += assists or 0
        p["teams"][tname] = p["teams"].get(tname, 0) + n

    def rank(metric: str) -> list[dict]:
        out = [
            {"player_id": p["player_id"], "name": p["name"],
             "team": max(p["teams"].items(), key=lambda kv: kv[1])[0], "value": p[metric]}
            for p in players.values() if p[metric] > 0
        ]
        out.sort(key=lambda x: (-x["value"], x["name"]))
        return out[:LEADER_TOP_N]

    return rank("goals"), rank("assists"), False


def _build_league_tables(con: sqlite3.Connection) -> dict:
    """Compute standings + leaders for EVERY season a tracked league has played (ADR 0025 +
    season picker), keyed by (league_id, season), and write them into the serving store.
    Reads the FULL data from the attached `src` (football.db). Leagues only; cups skipped.
    One fixture scan per league (grouped by season in Python) keeps the cost bounded."""
    con.execute(
        "create table league_meta (league_id integer, season integer, season_label text, "
        "team_count integer, played integer, stats_light integer, top_team_name text, "
        "top_team_goals integer, primary key (league_id, season))"
    )
    con.execute(
        "create table league_standing (league_id integer, season integer, pos integer, "
        "team_id integer, team_name text, P integer, W integer, D integer, L integer, "
        "GF integer, GA integer, GD integer, Pts integer)"
    )
    con.execute(
        "create table league_scorer (league_id integer, season integer, kind text, "
        "rank integer, player_id integer, player_name text, team_name text, value integer)"
    )

    leagues = con.execute(
        "select id from src.competition where lower(type) = 'league'"
    ).fetchall()
    counts = {"leagues": 0, "league_seasons": 0, "standings": 0, "leaders": 0}
    for (lid,) in leagues:
        # All Final, regular-season fixtures for this league, every season, in one scan.
        all_fx = con.execute(
            "select season, id, home_team_id, home_team_name, away_team_id, away_team_name, "
            "home_goals, away_goals from src.fixture where league_id=? and matchday is not null "
            f"and status in {FINAL} and home_goals is not null and away_goals is not null",
            (lid,),
        ).fetchall()
        by_season: dict[int, list] = {}
        for season, fid, h, hn, a, an, hg, ag in all_fx:
            by_season.setdefault(season, []).append((fid, h, hn, a, an, hg, ag))
        if not by_season:
            continue

        for season, fx in by_season.items():
            standings = _standings_rows([row[1:] for row in fx])  # drop the fixture id
            if not standings:
                continue
            scorers, assists, stats_light = _leaders(con, [row[0] for row in fx])
            top_team = max(standings, key=lambda t: t["GF"])

            con.execute(
                "insert into league_meta (league_id, season, season_label, team_count, played, "
                "stats_light, top_team_name, top_team_goals) values (?,?,?,?,?,?,?,?)",
                (lid, season, _season_label(lid, season), len(standings),
                 sum(t["P"] for t in standings) // 2, 1 if stats_light else 0,
                 top_team["name"], top_team["GF"]),
            )
            con.executemany(
                "insert into league_standing (league_id,season,pos,team_id,team_name,P,W,D,L,GF,GA,GD,Pts) "
                "values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(lid, season, pos, t["team_id"], t["name"], t["P"], t["W"], t["D"], t["L"],
                  t["GF"], t["GA"], t["GD"], t["Pts"]) for pos, t in enumerate(standings, 1)],
            )
            for kind, leaders in (("goals", scorers), ("assists", assists)):
                con.executemany(
                    "insert into league_scorer (league_id,season,kind,rank,player_id,player_name,team_name,value) "
                    "values (?,?,?,?,?,?,?,?)",
                    [(lid, season, kind, i, l["player_id"], l["name"], l["team"], l["value"])
                     for i, l in enumerate(leaders, 1)],
                )
            counts["league_seasons"] += 1
            counts["standings"] += len(standings)
            counts["leaders"] += len(scorers) + len(assists)
        counts["leagues"] += 1
    return counts


def _clear_settled_live_rows(serve_db: Path) -> int:
    """Auto-clear the Live Mirror once the snapshot has caught up (ADR 0024).

    Delete `live/live.db` rows for any fixture the freshly-published serve.db now records
    as **Final** — its provisional overlay is redundant, and leaving it would keep a stale
    'provisional' marker on a finished game in the Viewer table. Best-effort: a failure
    here never fails the publish (serve.db is already swapped in).
    """
    live_path = config.ROOT / "live" / "live.db"
    if not live_path.exists():
        return 0
    try:
        con = sqlite3.connect(live_path, timeout=5)
        try:
            con.execute("ATTACH DATABASE ? AS serve", (str(serve_db),))
            settled = [
                r[0] for r in con.execute(
                    "select lp.fixture_id from livepoll lp "
                    "join serve.fixture sf on sf.id = lp.fixture_id "
                    "where upper(sf.status) in ('FT','AET','PEN')"
                )
            ]
            for fid in settled:
                con.execute("delete from event where fixture_id = ?", (fid,))
                con.execute("delete from fixture where id = ?", (fid,))
                con.execute("delete from livepoll where fixture_id = ?", (fid,))
            con.commit()
            con.execute("DETACH DATABASE serve")
            return len(settled)
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def publish_after_build() -> None:
    """Auto-publish hook for a collector that just rebuilt football.db (ADR 0023).

    Non-fatal by design: the collection + football.db rebuild have already succeeded,
    so a publish failure (e.g. a concurrent build holding the lock, a disk error) must
    NOT sink that work — it only means the Viewer shows the previous snapshot until the
    next publish. Warn and point at the manual command instead of raising.
    """
    print("\n▶ Publishing serve.db for the Viewer …")
    try:
        counts, _ = publish()
    except BaseException as e:  # SystemExit (build lock) included — never crash the collect
        print(f"⚠ serve.db publish skipped: {e}\n"
              "  football.db is up to date; run `python -m web.publish` to refresh the Viewer.")
        return
    print(f"  serve.db updated — {counts.get('fixture', 0)} fixtures, "
          f"{counts.get('event', 0)} events in window.")


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
