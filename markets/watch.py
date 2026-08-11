"""Enrol every covered **Winner Market**, and observe it once (ADR 0046).

This is the job with the deadline. ADR 0043's sharpest finding is that an Exchange stops
listing a market once it settles — `KXMLSGAME` showed 31 open against 53 settled, and the
two sets are disjoint — so a few hours after kickoff neither sweep can find the market
again. The *history* is still served; the way to reach it is not. A **Winner Market** row
captured here, with `series_ticker` plus each leg's `market_ticker` and `event_slug` plus
each leg's `token_id`, is the only route back to a played Fixture's data. A Fixture never
enrolled cannot be backfilled at any price.

Everything else in this package can be late. This cannot, which is why it runs against
**every covered Fixture** rather than only those with a Publication and a Match Post: the
Fixture nobody chose to write about today is the one somebody wants a chart of next week,
and by then it is too late to decide.

The observation is free. The sweep already holds every leg's priced payload, so writing one
**Market Observation** per resolved market costs no extra request — it just means the store
has an hourly point through the run-up as well as a per-minute one through the match.

## The one thing this needs from outside the Market Store

Kalshi resolves on the **local match date**, so it needs a timezone, and ADR 0041 fixes
that as the Publication's `display_timezone` — the same date that fixes a Match Post's
slug. There is no second source: deriving it from the Competition's country works for MLS
(`USA`) and Liga MX (`Mexico`) and fails exactly where it matters, because Leagues Cup's
country is `World` and Leagues Cup is where six pairs of Fixtures share a kickoff instant.

So if the Editorial Store cannot be reached, the **Kalshi half is skipped and the
Polymarket half runs anyway** — Polymarket matches on the exact kickoff instant and needs
no timezone at all. Guessing a timezone would silently attach the wrong day's market; going
down entirely would lose a Polymarket enrolment that was reachable. Neither is necessary.

Run:
    uv run python -m markets.watch                 # enrol and observe
    uv run python -m markets.watch --dry-run       # resolve and report, write nothing
    uv run python -m markets.watch --days 45       # widen the enrolment horizon
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from football_blog import kalshi, polymarket
from football_blog.postgres import get_conn as published_conn

from . import store
from .exchanges import Sweep, kalshi_sweep, polymarket_sweep, quotes_for

#: How far ahead a Fixture is enrolled. Polymarket lists a Fixture around four weeks out
#: and Kalshi two to five days, so 35 days covers the earlier of the two with slack. A
#: Fixture beyond it is not missed, only not enrolled yet — the next hourly run reaches it.
HORIZON_DAYS = 35

#: How far back to keep re-confirming. A Fixture stays in the sweep while it is in play, so
#: `last_seen_at` keeps moving and the enrolment is re-checked against a market that may
#: have been re-issued. Past this the market is settled and the sweep cannot see it anyway.
GRACE_DAYS = 1

#: Fixture statuses worth enrolling forward. The same tuple `preview.py` uses, and for the
#: same reason: a Fixture that is already Final has nothing left to forecast.
SCHEDULED = ("NS", "TBD")

#: How far back `--settled` reaches. Both Exchanges keep listing a market under a settled
#: status long after the match — 162 settled `KXMLSGAME` markets and 1,325 closed
#: `mls-2025` events on the day this was written — so the limit is what our own Published
#: Store holds, not what they serve.
SETTLED_BACK_DAYS = 120


def display_timezones(competition_ids: tuple[int, ...]) -> dict[int, str]:
    """`competition id -> display_timezone`, from the Editorial Store.

    Returns `{}` rather than raising when PocketBase is unreachable — the caller reads an
    empty map as "no Kalshi this run" and says so, which is the honest degradation. Never
    falls back to a derived timezone: see the module docstring for why the derivation is
    wrong precisely where it would matter.
    """
    from football_blog.pocketbase import PocketBaseClient
    pb = PocketBaseClient()
    try:
        out = {}
        for pub in pb.list_publications():
            cid = pub.get("postgres_competition_id")
            tz = pub.get("display_timezone")
            if cid in competition_ids and tz:
                out[int(cid)] = tz
        return out
    finally:
        pb.close()


def upcoming_fixtures(competition_ids: tuple[int, ...], now: datetime,
                      days: int = HORIZON_DAYS) -> list[tuple]:
    """Scheduled Fixtures in the covered Competitions, inside the enrolment horizon.

    `fixture.date` is naive UTC in the Published Store (ADR 0005), so the bounds are
    stripped of their timezone rather than converted.
    """
    with published_conn().cursor() as cur:
        cur.execute(
            "SELECT id, league_id, date, home_team_id, home_team_name, "
            "       away_team_id, away_team_name "
            "FROM fixture "
            "WHERE league_id = ANY(%s) AND status = ANY(%s) "
            "  AND date >= %s AND date < %s "
            "ORDER BY date",
            (list(competition_ids), list(SCHEDULED),
             (now - timedelta(days=GRACE_DAYS)).replace(tzinfo=None),
             (now + timedelta(days=days)).replace(tzinfo=None)),
        )
        return cur.fetchall()


def played_fixtures(competition_ids: tuple[int, ...], now: datetime,
                    days: int = SETTLED_BACK_DAYS) -> list[tuple]:
    """**Final** Fixtures in the covered Competitions, back as far as `days`.

    The retroactive half. `FINAL` is the same tuple everything else uses — a postponed or
    abandoned Fixture is not Final and has no settled market to find.
    """
    from football.status import FINAL
    with published_conn().cursor() as cur:
        cur.execute(
            "SELECT id, league_id, date, home_team_id, home_team_name, "
            "       away_team_id, away_team_name "
            "FROM fixture "
            "WHERE league_id = ANY(%s) AND status = ANY(%s) "
            "  AND date >= %s AND date < %s "
            "ORDER BY date DESC",
            (list(competition_ids), list(FINAL),
             (now - timedelta(days=days)).replace(tzinfo=None),
             now.replace(tzinfo=None)),
        )
        return cur.fetchall()


def _enrol(conn, sweep: Sweep, market, *, fixture_id: int, competition_id: int,
           kickoff_utc: datetime, home: tuple[int, str], away: tuple[int, str],
           now: datetime, dry_run: bool, observe: bool = True) -> tuple[bool, int]:
    """Write one **Winner Market**, its legs and (usually) one **Market Observation**.

    Returns `(enrolled, quote_rows)`. The legs come from the resolved outcomes, which
    `label_sides` has already rewritten to home/draw/away and sorted — a Track's legs are
    its outcomes', and there is no second place to learn which club is at home.

    `observe=False` on the retroactive path, and this is the important half of it. A
    settled market's published prices are 1 and 0: the **outcome**, not a forecast. Stamped
    with `now` they would enter the store as an Observation made months after the match,
    reading as though somebody had quoted a certainty — the exact confusion ADR 0040's
    freeze exists to prevent, rebuilt in a new place. The **Market Candles** the backfill
    harvests carry their own real timestamps and are safe; the live snapshot is not.
    """
    legs = [
        store.Leg(
            side=o["side"],
            team_id=o.get("team_id"),
            exchange_team_id=str(o.get("kalshi_team") or o.get("polymarket_team") or "")
            or None,
            exchange_team_name=o.get("kalshi_name") or o.get("polymarket_name"),
            market_ticker=o.get("market_ticker"),
            token_id=o.get("token_id"),
            market_slug=o.get("market_slug"),
        )
        for o in market.outcomes
    ]
    quotes = quotes_for(sweep.exchange, market.outcomes, sweep.raw) if observe else []
    if dry_run:
        return True, len(quotes)

    store.upsert_market(
        conn, fixture_id=fixture_id, exchange=sweep.exchange, legs=legs, observed_at=now,
        competition_id=competition_id, kickoff_utc=kickoff_utc,
        local_date=getattr(market, "local_date", None),
        home_team_id=home[0], home_team_name=home[1],
        away_team_id=away[0], away_team_name=away[1],
        series_ticker=getattr(market, "series_ticker", None),
        event_ticker=getattr(market, "event_ticker", None),
        event_slug=getattr(market, "event_slug", None),
        league=getattr(market, "league", None),
        state="tracked",
    )
    written = store.write_observation(
        conn, fixture_id=fixture_id, exchange=sweep.exchange,
        observed_at=now, quotes=quotes,
    )
    return True, written


def run(*, now: Optional[datetime] = None, days: Optional[int] = None,
        settled: bool = False, dry_run: bool = False) -> dict[str, int]:
    """One enrolment pass. `settled=True` reaches backwards instead of forwards."""
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    counts: dict[str, int] = defaultdict(int)
    if days is None:
        days = SETTLED_BACK_DAYS if settled else HORIZON_DAYS

    tz_by_competition = {}
    try:
        tz_by_competition = display_timezones(store.COVERED_COMPETITIONS)
    except Exception as exc:
        print(f"Editorial Store unreachable ({exc}) — Polymarket only this run; "
              f"Kalshi resolves on the local match date and has no timezone to use.")

    k_registry = kalshi.load_registry()
    p_registry = polymarket.load_registry()

    sweeps: list[Sweep] = []
    if tz_by_competition:
        sweeps.append(kalshi_sweep(k_registry,
                                   status="settled" if settled else "open"))
    else:
        counts["kalshi_skipped"] = 1
    sweeps.append(polymarket_sweep(p_registry, closed=settled))

    for sweep in sweeps:
        counts[f"{sweep.exchange}_listed"] = len(sweep.index)
        counts[f"{sweep.exchange}_unmapped"] = len(sweep.unmapped)
        for series in sweep.failed:
            print(f"  ! {sweep.exchange} {series}: swept nothing — enrolment for that "
                  f"Competition is unchanged this run, not emptied")

    fixtures = (played_fixtures(store.COVERED_COMPETITIONS, now, days) if settled
                else upcoming_fixtures(store.COVERED_COMPETITIONS, now, days))
    counts["fixtures"] = len(fixtures)

    conn = None if dry_run else store.get_conn()
    if conn is not None:
        store.init(conn)

    for fid, league_id, date, home_id, home_name, away_id, away_name in fixtures:
        kickoff = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
        for sweep in sweeps:
            if sweep.exchange == "kalshi":
                tz = tz_by_competition.get(league_id)
                if not tz:
                    continue
                market = kalshi.attach(home_id, away_id, kickoff, tz, sweep.index)
                if market is not None:
                    market = kalshi.label_sides(market, home_id, away_id)
            else:
                if not p_registry.covers(league_id):
                    counts["polymarket_not_covered"] += 1
                    continue
                market = polymarket.attach(home_id, away_id, kickoff, sweep.index)
                if market is not None:
                    market = polymarket.label_sides(market, home_id, away_id)

            if market is None:
                counts[f"{sweep.exchange}_not_listed"] += 1
                continue

            _, written = _enrol(
                conn, sweep, market, fixture_id=fid, competition_id=league_id,
                kickoff_utc=kickoff, home=(home_id, home_name), away=(away_id, away_name),
                now=now, dry_run=dry_run, observe=not settled,
            )
            counts[f"{sweep.exchange}_enrolled"] += 1
            counts[f"{sweep.exchange}_quote_rows"] += written
            # Only a finding on the forward path. With --settled nothing is observed on
            # purpose, so counting it as "unquoted" would report the design as a fault.
            if not written and not settled:
                counts[f"{sweep.exchange}_unquoted"] += 1

    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m markets.watch",
        description="Enrol every covered Winner Market and observe it once (ADR 0046).",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and report; write nothing")
    ap.add_argument("--days", type=int, default=None, metavar="N",
                    help=f"how many days to reach (default {HORIZON_DAYS} forward, "
                         f"{SETTLED_BACK_DAYS} back with --settled)")
    ap.add_argument("--settled", action="store_true",
                    help="reach BACKWARDS: enrol Final Fixtures from each Exchange's "
                         "settled/closed listings so their history can be harvested. "
                         "Writes no Market Observation — a settled market quotes 1 and 0, "
                         "which is an outcome and not a forecast. Not on the hourly cron; "
                         "run it once to seed the store, and again if it is ever emptied.")
    args = ap.parse_args(argv)

    started = datetime.now(timezone.utc)
    try:
        counts = run(days=args.days, settled=args.settled, dry_run=args.dry_run)
    finally:
        store.close_conn()

    print(f"\n{started:%Y-%m-%d %H:%M:%SZ}"
          f"{'  (dry run — nothing written)' if args.dry_run else ''}")
    for key in sorted(counts):
        print(f"  {key:<28} {counts[key]:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
