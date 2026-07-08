"""Backfill team match stats (fixtures/statistics) into the raw cache.

A deliberately separate entrypoint from `collect`, mirroring `collect_events`: one
statistics call per fixture is ~one request per collected fixture (tens of
thousands across all leagues), enough to want its own resumable run under the
daily cap. Cache-first and resumable, so a stop (or hitting the daily cap) loses
nothing and a re-run resumes for free. Fixture lists come from cache (already
collected by `collect`), so the only live requests here are the statistics calls
themselves.

Run with:  uv run python -m football.collect_stats
Then rebuild:  uv run python -m football.parse
"""
from __future__ import annotations

from . import collect, config
from .client import CachedClient, QuotaExceeded


def collect_stats() -> None:
    client = CachedClient()
    fixtures_done = 0
    try:
        for league_id, name, season in config.targets():
            fixtures = collect.fetch_fixtures(client, league_id, season)
            print(f"\n{name} {season}: {len(fixtures)} fixtures")
            for i, fx in enumerate(fixtures, 1):
                collect.fetch_fixture_statistics(client, fx["fixture"]["id"])
                fixtures_done += 1
                if i % 50 == 0 or i == len(fixtures):
                    print(f"  stats {i}/{len(fixtures)} "
                          f"({client.live_requests} live / {client.cache_hits} cached)")
    except QuotaExceeded as e:
        print(f"\n[stopped] {e}")
    finally:
        print(f"\nFixtures processed: {fixtures_done} | "
              f"live requests: {client.live_requests} | cache hits: {client.cache_hits}")


if __name__ == "__main__":
    collect_stats()
