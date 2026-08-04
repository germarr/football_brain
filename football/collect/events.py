"""Backfill the match-event timeline (fixtures/events) into the raw cache.

A deliberately separate entrypoint from `collect` (ADR 0007): one events call per
fixture is ~20,780 requests — a full day's run on its own under the 75k/day Ultra
cap. Cache-first and resumable, so a stop (or hitting the daily cap) loses nothing
and a re-run resumes for free. Fixture lists come from cache (already collected by
`collect`), so the only live requests here are the events calls themselves.

Run with:  uv run python -m football.collect.events
"""
from __future__ import annotations

from .. import config, fetch
from ..client import CachedClient, QuotaExceeded


def collect_events() -> None:
    client = CachedClient()
    fixtures_done = 0
    try:
        for league_id, name, season in config.targets():
            fixtures = fetch.fetch_fixtures(client, league_id, season)
            print(f"\n{name} {season}: {len(fixtures)} fixtures")
            for i, fx in enumerate(fixtures, 1):
                fetch.fetch_fixture_events(client, fx["fixture"]["id"])
                fixtures_done += 1
                if i % 50 == 0 or i == len(fixtures):
                    print(f"  events {i}/{len(fixtures)} "
                          f"({client.live_requests} live / {client.cache_hits} cached)")
    except QuotaExceeded as e:
        print(f"\n[stopped] {e}")
    finally:
        print(f"\nFixtures processed: {fixtures_done} | "
              f"live requests: {client.live_requests} | cache hits: {client.cache_hits}")


if __name__ == "__main__":
    collect_events()
