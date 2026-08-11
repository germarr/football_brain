"""**Market Observations** through the **In-Play Window** (ADR 0046).

The only job here whose output cannot be reconstructed. Both Exchanges serve their price
history forever — Kalshi's candlesticks at one-minute granularity, Polymarket's
`/prices-history` at `fidelity=1` — so a missed *price* is a re-fetch away. Neither serves
its **book**. Kalshi's `yes_bid_size_fp` / `yes_ask_size_fp` appear in no candlestick, and
Polymarket publishes no history of `bestBid`, `bestAsk`, `spread` or `liquidity` at all.
A minute not read here is a minute nobody can ever read.

So this runs every minute, and does almost nothing on almost every one of them. It asks
the store which enrolled markets have a Fixture inside the In-Play Window right now, and
when the answer is none — which it is for most of the day — it exits without a request, a
write or a line of output. The log therefore holds only the minutes that collected
something, which is what makes 1,440 ticks a day readable.

## It never re-resolves, and that is not only an optimisation

The legs come from `winner_market_leg`, written at enrolment, and the raw payload is
matched to them **by ticker and token**. Resolution — the team registries, the local match
date, the kickoff instant — happens once in `markets.watch` and never again. During play
that is merely cheaper; the moment the market settles it is the difference between having
the data and not (ADR 0043). Keeping the poller on the same path as the backfill means the
path that must work after settlement is exercised every minute before it.

## What one tick costs

One request per **Exchange-series that has something in play** — not per Fixture. Ten
matches kicking off together cost what one costs. A quiet Tuesday costs nothing at all.

Run:
    uv run python -m markets.poll                        # one tick, now
    uv run python -m markets.poll --at 2026-08-15T23:05Z # force an instant, for testing
    uv run python -m markets.poll --verbose              # say so even when idle
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from football_blog import kalshi, polymarket

from . import store
from .exchanges import kalshi_sweep, polymarket_sweep, quotes_for


def _parse_at(value: str) -> datetime:
    """An ISO instant, with `Z` accepted because that is how the rest of the wire spells it."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def tick(*, now: Optional[datetime] = None, dry_run: bool = False) -> dict[str, int]:
    """One poll. Returns counts; an empty window returns `{}` having touched no network."""
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    counts: dict[str, int] = defaultdict(int)

    conn = store.get_conn()
    in_play = store.markets_in_play(conn, now)
    counts["in_play"] = len(in_play)
    if not in_play:
        return {}

    # Sweep only what is actually in play. The series a market names were written at
    # enrolment, so this needs no resolution and no Published Store read.
    k_series = {r["series_ticker"] for r in in_play
                if r["exchange"] == "kalshi" and r["series_ticker"]}
    p_leagues = {r["league"] for r in in_play
                 if r["exchange"] == "polymarket" and r["league"]}

    raw: dict[str, dict[str, dict]] = {}
    if k_series:
        sweep = kalshi_sweep(kalshi.load_registry(), only=k_series)
        raw["kalshi"] = sweep.raw
        counts["kalshi_series_swept"] = len(sweep.swept)
        counts["kalshi_series_failed"] = len(sweep.failed)
    if p_leagues:
        sweep = polymarket_sweep(polymarket.load_registry(), only=p_leagues)
        raw["polymarket"] = sweep.raw
        counts["polymarket_series_swept"] = len(sweep.swept)
        counts["polymarket_series_failed"] = len(sweep.failed)

    for row in in_play:
        exchange = row["exchange"]
        by_key = raw.get(exchange)
        if by_key is None:
            # That Exchange's sweep failed outright. Silence here is an outage, not an
            # empty book, and must not be written as one.
            counts[f"{exchange}_unswept"] += 1
            continue
        legs = store.legs_for(conn, row["fixture_id"], exchange)
        quotes = quotes_for(exchange, legs, by_key)
        if not quotes:
            # The market has left the sweep — settled, or delisted mid-window. Expected at
            # the tail of the window and the reason the backfill exists.
            counts[f"{exchange}_gone"] += 1
            continue
        if dry_run:
            counts[f"{exchange}_rows"] += len(quotes)
            continue
        counts[f"{exchange}_rows"] += store.write_observation(
            conn, fixture_id=row["fixture_id"], exchange=exchange,
            observed_at=now, quotes=quotes,
        )
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m markets.poll",
        description="Collect Market Observations through the In-Play Window (ADR 0046).",
    )
    ap.add_argument("--at", type=_parse_at, metavar="ISO",
                    help="poll as if it were this instant — for testing without a match")
    ap.add_argument("--dry-run", action="store_true", help="fetch and count; write nothing")
    ap.add_argument("--verbose", action="store_true",
                    help="print a line even when nothing is in play (the cron does not)")
    args = ap.parse_args(argv)

    now = args.at or datetime.now(timezone.utc)
    try:
        counts = tick(now=now, dry_run=args.dry_run)
    finally:
        store.close_conn()

    if not counts:
        if args.verbose:
            print(f"{now:%Y-%m-%d %H:%M:%SZ}  nothing in the In-Play Window")
        return 0
    summary = "  ".join(f"{k}={v:,}" for k, v in sorted(counts.items()))
    print(f"{now:%Y-%m-%d %H:%M:%SZ}  {summary}"
          f"{'  (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
