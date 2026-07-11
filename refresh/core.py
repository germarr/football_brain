"""Nightly Refresh: re-collect only the mutable frontier of the data (ADR 0018).

Every other collector is a one-shot backfill. This is the recurring job — meant to
run from cron each night — that keeps the store current without re-collecting the
immutable past or breaking the cache-first contract everything else relies on.

For every Competition (leagues *and* cups) it touches only the **current Season**
(`max(config seasons)`), and within it only the two things cache-first would freeze:
  1. the /leagues record (force-refreshed — refreshes provider metadata and exposes
     the provider's latest season, so we can warn when config needs a new one),
  2. the current Season's fixture list (force-refreshed — otherwise a match played
     since the list was first cached never surfaces).
From the fresh list it collects per-fixture data only for **Final** (FT/AET/PEN)
fixtures not already in the ledger (refresh/refresh_ledger.json), healing any
stale-empty cache left by the backfill (a fixture fetched while still scheduled). It
then runs the full enrichment chain (bios → careers → Team Profiles) for the players
and teams those new Finals surface — cache-first, so already-seen ones cost nothing.

Each run writes a human log (refresh/logs/refresh-YYYY-MM-DD.txt) listing which Teams
were updated vs unchanged per Competition, and records a row (plus a per-endpoint call
breakdown) in a standalone refresh/refresh.db that the nightly football.db rebuild
can't wipe. By default it ends with a full parse.build() + a re-scope of any existing
data/<slug>.db, even after a partial run, and exits non-zero on failure so cron notices.

The full rebuild dominates a run's wall-clock (the frontier collection is seconds; the
rebuild is minutes). For interactive iteration that only reads a scoped data/<slug>.db,
--scope-only collects the frontier and re-scopes straight from cache but skips the full
rebuild, so a Competition's scoped DB refreshes in seconds. football.db is then stale
until the next full run, which self-heals it (the Finals are already ledgered and cached).

Run with:
    uv run python -m refresh
    uv run python -m refresh --scope-only     # skip the full football.db rebuild; re-scope only
    uv run python -m refresh --no-rebuild     # collect + log only

See refresh/README.md for the operator's guide (crontab, the new-season warning, etc.).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from football import collect, config, orchestrate, parse, scope, teams
from football.client import CachedClient, QuotaExceeded

# A Fixture is Final (CONTEXT.md) — played to completion with per-fixture data to
# collect — iff its provider status.short is one of these.
FINAL_STATUSES = {"FT", "AET", "PEN"}

# All Refresh state lives beside this file, in the refresh/ folder — not under data/.
# That keeps the run history (refresh.db) and ledger with the code that owns them and
# clear of the football.db rebuild, which only touches data/.
REFRESH_DIR = Path(__file__).resolve().parent
LEDGER_FILE = REFRESH_DIR / "refresh_ledger.json"
HISTORY_DB = REFRESH_DIR / "refresh.db"
LOG_DIR = REFRESH_DIR / "logs"


# --- the ledger ------------------------------------------------------------

class Ledger:
    """The record of every Fixture collected while Final (ADR 0018).

    `fixture_id -> {"status": "FT", "collected": "2026-07-10"}`, in a standalone JSON
    file. A Fixture is written only after all its applicable per-fixture stages
    succeed, so an interrupted run re-does a half-collected match rather than skipping
    it. Fixture ids are global provider ids, so one flat map spans every Competition.
    """

    def __init__(self, path: Path = LEDGER_FILE) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def __contains__(self, fixture_id: int) -> bool:
        return str(fixture_id) in self._data

    def record(self, fixture_id: int, status: str, collected: str) -> None:
        self._data[str(fixture_id)] = {"status": status, "collected": collected}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))


# --- per-competition result ------------------------------------------------

class FixtureUpdate(NamedTuple):
    """One newly-Final Fixture, enough to render it from either team's side."""
    fid: int
    home_id: int
    home: str
    away_id: int
    away: str
    date: str
    score: str
    status: str

    def line_for(self, team_id: int) -> str:
        """A human line describing this fixture from `team_id`'s perspective."""
        if team_id == self.home_id:
            return f"vs {self.away}  {self.date}  {self.score}  ({self.status})"
        return f"@ {self.home}  {self.date}  {self.score}  ({self.status})"


class CompResult(NamedTuple):
    name: str
    comp_type: str
    current_season: int
    all_teams: dict[int, str]                     # every current-Season team id -> name
    updated_teams: dict[int, list[FixtureUpdate]]  # team id -> its new Final fixtures
    new_finals: int                               # count of newly-collected Final fixtures
    newer_season: int | None                      # provider season > config, else None


