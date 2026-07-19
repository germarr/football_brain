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
    uv run python -m refresh --only 262 253   # refresh just these Competitions (quota-saving scope)

See refresh/README.md for the operator's guide (crontab, the new-season warning, etc.).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

from football import collect, config, orchestrate, parse, scope, teams
from football.client import CachedClient, QuotaExceeded

# A Fixture is Final (CONTEXT.md) — played to completion with per-fixture data to
# collect — iff its provider status.short is one of these.
FINAL_STATUSES = {"FT", "AET", "PEN"}

# A Season's per-fixture stat Coverage (statistics_players / statistics_fixtures) can
# lag the data: a brand-new Season opens flagged stats-light, yet its Finals' player and
# team-stat endpoints may already return populated payloads days before the flag flips
# (ADR 0018 amendment 2, 2026-07-17). So when a stat is *expected* — the Competition's
# last completed Season carried it — Refresh probes a stats-light Season's recent Finals
# optimistically and collects whatever data has actually landed, rather than waiting for
# the flag. The lag runs the other way too: a Season's flag can be *on* before an
# individual match's payload has populated (data lands unevenly across a matchday), so a
# covered Final whose fetch comes back empty is left owing and retried, not trusted as
# empty, while it is still within the window. This window bounds both — a genuinely empty
# Final (covered or stats-light) that has aged past N days is stamped and not re-probed
# forever (ADR 0018 amendment 3, 2026-07-17).
OPTIMISTIC_PROBE_DAYS = 14

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

    `fixture_id -> {"status": "FT", "collected": "2026-07-10", "coverage": {...}}`, in a
    standalone JSON file. A Fixture is written only after all its applicable per-fixture
    stages succeed, so an interrupted run re-does a half-collected match rather than
    skipping it. Fixture ids are global provider ids, so one flat map spans every
    Competition.

    The `coverage` fingerprint records which Coverage-gated stages a Final actually
    carries — `{"players": bool, "fixture_stats": bool}` (events is unconditional and not
    tracked). It is the per-Fixture record of collected stat data, and drives re-heal in
    two ways (`_stages_to_attempt`): a Season whose Coverage the provider *widens* after
    opening night re-collects any Final whose fingerprint is missing a now-covered stage;
    and a stats-light Season whose stats merely *lag* the flag has its recent Finals
    probed optimistically until the missing stages' data lands. Without it, a Final
    collected in the stats-light window would be ledgered on its events alone and never
    gain a Squad Entry or Team Match Stat (ADR 0018 amendments 2026-07-17).
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

    def fingerprint(self, fixture_id: int) -> dict:
        """The Coverage fingerprint recorded for a Fixture — which gated stages it
        carries — or `{}` if the Fixture is unseen or is a legacy entry written before
        fingerprints existed. A legacy `{}` reads as *nothing collected*, so every gated
        stage is re-evaluated (and re-stamped) on the next run."""
        entry = self._data.get(str(fixture_id))
        return (entry or {}).get("coverage") or {}

    def record(self, fixture_id: int, status: str, collected: str,
               coverage: dict) -> None:
        self._data[str(fixture_id)] = {
            "status": status, "collected": collected, "coverage": coverage,
        }

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


def _within(fixture_date: str, today: str, days: int) -> bool:
    """True if `fixture_date` (provider ISO date) falls within `days` before `today`.

    Both are `YYYY-MM-DD…`; only the date part is compared. A future or unparseable date
    reads as out of window (never optimistically probed)."""
    try:
        fd = date.fromisoformat((fixture_date or "")[:10])
        td = date.fromisoformat((today or "")[:10])
    except ValueError:
        return False
    return 0 <= (td - fd).days <= days


def _expected_coverage(rec: dict, current: int) -> dict:
    """Whether each stat stage is *expected* for the current Season, per the most recent
    prior Season (ADR 0018 amendment 2). A stats-light current Season of a Competition
    whose last completed Season carried the stat is a provider *lag* — its Finals are
    probed optimistically. A Competition stats-light in prior Seasons too (e.g. Liga MX
    Femenil) is genuinely stats-light: not expected, never probed. A Competition with no
    prior Season (its very first) defaults to not-expected — conservative, so a brand-new
    genuinely stats-light Competition is not probed on a hunch."""
    priors = [c for c in orchestrate._seasons(rec, None, None) if c.year < current]
    if not priors:
        return {"players": False, "fixture_stats": False}
    latest = max(priors, key=lambda c: c.year)
    return {"players": latest.has_player_stats, "fixture_stats": latest.has_fixture_stats}


