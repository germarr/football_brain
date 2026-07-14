"""Collect one league end to end from a single terminal command (ADR 0009).

Given a provider league id, this:
  1. looks it up in the /leagues catalogue (canonical name, per-season coverage),
  2. registers it as a first-class Competition in football/competitions.json so
     parse.py and future runs keep collecting it,
  3. collects EVERYTHING for it into the raw cache, reusing collect's cache-first
     helpers — fixtures, squad/goals, player bios, career histories, match events,
     team match stats (possession, shots, xG),
  4. rebuilds football.db from the cache so the league lands in SQLite.

Cache-first, so a stop (or hitting the daily cap) loses nothing and a re-run
resumes for free. If collection is cut short, the DB is left untouched (never
built from a half-collected league) and resume instructions are printed.

Run with:
    uv run python -m football.orchestrate 61
    uv run python -m football.orchestrate 61 --name "Ligue 1" --from 2018
    uv run python -m football.orchestrate 61 --no-events --no-rebuild
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import NamedTuple

from . import collect, config, teams
from .client import CachedClient, QuotaExceeded

_BAR_WIDTH = 28


# --- terminal cues ---------------------------------------------------------

def _banner(step: int, total: int, title: str, detail: str) -> None:
    print(f"\n▶ [{step}/{total}] {title} — {detail}", flush=True)


def _bar(done: int, total: int, client: CachedClient) -> None:
    """Redraw an in-place loading bar; newline once the stage completes."""
    total = max(total, 1)
    frac = min(done / total, 1.0)
    filled = int(_BAR_WIDTH * frac)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    sys.stdout.write(
        f"\r  [{bar}] {frac * 100:5.1f}%  {done}/{total}"
        f"  · live {client.live_requests} / cached {client.cache_hits}"
    )
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


# --- league lookup & registration ------------------------------------------

def _lookup(client: CachedClient, league_id: int) -> dict:
    """The /leagues record for one league id (name, type, per-season coverage)."""
    resp = client.get("leagues", {"id": league_id}).get("response") or []
    if not resp:
        raise SystemExit(f"No league with id {league_id} in the provider catalogue.")
    return resp[0]


class SeasonCoverage(NamedTuple):
    """One provider season and the Coverage it exposes (CONTEXT.md, ADR 0014).

    has_player_stats (fixtures.statistics_players) gates the per-player stages
    (squad/goals, bios, careers); has_fixture_stats (fixtures.statistics_fixtures)
    gates team match stats. Fixtures and events are collected regardless.
    """
    year: int
    has_player_stats: bool
    has_fixture_stats: bool


def _seasons(record: dict, frm: int | None, to: int | None) -> list[SeasonCoverage]:
    """Every provider season as a SeasonCoverage, bounded by --from/--to.

    A Competition is no longer gated on per-player Coverage (ADR 0014): all seasons'
    fixtures + events are collected, and the per-season flags decide which of the
    aggregate stages run. A season with neither flag is stats-light — fixtures +
    events only.
    """
    out: list[SeasonCoverage] = []
    for s in record["seasons"]:
        year = s["year"]
        if frm is not None and year < frm:
            continue
        if to is not None and year > to:
            continue
        cov = s.get("coverage", {}).get("fixtures", {}) or {}
        out.append(SeasonCoverage(
            year, bool(cov.get("statistics_players")), bool(cov.get("statistics_fixtures")),
        ))
    return sorted(out)


def _resolve_name(league_id: int, record: dict, override: str | None) -> str:
    """Canonical name: the override, else the provider name; guard collisions."""
    name = override or record["league"]["name"]
    for lid, existing in config.COMPETITION_NAMES.items():
        if existing == name and lid != league_id:
            raise SystemExit(
                f"Canonical name {name!r} is already used by league {lid}. "
                "Pass --name to disambiguate (see CONTEXT.md: two 'Serie A's)."
            )
    return name


def _register(league_id: int, name: str, seasons: list[int], calendar_year: bool,
              comp_type: str = "league") -> None:
    """Upsert the competition into football/competitions.json and refresh config.

    One uniform path for every Competition — league or cup, first-seen or already
    listed (ADR 0019): drop any existing entry with this id, append the new one, write,
    reload. comp_type is the provider's kind ("league" or "cup", ADR 0010); it governs
    how parse.py structures the Season's Fixtures.
    """
    existing = [r for r in config._load_competitions() if r.get("league_id") != league_id]
    existing.append({
        "league_id": league_id, "name": name,
        "seasons": seasons, "calendar_year": calendar_year, "type": comp_type,
    })
    config.COMPETITIONS_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    config.reload_competitions()
    print(f"  registered in {config.COMPETITIONS_FILE.relative_to(config.ROOT)}")


# --- collection stages -----------------------------------------------------

def _collect(client: CachedClient, league_id: int, seasons: list[SeasonCoverage],
             do_careers: bool, do_events: bool, do_stats: bool,
             do_teams: bool = True) -> None:
    """Drive every collection stage for one Competition — league or cup (ADR 0014).

    Fixtures and events cover every season; the per-player stages (squad/goals,
    bios, careers) run only for seasons with player Coverage, and team match stats
    only for seasons with fixture Coverage. When careers run, a Team Profile stage
    enriches the teams they surface (ADR 0017). Reuses collect.fetch_* (cache-first)
    so re-runs are free. Raises QuotaExceeded on hitting the per-run/daily cap; the
    caller decides whether to rebuild.
    """
    do_teams = do_teams and do_careers  # career histories are what surface the teams
    n_stages = 3 + int(do_careers) + int(do_teams) + int(do_events) + int(do_stats)
    all_years = [c.year for c in seasons]
    player_years = [c.year for c in seasons if c.has_player_stats]

    # Stage 1 — fixtures (every season). Track this run's teams: their league is in
    # our own fixtures, so the Team Profile stage skips leagues?team for them.
    _banner(1, n_stages, "Fixtures", f"{len(all_years)} seasons")
    fixtures: list[tuple[int, dict, SeasonCoverage]] = []  # (season, fixture, coverage)
    fixture_team_ids: set[int] = set()
    for i, cov in enumerate(seasons, 1):
        fx = collect.fetch_fixtures(client, league_id, cov.year)
        fixtures.extend((cov.year, f, cov) for f in fx)
        for f in fx:
            for side in ("home", "away"):
                tid = (f["teams"][side] or {}).get("id")
                if tid:
                    fixture_team_ids.add(tid)
        _bar(i, len(seasons), client)
    print(f"    {len(fixtures)} fixtures across {len(all_years)} seasons "
          f"({len(player_years)} with player stats)")

    # Stage 2 — squad & goals (only seasons with player Coverage).
    player_fixtures = [(season, f) for season, f, cov in fixtures if cov.has_player_stats]
    _banner(2, n_stages, "Squad & goals", f"{len(player_fixtures)} fixtures w/ player stats")
    player_season: dict[int, int] = {}
    for i, (season, f) in enumerate(player_fixtures, 1):
        fp = collect.fetch_fixture_players(client, f["fixture"]["id"])
        for _tid, pblock in collect.players_in_fixture(fp):
            pid = pblock["player"]["id"]
            if pid:
                player_season.setdefault(pid, season)
        _bar(i, max(len(player_fixtures), 1), client)
    print(f"    {len(player_season)} unique players seen")

    # Stage 3 — player biographies (one players call per unique player).
    players = sorted(player_season.items())
    _banner(3, n_stages, "Player bios", f"{len(players)} players")
    for j, (pid, season) in enumerate(players, 1):
        collect.fetch_player(client, pid, season)
        _bar(j, max(len(players), 1), client)

    step = 3
    # Stage 4 — career histories (one players/teams call per unique player). Collect
    # every team the histories name — the population the Team Profile stage enriches.
    career_team_ids: set[int] = set()
    if do_careers:
        step += 1
        _banner(step, n_stages, "Career histories", f"{len(players)} players")
        for j, (pid, _season) in enumerate(players, 1):
            for entry in collect.fetch_player_teams(client, pid):
                tid = (entry.get("team") or {}).get("id")
                if tid:
                    career_team_ids.add(tid)
            _bar(j, max(len(players), 1), client)

    # Stage 5 — Team Profiles: enrich the teams the careers surfaced (ADR 0017).
    # /teams for all; /leagues?team only for teams not in this run's fixtures.
    if do_teams:
        step += 1
        _banner(step, n_stages, "Team profiles", f"{len(career_team_ids)} teams")
        teams.enrich(client, career_team_ids, fixture_team_ids,
                     on_progress=lambda i, total: _bar(i, total, client))

    # Stage 6 — match events (every season).
    if do_events:
        step += 1
        _banner(step, n_stages, "Match events", f"{len(fixtures)} fixtures")
        for i, (_season, f, _cov) in enumerate(fixtures, 1):
            collect.fetch_fixture_events(client, f["fixture"]["id"])
            _bar(i, max(len(fixtures), 1), client)

    # Stage 7 — team match stats (only seasons with fixture Coverage).
    if do_stats:
        step += 1
        stat_fixtures = [f for _season, f, cov in fixtures if cov.has_fixture_stats]
        _banner(step, n_stages, "Team match stats",
                f"{len(stat_fixtures)} fixtures w/ fixture stats")
        for i, f in enumerate(stat_fixtures, 1):
            collect.fetch_fixture_statistics(client, f["fixture"]["id"])
            _bar(i, max(len(stat_fixtures), 1), client)


# --- entrypoint ------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.orchestrate",
        description="Collect one league end to end and register it as a Competition.",
    )
    ap.add_argument("league_id", type=int, help="provider league id, e.g. 61 (Ligue 1)")
    ap.add_argument("--name", help="canonical name override (default: provider name)")
    ap.add_argument("--from", dest="from_season", type=int, help="earliest season to collect")
    ap.add_argument("--to", dest="to_season", type=int, help="latest season to collect")
    ap.add_argument("--calendar-year", action="store_true",
                    help="label seasons as a single calendar year (e.g. Brasileirão)")
    ap.add_argument("--no-careers", action="store_true", help="skip career histories")
    ap.add_argument("--no-teams", action="store_true",
                    help="skip Team Profile enrichment (teams the careers surface)")
    ap.add_argument("--no-events", action="store_true", help="skip the match-event timeline")
    ap.add_argument("--no-stats", action="store_true",
                    help="skip team match stats (possession, shots, xG)")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="collect only; do not rebuild football.db")
    ap.add_argument("--no-publish", action="store_true",
                    help="do not refresh the Viewer's serve.db after the rebuild (ADR 0023)")
    args = ap.parse_args(argv)

    client = CachedClient()

    print(f"Looking up league {args.league_id} in the provider catalogue …")
    record = _lookup(client, args.league_id)
    name = _resolve_name(args.league_id, record, args.name)
    seasons = _seasons(record, args.from_season, args.to_season)
    if not seasons:
        raise SystemExit(
            f"{name} (id {args.league_id}) has no seasons in the requested range."
        )
    country = record["country"]["name"]
    player_years = [c.year for c in seasons if c.has_player_stats]
    print(f"  {name}  ({country}, {record['league']['type']})")
    print(f"  seasons: {seasons[0].year}–{seasons[-1].year}  ({len(seasons)} seasons, "
          f"{len(player_years)} with player stats)")

    _register(args.league_id, name, [c.year for c in seasons], args.calendar_year)

    try:
        _collect(
            client, args.league_id, seasons,
            do_careers=not args.no_careers, do_events=not args.no_events,
            do_stats=not args.no_stats, do_teams=not args.no_teams,
        )
    except QuotaExceeded as e:
        print(f"\n[stopped] {e}")
        print("Collection is incomplete, so football.db was NOT rebuilt. "
              "Re-run the same command to resume for free.")
        print(f"\nRequests this run: live {client.live_requests} / "
              f"cached {client.cache_hits}")
        return

    print(f"\nCollection complete for {name}: "
          f"live {client.live_requests} / cached {client.cache_hits} requests.")

    if args.no_rebuild:
        print("Skipping DB rebuild (--no-rebuild). Run `python -m football.parse` "
              "when ready.")
        return

    # Rebuild the modeled store from cache (drops & rebuilds all leagues; the new
    # one is now a config target). Import here so the competitions-file write above is seen.
    from . import parse
    print("\n▶ Rebuilding football.db from the raw cache …")
    parse.build()

    # Refresh the Viewer's serving store so the new league shows up without a manual
    # publish step (ADR 0023). Non-fatal — a publish hiccup never sinks the collection.
    if not args.no_publish:
        from web.publish import publish_after_build
        publish_after_build()


if __name__ == "__main__":
    main()
