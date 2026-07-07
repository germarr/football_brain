"""Collect all of La Liga for every season in config.SEASONS into the raw cache.

Per season:
  - the league fixture list (1 request -> ~380 fixtures),
  - per-fixture player data (1 request per fixture) -> every player of both teams,
    with goals/assists and per-game stats,
  - per-player biography (1 request per unique player) -> nationality, birth,
    height, weight. Each player is fetched with a season they actually appeared
    in, so the response is never empty (handles mid-season transfers).

Cache-first, so re-running is free and resumes where a stop left off.
Run with:  uv run python -m football.collect
"""
from __future__ import annotations

from collections import defaultdict

from . import config
from .client import CachedClient, QuotaExceeded


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
