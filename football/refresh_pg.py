"""Pull the latest match results into the cache, then push them to Postgres.

A one-command composition of the two steps that get fresh results into the Postgres
Published Store **without a football.db rebuild** — the reason it works is that
`publish_pg` re-parses straight from the raw cache (ADR 0027), never from football.db:

  1. **Refresh the cache** — the Nightly Refresh frontier collection (ADR 0018) with
     `--no-rebuild`: force-refresh each Competition's current-Season fixture list (a
     cache-first read would freeze it, so a match played since the last run would never
     surface) and collect per-fixture data for the newly-**Final** matches into the raw
     cache. Seconds. It deliberately skips the minutes-long football.db rebuild.
  2. **Publish** — `publish_pg` re-parses the selected Competitions out of that fresh
     cache into a throwaway store and swaps them into Postgres (a wholesale replace).

The cost of skipping the rebuild is that `data/football.db` (and the Viewer) stay stale
until the next full `python -m refresh`, which self-heals them — the Finals collected
here are already ledgered and cached, so that run costs nothing extra for them.

Note the two scopes differ, on purpose: step 1 refreshes the current Season of **every**
Competition in the config (that is what Refresh does — it is cheap, one forced call per
Competition plus enrichment for genuinely new Finals), while step 2 publishes only the
Competitions you name (default: the Liga MX + MLS pair). Publishing is destructive by
omission — an unnamed Competition is *removed* from Postgres (ADR 0027) — so always list
the full set you want present.

Run:
    uv run python -m football.refresh_pg               # Liga MX + MLS (the default pair)
    uv run python -m football.refresh_pg 262 253 71    # ... plus Brasileirao
    uv run python -m football.refresh_pg --all         # publish every tracked Competition
    uv run python -m football.refresh_pg --skip-refresh 262 253   # publish only, no refresh
"""
from __future__ import annotations

import argparse
import time

import refresh

from . import publish_pg


def run(league_ids: list[int] | None = None, use_all: bool = False,
        skip_refresh: bool = False) -> dict[str, int]:
    """Refresh the cache frontier (unless skipped) then publish to Postgres.

    Returns publish_pg's per-table row counts.
    """
    if skip_refresh:
        print("Skipping the cache refresh (--skip-refresh); "
              "publishing from the current cache.\n")
    else:
        print("━━ Step 1/2 · Refreshing the current-Season frontier into the cache "
              "(no football.db rebuild) ━━\n")
        try:
            refresh.main(["--no-rebuild"])
        except SystemExit as e:
            # Refresh exits non-zero on a hit quota or a bad Competition, but the cache
            # is internally consistent even after a partial run (ADR 0018) and publishing
            # is an idempotent re-parse of it — so warn and push what we have rather than
            # block the Postgres update on an incomplete frontier.
            if e.code:
                print(f"\n⚠ Refresh exited non-zero (code {e.code}) — the cache still "
                      "holds whatever it collected; publishing it as-is.")
        print()

    print("━━ Step 2/2 · Publishing to the Postgres Published Store ━━\n")
    return publish_pg.publish(league_ids, use_all)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.refresh_pg",
        description="Refresh the current-Season frontier into the cache, then publish "
                    "selected Competitions to Postgres (ADR 0018 + ADR 0027).",
    )
    ap.add_argument("league_ids", type=int, nargs="*",
                    help=f"provider league ids to publish (default: "
                         f"{' '.join(map(str, publish_pg.DEFAULT_LEAGUE_IDS))} "
                         "— Liga MX, Major League Soccer)")
    ap.add_argument("--all", action="store_true",
                    help="publish every tracked Competition instead")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="skip the cache refresh; publish from the current cache only")
    args = ap.parse_args(argv)
    if args.league_ids and args.all:
        raise SystemExit("Pass league ids or --all, not both.")

    t0 = time.monotonic()
    counts = run(args.league_ids, args.all, args.skip_refresh)
    print(f"\nrefresh_pg done — {sum(counts.values()):,} rows in Postgres "
          f"in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()