# --- collection ------------------------------------------------------------

def _heal_get(client: CachedClient, endpoint: str, params: dict) -> dict:
    """Fetch per-fixture data, force-refetching a stale-empty cache (ADR 0018).

    A fixture fetched while still scheduled cached an empty response that cache-first
    would return forever. So: if the cache is present but empty, force a live
    re-fetch; otherwise a plain cache-first get (a counted hit if present and
    non-empty, a live fetch if absent)."""
    cached = client.peek(endpoint, params)
    if cached is not None and not (cached.get("response") or []):
        return client.get(endpoint, params, force=True)
    return client.get(endpoint, params)


def _score(fixture: dict) -> str:
    """The scoreline `H-A` from a raw fixture, or `?-?` if the provider omits it."""
    goals = fixture.get("goals") or {}
    home, away = goals.get("home"), goals.get("away")
    return f"{home if home is not None else '?'}-{away if away is not None else '?'}"


def _refresh_competition(client: CachedClient, comp: dict, ledger: Ledger,
                         today: str) -> CompResult:
    """Refresh one Competition's current Season and return what changed.

    Force-refreshes the /leagues record and the current-season fixture list, collects
    per-fixture data for Final fixtures not yet in the ledger (healing stale-empties),
    then runs the enrichment chain for the players/teams those Finals surface."""
    league_id, name = comp["league_id"], comp["name"]
    current = max(comp["seasons"])

    # 1. /leagues record (force) — provider metadata, coverage, latest-season check.
    record = client.get("leagues", {"id": league_id}, force=True).get("response") or []
    newer_season: int | None = None
    coverage = orchestrate.SeasonCoverage(current, False, False)
    if record:
        rec = record[0]
        provider_years = [s.get("year") for s in rec.get("seasons", []) if s.get("year")]
        if provider_years and max(provider_years) > current:
            newer_season = max(provider_years)
        cur_cov = orchestrate._seasons(rec, current, current)
        if cur_cov:
            coverage = cur_cov[0]

    # 2. current-season fixture list (force) — surfaces matches played since last run.
    fixtures = client.get(
        "fixtures", {"league": league_id, "season": current}, force=True
    ).get("response") or []
    all_teams: dict[int, str] = {}
    for f in fixtures:
        for side in ("home", "away"):
            t = f["teams"].get(side) or {}
            if t.get("id"):
                all_teams[t["id"]] = t.get("name") or str(t["id"])

    # 3. per-fixture data for Final fixtures not already collected.
    new_finals = [
        f for f in fixtures
        if f["fixture"]["status"]["short"] in FINAL_STATUSES
        and f["fixture"]["id"] not in ledger
    ]
    updated: dict[int, list[FixtureUpdate]] = defaultdict(list)
    player_season: dict[int, int] = {}
    for f in new_finals:
        fid = f["fixture"]["id"]
        status = f["fixture"]["status"]["short"]
        if coverage.has_player_stats:
            fp = _heal_get(client, "fixtures/players", {"fixture": fid})
            for _, pblock in collect.players_in_fixture(fp):
                pid = pblock["player"]["id"]
                if pid:
                    player_season.setdefault(pid, current)
        _heal_get(client, "fixtures/events", {"fixture": fid})
        if coverage.has_fixture_stats:
            _heal_get(client, "fixtures/statistics", {"fixture": fid})

        upd = FixtureUpdate(
            fid=fid,
            home_id=(f["teams"].get("home") or {}).get("id") or 0,
            home=(f["teams"].get("home") or {}).get("name") or "?",
            away_id=(f["teams"].get("away") or {}).get("id") or 0,
            away=(f["teams"].get("away") or {}).get("name") or "?",
            date=(f["fixture"].get("date") or "")[:10],
            score=_score(f),
            status=status,
        )
        for tid in (upd.home_id, upd.away_id):
            if tid:
                updated[tid].append(upd)
        # Ledger only after every applicable stage above has succeeded for this match.
        ledger.record(fid, status, today)

    # 4. enrichment chain for the players/teams the new Finals surfaced (cache-first,
    #    so already-seen players and teams are free). Careers gate Team Profiles, both
    #    behind config.COLLECT_CAREERS to mirror the rest of the pipeline.
    for pid, season in sorted(player_season.items()):
        collect.fetch_player(client, pid, season)  # biography
    if config.COLLECT_CAREERS and player_season:
        career_team_ids: set[int] = set()
        for pid in sorted(player_season):
            for entry in collect.fetch_player_teams(client, pid):
                tid = (entry.get("team") or {}).get("id")
                if tid:
                    career_team_ids.add(tid)
        if career_team_ids:
            # Teams on our own current-season fixtures resolve their league from cache,
            # so they skip the leagues?team call (teams.enrich, ADR 0017).
            teams.enrich(client, career_team_ids, set(all_teams))

    return CompResult(
        name=name, comp_type=comp.get("type", "league"), current_season=current,
        all_teams=all_teams, updated_teams=dict(updated),
        new_finals=len(new_finals), newer_season=newer_season,
    )


