"""Bulk-collect the fixtures -> players -> bios -> careers layer for every
competition in config.COMPETITIONS into the raw cache.

config.COMPETITIONS is the MERGED set: the 7 hand-curated built-in leagues plus
every league/cup registered in football/competitions.json (added by football.orchestrate
/ football.cups). This is the original full-sweep collector and predates the
per-league orchestrator (ADR 0009): it does NOT collect match events (ADR 0007) or
team match stats (ADR 0010), and it is NOT Coverage-gated (ADR 0014) — so it fetches
per-player data even for cup / stats-light seasons that have none. Events, team
stats, and coverage-aware collection live in football.orchestrate / football.cups
and the dedicated collect_events / collect_stats modules; the 17 registered
competitions were collected through those, not here. This only fills Layer 1 (the
raw response cache) — run `python -m football.parse` afterwards to model it into
football.db.

Once per competition:
  - the /leagues record (1 request) -> country, ISO code, crest and flag (ADR 0015).
Per (competition, season):
  - the league fixture list (1 request -> ~380 fixtures),
  - per-fixture player data (1 request per fixture) -> every player of both teams,
    with goals/assists and per-game stats.
Per unique player:
  - biography (1 request) -> nationality, birth, height, weight. Each player is
    fetched with a season they actually appeared in, so the response is never empty
    (handles mid-season transfers),
  - career history (1 request, if config.COLLECT_CAREERS) -> every team across their
    whole cross-competition career.

Cache-first, so re-running is free and resumes where a stop left off.
Run with:  uv run python -m football.collect
"""
from __future__ import annotations

from collections import defaultdict

from . import config
from .client import CachedClient, QuotaExceeded


def fetch_league(client: CachedClient, league_id: int) -> dict | None:
    """The provider's /leagues catalogue record for one competition, or None.

    One call per competition (not per season): the record carries the league's
    country, ISO code, crest and flag — the provider metadata parse writes onto the
    Competition row (ADR 0015). Cache-first like every other fetch, so it costs one
    live request the first time and is free thereafter. Returns None when the
    provider has no such league (an empty response), so a caller can skip it rather
    than key into an empty list."""
    resp = client.get("leagues", {"id": league_id}).get("response") or []
    return resp[0] if resp else None


def fetch_fixtures(client: CachedClient, league_id: int, season: int) -> list[dict]:
    """All fixtures in a competition for one season (both teams per fixture)."""
    payload = client.get("fixtures", {"league": league_id, "season": season})
    return payload["response"]


def fetch_fixture_players(client: CachedClient, fixture_id: int) -> dict:
    """Per-player match data for both teams in a fixture."""
    return client.get("fixtures/players", {"fixture": fixture_id})


def fetch_fixture_events(client: CachedClient, fixture_id: int) -> list[dict]:
    """Time-ordered match events for a fixture (goals, cards, subs, VAR).

    Sourced from fixtures/events (ADR 0007). One call per fixture; backfilled by
    the dedicated `football.collect_events` entrypoint, not this module's collect().
    """
    payload = client.get("fixtures/events", {"fixture": fixture_id})
    return payload.get("response") or []


def fetch_fixture_statistics(client: CachedClient, fixture_id: int) -> list[dict]:
    """Both teams' aggregate stat lines for a fixture (possession, shots, xG).

    Sourced from fixtures/statistics: one call per fixture, cache-first. The
    response is a two-element list (one entry per team), each carrying a list of
    {type, value} pairs; empty for a fixture the provider has no stats for.
    """
    payload = client.get("fixtures/statistics", {"fixture": fixture_id})
    return payload.get("response") or []


def fetch_player(client: CachedClient, player_id: int, season: int) -> dict | None:
    """Biography for one player in a season, or None if the provider has no record
    (some players appear in a fixture but return an empty /players response)."""
    payload = client.get("players", {"id": player_id, "season": season})
    resp = payload.get("response") or []
    return resp[0] if resp else None