def _stages_to_attempt(fingerprint: dict, season: dict, expected: dict,
                       fixture_date: str, today: str) -> dict:
    """Per stat stage, whether this run should (re)fetch it for a Final.

    A stage already in the `fingerprint` is done. Otherwise: if the current Season covers
    it, collect definitively (a first collection or a Coverage widen). If the Season is
    stats-light but the stage is *expected* and the Final is recent, probe optimistically
    — the flag may simply lag data that has already landed. Everything else is skipped."""
    out: dict[str, bool] = {}
    for stage, covered in season.items():
        if fingerprint.get(stage):
            out[stage] = False
        elif covered:
            out[stage] = True
        else:
            out[stage] = (expected.get(stage, False)
                          and _within(fixture_date, today, OPTIMISTIC_PROBE_DAYS))
    return out


def _refresh_competition(client: CachedClient, comp: dict, ledger: Ledger,
                         today: str) -> CompResult:
    """Refresh one Competition's current Season and return what changed.

    Force-refreshes the /leagues record and the current-season fixture list, collects
    per-fixture data for Final fixtures still owing a stage — never collected, collected
    under a Coverage the provider has since widened, or in a stats-light Season whose
    expected stats merely lag the flag (probed optimistically until they land) — then runs
    the enrichment chain for the players/teams those Finals surface."""
    league_id, name = comp["league_id"], comp["name"]
    current = max(comp["seasons"])

    # 1. /leagues record (force) — provider metadata, coverage, latest-season check.
    record = client.get("leagues", {"id": league_id}, force=True).get("response") or []
    newer_season: int | None = None
    coverage = orchestrate.SeasonCoverage(current, False, False)
    expected = {"players": False, "fixture_stats": False}
    if record:
        rec = record[0]
        provider_years = [s.get("year") for s in rec.get("seasons", []) if s.get("year")]
        if provider_years and max(provider_years) > current:
            newer_season = max(provider_years)
        cur_cov = orchestrate._seasons(rec, current, current)
        if cur_cov:
            coverage = cur_cov[0]
        # What the Competition normally carries — the yardstick for probing a stats-light
        # current Season whose stat flags may just be lagging the data (ADR 0018 amend. 2).
        expected = _expected_coverage(rec, current)

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

    # 3. per-fixture data for every Final owing a stat stage — never collected, or
    #    collected while the Season was stats-light and a stage is now available (the
    #    provider widened Coverage, or its stats merely lagged the flag). `season_cov` is
    #    the current Season's flags; `expected` is what the Competition normally carries.
    season_cov = {"players": coverage.has_player_stats,
                  "fixture_stats": coverage.has_fixture_stats}
    finals = [f for f in fixtures
              if f["fixture"]["status"]["short"] in FINAL_STATUSES]
    updated: dict[int, list[FixtureUpdate]] = defaultdict(list)
    collected_fids: set[int] = set()  # Finals that gained data this run (report + count)
    player_season: dict[int, int] = {}
    for f in finals:
        fid = f["fixture"]["id"]
        status = f["fixture"]["status"]["short"]
        fdate = (f["fixture"].get("date") or "")[:10]
        prev_fp = ledger.fingerprint(fid)
        is_new = fid not in ledger
        attempt = _stages_to_attempt(prev_fp, season_cov, expected, fdate, today)
        if not is_new and not any(attempt.values()):
            continue  # already carries every stage it can — nothing to do

        # A stat stage is collected (fingerprint True) as soon as a fetch returns data.
        # An *empty* fetch is trusted as genuinely-empty (stamped True) only once the Final
        # has aged out of the probe window; while it is still recent the stage is left owing
        # and retried next night — a covered Season's flag can be on before a given match's
        # payload has landed (data populates unevenly across a matchday), so an empty within
        # the window may simply be lagging, not absent. A stats-light Season (flag off) is
        # never stamped off an empty probe regardless of age, as before.
        recent = _within(fdate, today, OPTIMISTIC_PROBE_DAYS)
        live_before = client.live_requests
        new_fp = dict(prev_fp)
        if attempt["players"]:
            fp = _heal_get(client, "fixtures/players", {"fixture": fid})
            blocks = collect.players_in_fixture(fp)
            if blocks or (season_cov["players"] and not recent):
                new_fp["players"] = True
            for _, pblock in blocks:
                pid = pblock["player"]["id"]
                if pid:
                    player_season.setdefault(pid, current)
        if is_new:  # events are unconditional but immutable — collect once, healing a
            _heal_get(client, "fixtures/events", {"fixture": fid})  # stale-empty backfill.
        if attempt["fixture_stats"]:
            st = _heal_get(client, "fixtures/statistics", {"fixture": fid})
            if (st.get("response") or []) or (season_cov["fixture_stats"] and not recent):
                new_fp["fixture_stats"] = True

        normalized = {k: new_fp.get(k, False) for k in season_cov}
        # "Updated" (reportable/counted) iff genuinely new, or a stage flipped to collected
        # off the back of a *live* fetch this run. Gating on a live call is what tells a real
        # first landing (players cache was absent → fetched → data) apart from a legacy entry
        # (absent fingerprint) being back-stamped from an already-present cache (a cache hit,
        # no live call) — the latter is silent and doesn't churn the collected-date.
        pulled_live = client.live_requests > live_before
        gained = pulled_live and any(normalized[s] and not prev_fp.get(s)
                                     for s in season_cov)
        if is_new or gained:
            collected_fids.add(fid)
            upd = FixtureUpdate(
                fid=fid,
                home_id=(f["teams"].get("home") or {}).get("id") or 0,
                home=(f["teams"].get("home") or {}).get("name") or "?",
                away_id=(f["teams"].get("away") or {}).get("id") or 0,
                away=(f["teams"].get("away") or {}).get("name") or "?",
                date=fdate,
                score=_score(f),
                status=status,
            )
            for tid in (upd.home_id, upd.away_id):
                if tid:
                    updated[tid].append(upd)
        # Persist whenever the fingerprint moved (a fresh collection, a landed stage, or a
        # legacy entry gaining an explicit fingerprint); a no-op empty re-probe is not
        # re-recorded, so its collected-date stays put.
        if is_new or normalized != prev_fp:
            ledger.record(fid, status, today, normalized)

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
        new_finals=len(collected_fids), newer_season=newer_season,
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

