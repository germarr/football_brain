"""Collect one cup (group + knockout) end to end from a single command (ADR 0010).

Like `football.orchestrate`, but for cup-type Competitions such as the Champions
League (id 2) and the World Cup (id 1). A cup is one Tournament per Season played
over qualifying → group → knockout Phases, so this:
  1. looks the id up in /leagues and refuses anything the provider doesn't call a
     Cup (use `football.orchestrate` for leagues),
  2. registers it as a `type="cup"` Competition in football/competitions.json so
     parse.py tags each Fixture's Phase and future runs keep collecting it,
  3. hands off to the shared, Coverage-gated collector (`orchestrate._collect`,
     ADR 0014): EVERY provider season's fixtures and match events, the per-player
     stages (squad/goals, bios, careers) only for seasons with player Coverage, and
     team match stats (possession, shots, xG) only for seasons with fixture Coverage,
  4. rebuilds football.db from the cache.

Shares the same cache-first client and 150k/day cap as every other collector, so
it simply spends what is left of the day's budget and resumes for free after a
stop. If cut short the DB is left untouched and resume instructions are printed.

Run with:
    uv run python -m football.cups 2                  # Champions League
    uv run python -m football.cups 1 --calendar-year  # World Cup (single-year seasons)
    uv run python -m football.cups 2 --from 2015 --no-rebuild
"""
from __future__ import annotations

import argparse

from . import orchestrate
from .client import CachedClient, QuotaExceeded


# --- entrypoint ------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.cups",
        description="Collect one cup (group + knockout) end to end and register it.",
    )
    ap.add_argument("league_id", type=int,
                    help="provider cup league id, e.g. 2 (Champions League), 1 (World Cup)")
    ap.add_argument("--name", help="canonical name override (default: provider name)")
    ap.add_argument("--from", dest="from_season", type=int, help="earliest season to collect")
    ap.add_argument("--to", dest="to_season", type=int, help="latest season to collect")
    ap.add_argument("--calendar-year", action="store_true",
                    help="label seasons as a single calendar year (e.g. the World Cup)")
    ap.add_argument("--no-careers", action="store_true", help="skip career histories")
    ap.add_argument("--no-teams", action="store_true",
                    help="skip Team Profile enrichment (teams the careers surface)")
    ap.add_argument("--no-events", action="store_true", help="skip the match-event timeline")
    ap.add_argument("--no-stats", action="store_true",
                    help="skip team match stats (possession, shots, xG)")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="collect only; do not rebuild football.db")
    args = ap.parse_args(argv)

    client = CachedClient()

    print(f"Looking up cup {args.league_id} in the provider catalogue …")
    record = orchestrate._lookup(client, args.league_id)
    if record["league"]["type"].lower() != "cup":
        raise SystemExit(
            f"League {args.league_id} ({record['league']['name']}) is a "
            f"{record['league']['type']!r}, not a Cup. Use `football.orchestrate` for leagues."
        )
    name = orchestrate._resolve_name(args.league_id, record, args.name)
    seasons = orchestrate._seasons(record, args.from_season, args.to_season)
    if not seasons:
        raise SystemExit(
            f"{name} (id {args.league_id}) has no seasons in the requested range."
        )
    country = record["country"]["name"]
    player_years = [c.year for c in seasons if c.has_player_stats]
    print(f"  {name}  ({country}, cup)")
    print(f"  seasons: {seasons[0].year}–{seasons[-1].year}  ({len(seasons)} seasons, "
          f"{len(player_years)} with player stats)")

    orchestrate._register(args.league_id, name, [c.year for c in seasons],
                          args.calendar_year, comp_type="cup")

    try:
        orchestrate._collect(
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

    from . import parse
    print("\n▶ Rebuilding football.db from the raw cache …")
    parse.build()


if __name__ == "__main__":
    main()