def _attribute(per_comp_calls: dict[str, dict[str, int]], name: str,
               before: dict[str, int], client: CachedClient) -> None:
    """Record the live calls `name` spent, by endpoint, as the delta since `before`."""
    delta = {
        ep: client.live_by_endpoint[ep] - before.get(ep, 0)
        for ep in client.live_by_endpoint
        if client.live_by_endpoint[ep] - before.get(ep, 0) > 0
    }
    if delta:
        per_comp_calls[name] = delta


# --- reporting -------------------------------------------------------------

def _write_log(path: Path, started: datetime, finished: datetime,
               results: list[CompResult], client: CachedClient, outcome: str) -> None:
    """The human-readable per-run log — summary header, new-season warnings, then a
    per-Competition Updated/Unchanged Team breakdown with per-fixture detail."""
    total_teams = sum(len(r.all_teams) for r in results)
    teams_updated = sum(len(r.updated_teams) for r in results)
    finals = sum(r.new_finals for r in results)
    dur = int((finished - started).total_seconds())

    lines: list[str] = []
    lines.append(f"Refresh — {started.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 64)
    lines.append(
        f"Competitions: {len(results)}   ·   Final fixtures collected: {finals}   ·   "
        f"Teams updated: {teams_updated} / {total_teams}"
    )
    lines.append(
        f"API calls: live {client.live_requests:,} / cached {client.cache_hits:,}   ·   "
        f"duration {dur // 60}m {dur % 60:02d}s   ·   outcome: {outcome}"
    )

    warnings = [r for r in results if r.newer_season is not None]
    if warnings:
        lines.append("")
        for r in warnings:
            lines.append(
                f"⚠ NEW SEASON AVAILABLE: {r.name} {r.newer_season} — "
                f"add to config (currently pinned to {r.current_season})"
            )

    for r in results:
        lines.append("")
        lines.append(f"## {r.name} ({r.comp_type}) — season {r.current_season}")
        if not r.updated_teams:
            lines.append(f"  No new Final fixtures. {len(r.all_teams)} teams unchanged.")
            continue
        lines.append("  Updated:")
        for tid, fixtures in sorted(r.updated_teams.items(),
                                    key=lambda kv: r.all_teams.get(kv[0], "")):
            tname = r.all_teams.get(tid, str(tid))
            lines.append(f"    {tname} — {len(fixtures)} new fixture(s)")
            for upd in fixtures:
                lines.append(f"      {upd.line_for(tid)}")
        unchanged = sorted(
            n for t, n in r.all_teams.items() if t not in r.updated_teams
        )
        lines.append(f"  Unchanged: {len(unchanged)} teams")
        if unchanged:
            lines.append("    " + ", ".join(unchanged))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _record_run(started: datetime, finished: datetime, results: list[CompResult],
                per_comp_calls: dict[str, dict[str, int]], client: CachedClient,
                outcome: str) -> int:
    """Append this run to the standalone refresh.db (survives the football.db rebuild).

    `refresh_run` holds one row per night; `refresh_call` the run × Competition ×
    endpoint live-call breakdown. Returns the new run id."""
    con = sqlite3.connect(HISTORY_DB)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS refresh_run(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, started TEXT, finished TEXT, duration_s REAL,
                competitions INTEGER, fixtures_collected INTEGER, teams_updated INTEGER,
                live_calls INTEGER, cache_hits INTEGER, outcome TEXT);
            CREATE TABLE IF NOT EXISTS refresh_call(
                run_id INTEGER, competition TEXT, endpoint TEXT, calls INTEGER,
                FOREIGN KEY(run_id) REFERENCES refresh_run(id));
            """
        )
        cur = con.execute(
            "INSERT INTO refresh_run(date, started, finished, duration_s, competitions, "
            "fixtures_collected, teams_updated, live_calls, cache_hits, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                started.date().isoformat(),
                started.isoformat(timespec="seconds"),
                finished.isoformat(timespec="seconds"),
                (finished - started).total_seconds(),
                len(results),
                sum(r.new_finals for r in results),
                sum(len(r.updated_teams) for r in results),
                client.live_requests,
                client.cache_hits,
                outcome,
            ),
        )
        run_id = cur.lastrowid
        for comp_name, delta in sorted(per_comp_calls.items()):
            for endpoint, calls in sorted(delta.items()):
                con.execute(
                    "INSERT INTO refresh_call(run_id, competition, endpoint, calls) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, comp_name, endpoint, calls),
                )
        con.commit()
        return run_id
    finally:
        con.close()


def _rescope_existing() -> list[str]:
    """Rebuild every scoped data/<slug>.db that already exists (zero-API re-parse)."""
    rescoped: list[str] = []
    for comp in config.COMPETITIONS:
        if scope._db_path(comp["name"]).exists():
            try:
                scope._build(comp["league_id"])
                rescoped.append(comp["name"])
            except SystemExit as e:  # a slug on disk with no cached fixtures — skip it
                print(f"  skip re-scope {comp['name']}: {e}")
    return rescoped


# --- entrypoint ------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m refresh",
        description="Nightly Refresh of every Competition's current Season (ADR 0018).",
    )
    rebuild = ap.add_mutually_exclusive_group()
    rebuild.add_argument("--no-rebuild", action="store_true",
                    help="collect + log only; do not rebuild football.db or scoped DBs")
    rebuild.add_argument("--scope-only", action="store_true",
                    help="skip the full football.db rebuild but still re-scope existing "
                         "data/<slug>.db files (the interactive fast path — leaves "
                         "football.db stale until the next full Refresh)")
    args = ap.parse_args(argv)

    started = datetime.now()
    today = started.date().isoformat()
    client = CachedClient()
    ledger = Ledger()
    results: list[CompResult] = []
    per_comp_calls: dict[str, dict[str, int]] = {}
    outcome = "ok"

    print(f"Refresh — {started:%Y-%m-%d %H:%M:%S}: {len(config.COMPETITIONS)} competitions")
    try:
        for comp in config.COMPETITIONS:
            before = dict(client.live_by_endpoint)
            try:
                res = _refresh_competition(client, comp, ledger, today)
            except QuotaExceeded as e:
                outcome = "interrupted"
                _attribute(per_comp_calls, comp["name"], before, client)
                print(f"\n[stopped] {e}")
                break
            except Exception as e:  # one bad competition shouldn't sink the whole run
                outcome = "failed"
                _attribute(per_comp_calls, comp["name"], before, client)
                print(f"  [error] {comp['name']}: {e}", file=sys.stderr)
                continue
            _attribute(per_comp_calls, res.name, before, client)
            results.append(res)
            flag = f"  ⚠ new season {res.newer_season}" if res.newer_season else ""
            print(f"  {res.name}: {res.new_finals} new Final(s), "
                  f"{len(res.updated_teams)} team(s) updated{flag}")
    finally:
        ledger.save()  # persist partial progress no matter how we exit the loop

    # Rebuild — the cache is internally consistent even after a partial run, so there is
    # no half-collected state to protect against (unlike orchestrate). Default does the
    # full football.db rebuild then re-scopes; --scope-only skips only the ~13-min full
    # rebuild and re-scopes straight from cache (football.db goes stale until the next
    # full run, which self-heals it); --no-rebuild skips both.
    if not args.no_rebuild:
        if args.scope_only:
            print("\n▶ --scope-only: skipping the full football.db rebuild "
                  "(stale until the next full Refresh); re-scoping from cache …")
        else:
            print("\n▶ Rebuilding football.db from the raw cache …")
            parse.build()
        rescoped = _rescope_existing()
        if rescoped:
            print(f"  re-scoped: {', '.join(rescoped)}")

    finished = datetime.now()
    log_path = LOG_DIR / f"refresh-{today}.txt"
    _write_log(log_path, started, finished, results, client, outcome)
    run_id = _record_run(started, finished, results, per_comp_calls, client, outcome)

    finals = sum(r.new_finals for r in results)
    print(f"\nRefresh {outcome}: {finals} Final fixture(s) collected, "
          f"live {client.live_requests} / cached {client.cache_hits} requests.")
    print(f"  log: {log_path.relative_to(config.ROOT)}   ·   "
          f"refresh.db run #{run_id}")
    if outcome != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