def _select_competitions(only: list[int] | None) -> list[dict]:
    """The Competitions to refresh — all of config, or just the given league ids.

    Preserves config order (so the log reads the same for a scoped run) and errors on any
    id that isn't a tracked Competition rather than silently refreshing fewer than asked.
    """
    if not only:
        return list(config.COMPETITIONS)
    by_id = {c["league_id"]: c for c in config.COMPETITIONS}
    unknown = [lid for lid in only if lid not in by_id]
    if unknown:
        raise SystemExit(
            f"--only: not tracked Competition(s): {' '.join(map(str, unknown))}. "
            f"Choose from config.COMPETITIONS league ids."
        )
    wanted = set(only)
    return [c for c in config.COMPETITIONS if c["league_id"] in wanted]


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
    ap.add_argument("--only", type=int, nargs="+", metavar="LEAGUE_ID",
                    help="refresh only these Competitions' current Season instead of every "
                         "tracked one (a quota-saving scope for frequent partial runs); the "
                         "rebuild/re-scope, if any, still runs over the whole cache")
    args = ap.parse_args(argv)
    comps = _select_competitions(args.only)

    started = datetime.now()
    today = started.date().isoformat()
    client = CachedClient()
    ledger = Ledger()
    results: list[CompResult] = []
    per_comp_calls: dict[str, dict[str, int]] = {}
    outcome = "ok"

    print(f"Refresh — {started:%Y-%m-%d %H:%M:%S}: {len(comps)} competitions"
          f"{' (--only)' if args.only else ''}")
    try:
        for comp in comps:
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
