"""The cache-first fetch helpers every collector, parser and publisher shares.

One `fetch_*` per provider endpoint, each a thin cache-first wrapper over
`CachedClient` (ADR 0002): keyed by endpoint + params, read from `data/raw/` before
the network, so a fixture is pulled once, ever. They fetch and shape a response and
do nothing else — no orchestration, no printing, no writing outside the cache.

This module is the collection kernel, imported by nine others (`build.parse`,
`build.scope`, `build.venues`, `collect.events`, `collect.stats`, `collect.teams`,
`onboard.orchestrate`, `publish.pg`, and `refresh.core`). It was previously
`football/collect.py`, which did two jobs at once: these shared helpers *and* a
full-sweep backfill CLI that predated the per-league orchestrator (ADR 0009) and was
superseded by it — not Coverage-gated (ADR 0014), collecting neither match events
(ADR 0007) nor team match stats (ADR 0010). ADR 0031 split the two: the helpers
became this module and the CLI was retired, which also freed the name `collect` for
the subpackage that now holds the real backfill entrypoints.

Fetching only fills Layer 1. Run `python -m football.build.parse` afterwards to model
the cache into `football.db`.
"""
from __future__ import annotations

from .client import CachedClient


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
    the dedicated `football.collect.events` entrypoint, which drives this helper.
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
