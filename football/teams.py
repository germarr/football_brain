"""Enrich the Team Profile directory: fetch club identity and league for every team
a player's Career Stint touches (ADR 0017).

For each career team we cache the provider's /teams record (name, country, founded,
national flag, crest); for teams **not** already covered by our own Fixtures we also
cache /leagues?team, from which parse picks the representative domestic league. This
only fills Layer 1 (the raw response cache) — `parse.build()` reads it, cache-only, to
build the `teamprofile` table.

Two entry points:
  - `enrich(...)` — the shared driver, called incrementally by football.orchestrate
    for the teams a single league run surfaces (cache-first, so repeats are free).
  - `python -m football.teams` — the full sweep: read every career team from
    football.db, backfill any missing enrichment, then (unless --no-rebuild) rebuild
    the DB so the profiles land in SQLite.

Cache-first and quota-guarded like every other collector: a stop loses nothing and a
re-run resumes for free.

Run with:
    uv run python -m football.teams                 # full sweep + rebuild
    uv run python -m football.teams --no-rebuild     # collect only
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable

from . import collect, config
from .client import CachedClient, QuotaExceeded

_BAR_WIDTH = 28


def _bar(done: int, total: int, client: CachedClient, start: float) -> None:
    """Redraw an in-place loading bar with percentage, request counts and a rough
    ETA (extrapolated from the average time per team so far); newline on completion.
    Mirrors orchestrate._bar, kept local to avoid an import cycle."""
    total = max(total, 1)
    frac = min(done / total, 1.0)
    filled = int(_BAR_WIDTH * frac)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    eta = ""
    elapsed = time.monotonic() - start
    if 0 < done < total and elapsed > 0:
        remaining = int(elapsed / done * (total - done))
        eta = f"  ETA {remaining // 60}:{remaining % 60:02d}"
    sys.stdout.write(
        f"\r  [{bar}] {frac * 100:5.1f}%  {done}/{total}"
        f"  · live {client.live_requests} / cached {client.cache_hits}{eta}"
    )
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def enrich(
    client: CachedClient,
    team_ids: Iterable[int],
    known_team_ids: set[int],
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Cache /teams for every team, and /leagues?team for the out-of-scope ones.

    `known_team_ids` are teams whose league is already in our Fixtures (parse resolves
    those from cache, ADR 0017), so their leagues?team call is skipped. Cache-first —
    already-fetched teams cost nothing. Raises QuotaExceeded on hitting the per-run /
    daily cap, having cached everything up to that point (a resume is free). Returns
    the number of teams processed before returning/raising."""
    ids = sorted({t for t in team_ids if t})
    for i, tid in enumerate(ids, 1):
        collect.fetch_team(client, tid)
        if tid not in known_team_ids:
            collect.fetch_team_leagues(client, tid)
        if on_progress:
            on_progress(i, len(ids))
    return len(ids)


def _career_and_known_teams(db_path) -> tuple[list[int], set[int]]:
    """(all career team ids, tracked-fixture team ids) read from football.db.

    Career teams are every distinct PlayerTeam.team_id; the tracked set is the `Team`
    table (both sides of every collected Fixture), whose leagues parse resolves from
    cache — so they skip the leagues?team call."""
    con = sqlite3.connect(db_path)
    try:
        career = [r[0] for r in con.execute("SELECT DISTINCT team_id FROM playerteam")]
        known = {r[0] for r in con.execute("SELECT id FROM team")}
    finally:
        con.close()
    return career, known


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.teams",
        description="Backfill the Team Profile directory for every career team (ADR 0017).",
    )
    ap.add_argument("--no-rebuild", action="store_true",
                    help="collect only; do not rebuild football.db")
    args = ap.parse_args(argv)

    if not config.DB_PATH.exists():
        raise SystemExit(
            f"No {config.DB_PATH.relative_to(config.ROOT)} yet — run a collection + "
            "`python -m football.parse` first so there are career teams to enrich."
        )

    career, known = _career_and_known_teams(config.DB_PATH)
    out_of_scope = [t for t in career if t not in known]
    print(f"Career teams: {len(career)}  ·  tracked (league from cache): {len(known & set(career))}"
          f"  ·  out-of-scope (need leagues?team): {len(out_of_scope)}")

    client = CachedClient()
    start = time.monotonic()

    def _progress(i: int, total: int) -> None:
        if i % 20 == 0 or i == total:
            _bar(i, total, client, start)

    try:
        enrich(client, career, known, on_progress=_progress)
    except QuotaExceeded as e:
        print(f"\n[stopped] {e}")
        print("Enrichment is incomplete, so football.db was NOT rebuilt. "
              "Re-run to resume for free.")
        print(f"\nRequests this run: live {client.live_requests} / cached {client.cache_hits}")
        return

    print(f"\nEnrichment complete: live {client.live_requests} / cached {client.cache_hits} requests.")

    if args.no_rebuild:
        print("Skipping DB rebuild (--no-rebuild). Run `python -m football.parse` when ready.")
        return

    from . import parse
    print("\n▶ Rebuilding football.db from the raw cache …")
    parse.build()


if __name__ == "__main__":
    main()