def fetch_player_teams(client: CachedClient, player_id: int) -> list[dict]:
    """Every team a player has appeared for across their WHOLE career (all
    competitions and national teams), each with the seasons the provider records.
    Powers the Career Stint history (players/teams endpoint); one call per player."""
    payload = client.get("players/teams", {"player": player_id})
    return payload.get("response") or []


def fetch_team(client: CachedClient, team_id: int) -> dict | None:
    """The provider's /teams record for one team id: identity (name, code, country,
    founded, national flag, crest) plus a venue block. Powers the Team Profile
    dossier (ADR 0017); one cache-first call per team. Returns None when the provider
    has no such team (empty response), so the caller can emit a minimal name-only row."""
    resp = client.get("teams", {"id": team_id}).get("response") or []
    return resp[0] if resp else None


def fetch_team_leagues(client: CachedClient, team_id: int) -> list[dict]:
    """Every league a team has ever competed in (the leagues?team endpoint), each with
    its country and the seasons the team played. A Team Profile's representative
    domestic league is picked from this (ADR 0017); one cache-first call per team, made
    only for teams not already covered by our own Fixtures (their league is in cache)."""
    return client.get("leagues", {"team": team_id}).get("response") or []


def players_in_fixture(fixture_players: dict) -> list[tuple[int, dict]]:
    """(team_id, player_block) for every player of both teams in a fixture."""
    out: list[tuple[int, dict]] = []
    for team in fixture_players["response"]:
        tid = team["team"]["id"]
        for pblock in team["players"]:
            out.append((tid, pblock))
    return out


def collect() -> None:
    client = CachedClient()
    # player_id -> a season we know they appeared in (for a valid bio fetch)
    player_season: dict[int, int] = {}
    try:
        # One /leagues record per competition — country/code/crest/flag for the
        # Competition row (ADR 0015). Built-in leagues never went through the
        # orchestrator's _lookup, so this is the only path that caches them.
        for comp in config.COMPETITIONS:
            fetch_league(client, comp["league_id"])
        for league_id, name, season in config.targets():
            fixtures = fetch_fixtures(client, league_id, season)
            print(f"\n{name} {season}: {len(fixtures)} fixtures")
            for i, fx in enumerate(fixtures, 1):
                fid = fx["fixture"]["id"]
                fp = fetch_fixture_players(client, fid)
                for _tid, pblock in players_in_fixture(fp):
                    pid = pblock["player"]["id"]
                    if pid:  # some fixtures list an unknown player as id 0/None
                        player_season.setdefault(pid, season)
                if i % 50 == 0 or i == len(fixtures):
                    print(f"  fixtures {i}/{len(fixtures)} "
                          f"({len(player_season)} players seen, "
                          f"{client.live_requests} live / {client.cache_hits} cached)")

        print(f"\nUnique players across all seasons: {len(player_season)}")
        for j, (pid, season) in enumerate(sorted(player_season.items()), 1):
            fetch_player(client, pid, season)
            if j % 100 == 0 or j == len(player_season):
                print(f"  bios {j}/{len(player_season)} "
                      f"({client.live_requests} live / {client.cache_hits} cached)")

        # Career history: every team each player has ever appeared for, across all
        # competitions (players/teams). One request per player, cache-first.
        # Deferred by default to a separate run (config.COLLECT_CAREERS, ADR 0006).
        if config.COLLECT_CAREERS:
            print(f"\nCareer histories: {len(player_season)} players")
            for j, pid in enumerate(sorted(player_season), 1):
                fetch_player_teams(client, pid)
                if j % 100 == 0 or j == len(player_season):
                    print(f"  careers {j}/{len(player_season)} "
                          f"({client.live_requests} live / {client.cache_hits} cached)")

    except QuotaExceeded as e:
        print(f"\n[stopped] {e}")
    finally:
        print(f"\nTotal live requests this run: {client.live_requests} | "
              f"cache hits: {client.cache_hits}")


if __name__ == "__main__":
    collect()
