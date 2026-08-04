"""Pull the latest match results into the cache, then push them to Postgres.

A one-command composition of the two steps that get fresh results into the Postgres
Published Store **without a football.db rebuild** — the reason it works is that
`publish_pg` re-parses straight from the raw cache (ADR 0027), never from football.db:

  1. **Refresh the cache** — the Nightly Refresh frontier collection (ADR 0018) with
     `--no-rebuild`: force-refresh each Competition's current-Season fixture list (a
     cache-first read would freeze it, so a match played since the last run would never
     surface) and collect per-fixture data for the newly-**Final** matches into the raw
     cache. Seconds. It deliberately skips the minutes-long football.db rebuild.
  2. **Publish** — by default a **delta** (ADR 0028): `pg.delta_publish` re-parses only
     the selected Competitions' current Season from the fresh cache and applies just the Finals
     whose data changed since the last publish, against the live Postgres tables. Sub-second and
     additive. `--full` instead runs the wholesale replace (`pg.publish`) — the reset used
     for first migration, a venue-registry re-baseline, or dropping a provider-retracted Final.

The cost of skipping the rebuild is that `data/football.db` (and the Viewer) stay stale
until the next full `python -m refresh`, which self-heals them — the Finals collected
here are already ledgered and cached, so that run costs nothing extra for them.

Step 1 scopes the frontier collection to exactly the Competitions step 2 will publish
(via Refresh's `--only`), so a run makes one forced /leagues + fixtures pair per *published*
Competition rather than for all ~42 tracked — the quota win that makes frequent runs cheap.
`--all` publishes every tracked Competition, so it refreshes all of them (no `--only`).
Publishing is destructive by omission — an unnamed Competition is *removed* from Postgres
(ADR 0027) — so always list the full set you want present.

The trade-off vs. refreshing everything: the un-refreshed Competitions' current Seasons stay
frozen in the cache until the next full `python -m refresh`, which force-refreshes and
self-heals them — so nothing is lost, only deferred to that run's expense instead of this one.

Run:
    uv run python -m football.publish.delta               # Liga MX + MLS delta (the default pair)
    uv run python -m football.publish.delta 262 253 71    # ... plus Brasileirao
    uv run python -m football.publish.delta --full        # wholesale replace (reset / re-baseline)
    uv run python -m football.publish.delta --all         # publish every tracked Competition (delta)
    uv run python -m football.publish.delta --skip-refresh 262 253   # publish only, no refresh
"""
from __future__ import annotations

import argparse
import time

import refresh

from . import pg


def run(league_ids: list[int] | None = None, use_all: bool = False,
        skip_refresh: bool = False, full: bool = False) -> dict[str, int]:
    """Refresh the cache frontier (unless skipped) then publish to Postgres.

    Publishes a delta by default (ADR 0028); `full=True` runs the wholesale replace.
    Returns publish_pg's per-table row counts.
    """
    if skip_refresh:
        print("Skipping the cache refresh (--skip-refresh); "
              "publishing from the current cache.\n")
    else:
        print("━━ Step 1/2 · Refreshing the current-Season frontier into the cache "
              "(no football.db rebuild) ━━\n")
        # Scope the frontier to exactly the Competitions we're about to publish (the
        # quota win — one forced /leagues + fixtures pair per Competition, so refreshing
        # only the published pair instead of all ~42 is ~40 fewer forced calls per run).
        # --all publishes everything, so it refreshes everything (no --only).
        refresh_argv = ["--no-rebuild"]
        if not use_all:
            scoped = [str(lid) for lid in (league_ids or pg.DEFAULT_LEAGUE_IDS)]
            refresh_argv += ["--only", *scoped]
        try:
            refresh.main(refresh_argv)
        except SystemExit as e:
            # Refresh exits non-zero on a hit quota or a bad Competition, but the cache
            # is internally consistent even after a partial run (ADR 0018) and publishing
            # is an idempotent re-parse of it — so warn and push what we have rather than
            # block the Postgres update on an incomplete frontier.
            if e.code:
                print(f"\n⚠ Refresh exited non-zero (code {e.code}) — the cache still "
                      "holds whatever it collected; publishing it as-is.")
        print()

    label = "wholesale replace" if full else "delta"
    print(f"━━ Step 2/2 · Publishing to the Postgres Published Store ({label}) ━━\n")
    if full:
        return pg.publish(league_ids, use_all)
    return pg.delta_publish(league_ids, use_all)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.publish.delta",
        description="Refresh the current-Season frontier into the cache, then publish "
                    "selected Competitions to Postgres (ADR 0018 + ADR 0027).",
    )
    ap.add_argument("league_ids", type=int, nargs="*",
                    help=f"provider league ids to publish (default: "
                         f"{' '.join(map(str, pg.DEFAULT_LEAGUE_IDS))} "
                         "— Liga MX, Major League Soccer)")
    ap.add_argument("--all", action="store_true",
                    help="publish every tracked Competition instead")
    ap.add_argument("--full", action="store_true",
                    help="wholesale replace instead of the default delta (reset / re-baseline)")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="skip the cache refresh; publish from the current cache only")
    args = ap.parse_args(argv)
    if args.league_ids and args.all:
        raise SystemExit("Pass league ids or --all, not both.")

    t0 = time.monotonic()
    counts = run(args.league_ids, args.all, args.skip_refresh, args.full)
    print(f"\nrefresh_pg done — {sum(counts.values()):,} rows in Postgres "
          f"in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()
