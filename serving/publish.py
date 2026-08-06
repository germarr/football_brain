"""Publish `serving/serve.db` — the Viewer's read-optimized serving store (ADR 0023).

A zero-API DB→DB copy: open the authoritative `data/football.db` read-only and copy a
small, schema-identical slice into a fresh `serve.db` — everything the Viewer renders
and nothing else. It writes `serve.db.tmp` and then **atomically renames** it over
`serve.db`, so a reader never sees a half-written store (the blue-green swap of ADR
0023). This is distinct from `football.build.scope` (which re-parses a Competition from the
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
    uv run python -m serving.publish                    # default window: -3 .. +10 days
    uv run python -m serving.publish --before 3 --after 10
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import SQLModel, create_engine

from football import config, models, status  # noqa: F401 — importing models registers the schema

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
        # `start` is the window's past edge: a polled fixture older than it can never
        # reappear in a future publish, so this is what makes reason 2 decidable.
        counts["live_cleared"] = _clear_settled_live_rows(serve_db, start)
        return counts, (start, end)
    finally:
        lock_f.close()


# --------------------------------------------------------------------------- #
# League standings + leaders (ADR 0025)
# --------------------------------------------------------------------------- #
FINAL = status.FINAL           # a Final fixture counts toward the table (CONTEXT.md)
LEADER_TOP_N = 5


def _season_label(league_id: int, season: int) -> str:
    """"2025/26" for a straddling league, "2025" for a calendar-year one (ADR 0001/CONTEXT)."""
    if league_id in config.CALENDAR_YEAR_LEAGUES:
        return str(season)
    return f"{season}/{str(season + 1)[-2:]}"


def _standings_rows(fixtures: list) -> list[dict]:
    """League table over (home_id, home_name, away_id, away_name, home_goals, away_goals,
    status) tuples. Every fixture seeds its two teams (so a not-yet-started season still
    lists all teams at 0), but only **Final** fixtures score points — explore.py's rules:
    3/1/0, sort Pts → GD → GF."""
    teams: dict[int, dict] = {}

    def ensure(tid: int, name: str) -> dict:
        return teams.setdefault(tid, {
            "team_id": tid, "name": name,
            "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "Pts": 0,
        })

    for h, hn, a, an, hg, ag, status in fixtures:
        th, ta = ensure(h, hn), ensure(a, an)   # seed teams even for scheduled games
        if status not in FINAL or hg is None or ag is None:
            continue
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


def _name_map(con: sqlite3.Connection, table: str, ids: set[int]) -> dict[int, str]:
    """Bulk id→name lookup from src.player / src.team, chunked under SQLite's variable
    limit. Only the handful of ids that actually surface as leaders are resolved."""
    out: dict[int, str] = {}
    ids = list(ids)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        placeholders = ",".join("?" * len(chunk))
        for rid, name in con.execute(
            f"select id, name from src.{table} where id in ({placeholders})", chunk
        ):
            out[rid] = name
    return out


def _build_league_tables(con: sqlite3.Connection) -> dict:
    """Compute standings + leaders for every (league, season, TOURNAMENT) a tracked league
    has — including not-yet-started seasons (rendered as 0's) — and write them into the
    serving store, keyed by (league_id, season, tournament) (ADR 0025 + season/tournament
    picker). Leagues only; cups skipped. A season's tournaments (La Liga: one "Regular
    Season"; Liga MX: "Apertura"/"Clausura") get a `tour_order` by first fixture date.

    Cost stays bounded to ONE fixture scan and ONE squadentry aggregation for the whole
    store: the aggregation groups by (league, season, tournament, player, team) with NO
    name joins — names are resolved in bulk afterwards for only the surfaced top-5."""
    con.execute(
        "create table league_meta (league_id integer, season integer, tournament text, "
        "tour_order integer, season_label text, team_count integer, played integer, "
        "stats_light integer, top_team_name text, top_team_goals integer, "
        "primary key (league_id, season, tournament))"
    )
    con.execute(
        "create table league_standing (league_id integer, season integer, tournament text, "
        "pos integer, team_id integer, team_name text, P integer, W integer, D integer, "
        "L integer, GF integer, GA integer, GD integer, Pts integer)"
    )
    con.execute(
        "create table league_scorer (league_id integer, season integer, tournament text, "
        "kind text, rank integer, player_id integer, player_name text, team_name text, "
        "value integer)"
    )

    league_ids = {r[0] for r in con.execute(
        "select id from src.competition where lower(type) = 'league'")}
    counts = {"leagues": 0, "league_seasons": 0, "standings": 0, "leaders": 0}
    if not league_ids:
        return counts

    # 1) One scan of every regular-season fixture (ANY status, so scheduled ones seed the
    #    0's tables); bucket by (league, season, tournament) and track each's first date.
    by_kst: dict[tuple, list] = defaultdict(list)
    first_date: dict[tuple, str] = {}
    for lid, season, tour, fid, h, hn, a, an, hg, ag, status, date in con.execute(
        "select league_id, season, tournament, id, home_team_id, home_team_name, "
        "away_team_id, away_team_name, home_goals, away_goals, status, date "
        "from src.fixture where matchday is not null"
    ):
        if lid not in league_ids:
            continue
        key = (lid, season, tour)
        by_kst[key].append((h, hn, a, an, hg, ag, status, fid))
        if date and (key not in first_date or date < first_date[key]):
            first_date[key] = date
    if not by_kst:
        return counts

    # tour_order: rank a season's tournaments by first fixture date (Apertura before Clausura).
    tour_order: dict[tuple, int] = {}
    by_ls: dict[tuple, list] = defaultdict(list)
    for (lid, season, tour) in by_kst:
        by_ls[(lid, season)].append(tour)
    for (lid, season), tours in by_ls.items():
        for i, tour in enumerate(sorted(tours, key=lambda t: first_date.get((lid, season, t), ""))):
            tour_order[(lid, season, tour)] = i

    # 2) One squadentry aggregation over all those fixtures — grouped by id, no name joins.
    con.execute("drop table if exists _fx")
    con.execute("create temp table _fx (id integer primary key, league_id integer, "
                "season integer, tournament text)")
    con.executemany(
        "insert into _fx (id, league_id, season, tournament) values (?,?,?,?)",
        [(row[7], lid, season, tour) for (lid, season, tour), rows in by_kst.items() for row in rows],
    )
    stats: dict[tuple, dict] = defaultdict(dict)
    for lid, season, tour, pid, tid, goals, assists, n in con.execute(
        "select fx.league_id, fx.season, fx.tournament, se.player_id, se.team_id, "
        "sum(se.goals), sum(se.assists), count(*) "
        "from _fx fx join src.squadentry se on se.fixture_id = fx.id "
        "group by fx.league_id, fx.season, fx.tournament, se.player_id, se.team_id"
    ):
        players = stats[(lid, season, tour)]
        p = players.setdefault(pid, {"goals": 0, "assists": 0, "teams": {}})
        p["goals"] += goals or 0
        p["assists"] += assists or 0
        p["teams"][tid] = p["teams"].get(tid, 0) + n
    con.execute("drop table if exists _fx")

    # 3) Rank top-N per (league, season, tournament) by id; collect ids that need names.
    def top(players: dict, metric: str) -> list[tuple]:
        ranked = sorted(
            ((pid, p[metric], max(p["teams"].items(), key=lambda kv: kv[1])[0])
             for pid, p in players.items() if p[metric] > 0),
            key=lambda x: (-x[1], x[0]),
        )
        return ranked[:LEADER_TOP_N]   # (player_id, value, primary_team_id)

    leaders: dict[tuple, tuple] = {}
    need_p: set[int] = set()
    need_t: set[int] = set()
    for key in by_kst:
        players = stats.get(key)
        if not players:
            leaders[key] = ([], [])
            continue
        scorers, assists = top(players, "goals"), top(players, "assists")
        leaders[key] = (scorers, assists)
        for pid, _, tid in scorers + assists:
            need_p.add(pid)
            need_t.add(tid)
    pnames = _name_map(con, "player", need_p)
    tnames = _name_map(con, "team", need_t)

    # 4) Write per (league, season, tournament).
    for (lid, season, tour), fx in by_kst.items():
        standings = _standings_rows([row[:7] for row in fx])  # (h,hn,a,an,hg,ag,status)
        if not standings:
            continue
        played = sum(t["P"] for t in standings) // 2
        stats_light = 1 if (played > 0 and (lid, season, tour) not in stats) else 0
        top_team = max(standings, key=lambda t: t["GF"]) if played > 0 else None
        scorers, assists = leaders[(lid, season, tour)]

        con.execute(
            "insert into league_meta (league_id, season, tournament, tour_order, season_label, "
            "team_count, played, stats_light, top_team_name, top_team_goals) "
            "values (?,?,?,?,?,?,?,?,?,?)",
            (lid, season, tour, tour_order[(lid, season, tour)], _season_label(lid, season),
             len(standings), played, stats_light,
             top_team["name"] if top_team else None, top_team["GF"] if top_team else 0),
        )
        con.executemany(
            "insert into league_standing (league_id,season,tournament,pos,team_id,team_name,P,W,D,L,GF,GA,GD,Pts) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(lid, season, tour, pos, t["team_id"], t["name"], t["P"], t["W"], t["D"], t["L"],
              t["GF"], t["GA"], t["GD"], t["Pts"]) for pos, t in enumerate(standings, 1)],
        )
        for kind, raw in (("goals", scorers), ("assists", assists)):
            con.executemany(
                "insert into league_scorer (league_id,season,tournament,kind,rank,player_id,player_name,team_name,value) "
                "values (?,?,?,?,?,?,?,?,?)",
                [(lid, season, tour, kind, i, pid, pnames.get(pid, "?"), tnames.get(tid, "?"), val)
                 for i, (pid, val, tid) in enumerate(raw, 1)],
            )
        counts["league_seasons"] += 1
        counts["standings"] += len(standings)
        counts["leaders"] += len(scorers) + len(assists)
    counts["leagues"] = len({lid for (lid, _, _) in by_kst})
    return counts


#: A fixture the Live Mirror should stop shadowing: Final, or ended without a Final
#: (postponed / cancelled / abandoned / awarded / walkover). Clearing only on Final left
#: a postponed game provisional forever, since nothing else ever revisits it.
TERMINAL = status.TERMINAL


def _clear_settled_live_rows(serve_db: Path, window_start: str) -> int:
    """Auto-clear the Live Mirror once the snapshot can take over (ADR 0024).

    Two reasons to drop a fixture's provisional rows:

      1. **serve.db now records it as terminal.** The overlay is redundant, and leaving
         it keeps a stale `provisional` marker on a finished game in the Viewer's table.
         The timing here is not incidental and is the reason this lives in publish rather
         than in the poller: clearing when the *poll* first sees a Final would drop the
         match page back to a serve.db that does not have the result yet, rendering a
         finished game as **scheduled**. A row may only be cleared once something
         authoritative can answer in its place.

      2. **serve.db can never adjudicate it.** A fixture whose kickoff predates the
         published window's past edge will not appear in this publish or any later one,
         so reason 1 can never fire for it. This happens whenever no publish runs for
         longer than `--before` days — a stopped cron, a machine that was off. Without
         this clause those rows stay provisional permanently, and the Match Tracker keeps
         serving a half-finished poll of a match nobody remembers watching, with no error
         anywhere (ADR 0033).

    Best-effort: a failure here never fails the publish — serve.db is already swapped in.
    """
    live_path = config.ROOT / "live" / "live.db"
    if not live_path.exists():
        return 0
    try:
        con = sqlite3.connect(live_path, timeout=5)
        try:
            con.execute("ATTACH DATABASE ? AS serve", (str(serve_db),))
            marks = ",".join("?" * len(TERMINAL))
            settled = [
                r[0] for r in con.execute(
                    # LEFT joins on purpose: reason 2 is precisely the case where the
                    # fixture is *absent* from serve.db, which an inner join drops.
                    f"select lp.fixture_id from livepoll lp "
                    f"left join serve.fixture sf on sf.id = lp.fixture_id "
                    f"left join fixture lf on lf.id = lp.fixture_id "
                    f"where upper(coalesce(sf.status,'')) in ({marks}) "
                    f"   or (sf.id is null and coalesce(lf.date, lp.polled_at) < ?)",
                    (*TERMINAL, window_start),
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
              "  football.db is up to date; run `python -m serving.publish` to refresh the Viewer.")
        return
    print(f"  serve.db updated — {counts.get('fixture', 0)} fixtures, "
          f"{counts.get('event', 0)} events in window.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m serving.publish",
        description="Publish serving/serve.db from data/football.db (ADR 0023).",
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
