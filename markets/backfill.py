"""Harvest **Market Candles** from each Exchange's own published history (ADR 0046).

The repairable half of the store. Both Exchanges serve their price history on demand and
keep serving it after settlement — verified again at one-minute granularity while ADR 0046
was written — so everything here is idempotent: running it twice changes nothing, running
it after a gap fills the gap, and running it a year later still works.

**It never resolves anything.** Every request is built from identifiers `markets.watch`
wrote before kickoff: `series_ticker` plus each leg's `market_ticker`, `event_slug` plus
each leg's `token_id`. Resolution sweeps only *open* markets, so by the time this runs the
Fixture it is harvesting is usually invisible to both sweeps (ADR 0043: `KXMLSGAME` showed
31 open against 53 settled, disjoint). A backfill that re-resolved would work perfectly in
testing and return nothing in production.

## Two resolutions, and why both are stored

    3600s   across the run-up — four weeks of Polymarket, days of Kalshi. An hourly bar is
            the right zoom for "what did the market think in the fortnight before this?"
    60s     across the **In-Play Window** only. A goal reprices a market in seconds; at
            hourly resolution it is one step between two flat stretches.

`period_seconds` is part of a Candle's primary key, so the same minute is legitimately
held at both and neither overwrites the other. Asking for a month at one-minute resolution
would be ~43,000 bars a leg for a picture nobody zooms into, and would run into Kalshi's
5,000-candle response ceiling besides.

## What a Polymarket Candle is missing, and why that is not a bug

Polymarket publishes one mid per point and nothing else, so its Candles fill `mid_close`
and leave every OHLC, volume and open-interest column null. That is the Exchange being
quiet, not a read failing — and it is exactly why the **Market Observations** the in-play
poller writes are not redundant with this.

Run:
    uv run python -m markets.backfill                    # everything outstanding
    uv run python -m markets.backfill --fixture 1530142  # one Fixture, both Exchanges
    uv run python -m markets.backfill --force            # re-harvest even if complete
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from psycopg.rows import dict_row

from . import store
from .exchanges import _money, _with_retry

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2"
CLOB_URL = "https://clob.polymarket.com"

#: How far before kickoff the run-up is asked for. Comfortably beyond Polymarket's ~4-week
#: listing horizon; Kalshi ignores the excess because its market did not exist. The same
#: constant `football_blog.track` uses, and for the same reason.
LOOKBACK_DAYS = 60

#: Kalshi returns at most this many candlesticks per request. The run-up at hourly
#: resolution is 1,440 and the In-Play Window at one minute is 165, so nothing here comes
#: close — but a future span that did would be silently truncated, so the guard is explicit.
KALSHI_MAX_CANDLES = 5000

#: The widest `startTs`/`endTs` the Polymarket CLOB will accept. Measured, not documented:
#: 14 days returns 200 and 20 days returns
#: `400 {"error":"invalid filters: 'startTs' and 'endTs' interval is too long"}`.
#: Anything wider must be asked for as `interval=max` and trimmed here — which is what the
#: run-up always does, since its span is 60 days. The In-Play Window is 165 minutes and is
#: always asked for precisely.
CLOB_MAX_BOUNDED_SPAN_S = 14 * 86400


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _at(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _spans(kickoff: datetime, now: datetime) -> tuple[list[tuple[int, int, int]], str]:
    """The `(start, end, period_seconds)` spans to fetch, and the state they leave behind.

    A Fixture whose window has not closed yet gets its run-up only and stays `partial`, so
    the next night picks it up again. The in-play span is fetched once, after the window
    has closed, because a market mid-match is still writing that period.
    """
    runup_from = _ts(kickoff - timedelta(days=LOOKBACK_DAYS))
    in_play_from = _ts(kickoff) - store.IN_PLAY_LEAD_S
    in_play_to = _ts(kickoff) + store.IN_PLAY_TAIL_S
    if _ts(now) < in_play_to:
        end = min(_ts(now), in_play_from)
        spans = [(runup_from, end, store.RESOLUTION_RUNUP_S)] if end > runup_from else []
        return spans, "partial"
    return (
        [(runup_from, in_play_from, store.RESOLUTION_RUNUP_S),
         (in_play_from, in_play_to, store.RESOLUTION_IN_PLAY_S)],
        "complete",
    )


# --------------------------------------------------------------------------- #
# Kalshi                                                                       #
# --------------------------------------------------------------------------- #
def _ohlc(block: Any, prefix: str) -> dict[str, Optional[float]]:
    """A candlestick's `yes_bid` / `yes_ask` / `price` sub-block -> four columns.

    `price` is `{}` on any period with no trade, which is most periods on these series,
    while the book is quoted throughout. An empty block yields four nulls rather than
    zeros — the distinction ADR 0043 corrected ADR 0040 over.
    """
    b = block if isinstance(block, dict) else {}
    return {f"{prefix}_{k}": _money(b.get(f"{k}_dollars"))
            for k in ("open", "high", "low", "close")}


def kalshi_candles(client: httpx.Client, series_ticker: str, market_ticker: str,
                   side: str, start_ts: int, end_ts: int,
                   period_seconds: int) -> list[store.Candle]:
    period_interval = period_seconds // 60
    expected = (end_ts - start_ts) // max(period_seconds, 1)
    if expected > KALSHI_MAX_CANDLES:
        raise ValueError(
            f"{market_ticker}: {expected:,} candles requested at {period_seconds}s, over "
            f"Kalshi's {KALSHI_MAX_CANDLES:,} ceiling — the response would be truncated "
            f"with no error. Narrow the span or coarsen the period.")

    def _get() -> dict:
        r = client.get(
            f"{KALSHI_URL}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts,
                    "period_interval": period_interval},
        )
        r.raise_for_status()
        return r.json()

    body = _with_retry(_get, what=f"kalshi candles {market_ticker}@{period_seconds}s")
    out: list[store.Candle] = []
    for c in body.get("candlesticks") or []:
        end_period = c.get("end_period_ts")
        if end_period is None:
            continue
        bid = _ohlc(c.get("yes_bid"), "yes_bid")
        ask = _ohlc(c.get("yes_ask"), "yes_ask")
        price = _ohlc(c.get("price"), "price")
        mid = (bid["yes_bid_close"] + ask["yes_ask_close"]) / 2.0 \
            if bid["yes_bid_close"] is not None and ask["yes_ask_close"] is not None \
            else None
        out.append(store.Candle(
            side=side,
            # `end_period_ts` is the END of the period; a Candle is filed by its start so
            # that a Kalshi bar and a bucketed Polymarket point covering the same real
            # minute land on the same row rather than an hour apart.
            period_start=_at(int(end_period) - period_seconds),
            period_seconds=period_seconds,
            mid_close=mid,
            volume=_money(c.get("volume_fp")),
            open_interest=_money(c.get("open_interest_fp")),
            **bid, **ask, **price,
        ))
    return out


# --------------------------------------------------------------------------- #
# Polymarket                                                                   #
# --------------------------------------------------------------------------- #
def polymarket_candles(client: httpx.Client, token_id: str, side: str,
                       start_ts: int, end_ts: int,
                       period_seconds: int) -> list[store.Candle]:
    """`mid_close` per bucket from one Yes token's CLOB price history.

    Each point's `p` **is** the mid — for an open market it equals `outcomePrices`, the
    number a **Market Observation** stores — so nothing is recomputed and a Candle and an
    Observation of the same minute are on one scale. Where two points share a bucket the
    later wins, which is the close.

    A span wider than the CLOB accepts is asked for as `interval=max` and trimmed here.
    That is not a fallback that might silently return less: `interval=max` returns the
    token's whole history, which is a superset of any window inside it. The bounded form
    is preferred where it fits only because it moves less over the wire.

    `httpx` is not incidental: the CLOB host sits behind Cloudflare and 403s a
    `Python-urllib` user agent while httpx's default header set passes (ADR 0043).
    """
    bounded = (end_ts - start_ts) <= CLOB_MAX_BOUNDED_SPAN_S
    params: dict[str, Any] = {"market": token_id, "fidelity": period_seconds // 60}
    params.update({"startTs": start_ts, "endTs": end_ts} if bounded
                  else {"interval": "max"})

    def _get() -> dict:
        r = client.get(f"{CLOB_URL}/prices-history", params=params)
        r.raise_for_status()
        return r.json()

    body = _with_retry(_get, what=f"polymarket history {token_id[:12]}…@{period_seconds}s")
    latest: dict[int, tuple[int, float]] = {}
    for point in body.get("history") or []:
        try:
            t, p = int(point["t"]), float(point["p"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (start_ts <= t <= end_ts):
            continue
        bucket = t // period_seconds * period_seconds
        if bucket not in latest or t >= latest[bucket][0]:
            latest[bucket] = (t, p)
    return [store.Candle(side=side, period_start=_at(b), period_seconds=period_seconds,
                         mid_close=p)
            for b, (_t, p) in sorted(latest.items())]


# --------------------------------------------------------------------------- #
# The run                                                                      #
# --------------------------------------------------------------------------- #
def outstanding(conn, now: datetime, fixture_id: Optional[int] = None,
                force: bool = False, limit: Optional[int] = None) -> list[dict]:
    """Enrolled markets with harvesting left to do, soonest kickoff first.

    A `complete` market is skipped unless forced: its spans are closed and re-fetching
    them would return the same bars. `unreachable` is *not* skipped — an Exchange that
    was down last night may answer tonight, and the row says which it was.
    """
    where = ["state = 'tracked'", "kickoff_utc IS NOT NULL"]
    params: list[Any] = []
    if not force:
        where.append("backfill_state <> 'complete'")
    if fixture_id is not None:
        where.append("fixture_id = %s")
        params.append(fixture_id)
    sql = ("SELECT fixture_id, exchange, series_ticker, event_slug, kickoff_utc, "
           "       backfill_state, home_team_name, away_team_name "
           "FROM winner_market WHERE " + " AND ".join(where) +
           " ORDER BY kickoff_utc, fixture_id, exchange")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def backfill_market(conn, client: httpx.Client, row: dict, now: datetime,
                    dry_run: bool = False) -> tuple[int, str]:
    """Harvest one **Winner Market**. Returns `(candles written, new backfill state)`."""
    spans, state = _spans(row["kickoff_utc"], now)
    legs = store.legs_for(conn, row["fixture_id"], row["exchange"])
    if not legs or not spans:
        return 0, row["backfill_state"]

    candles: list[store.Candle] = []
    for leg in legs:
        for start_ts, end_ts, period in spans:
            if row["exchange"] == "kalshi":
                if not (row["series_ticker"] and leg["market_ticker"]):
                    continue
                candles += kalshi_candles(client, row["series_ticker"],
                                          leg["market_ticker"], leg["side"],
                                          start_ts, end_ts, period)
            else:
                if not leg["token_id"]:
                    continue
                candles += polymarket_candles(client, leg["token_id"], leg["side"],
                                              start_ts, end_ts, period)

    # An Exchange that answers with nothing at all for a played Fixture is not "complete",
    # it is unreachable — recording that as a finished harvest would hide the one case a
    # human should look at.
    if not candles:
        state = "unreachable" if state == "complete" else row["backfill_state"]
    if dry_run:
        return len(candles), state

    written = store.write_candles(conn, fixture_id=row["fixture_id"],
                                  exchange=row["exchange"], candles=candles)
    store.set_backfill_state(conn, fixture_id=row["fixture_id"],
                             exchange=row["exchange"], state=state, at=now)
    return written, state


def run(*, now: Optional[datetime] = None, fixture_id: Optional[int] = None,
        force: bool = False, limit: Optional[int] = None,
        dry_run: bool = False) -> dict[str, int]:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    counts: dict[str, int] = defaultdict(int)

    conn = store.get_conn()
    store.init(conn)
    rows = outstanding(conn, now, fixture_id=fixture_id, force=force, limit=limit)
    counts["markets"] = len(rows)

    client = httpx.Client(timeout=30.0)
    try:
        for row in rows:
            try:
                written, state = backfill_market(conn, client, row, now, dry_run)
            except Exception as exc:
                # One market's failure is not the run's. A Fixture skipped tonight is
                # picked up tomorrow; the whole point of an idempotent harvest.
                print(f"  ! {row['fixture_id']} {row['exchange']}: {exc}")
                counts["failed"] += 1
                continue
            counts["candles"] += written
            counts[f"state_{state}"] += 1
    finally:
        client.close()
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m markets.backfill",
        description="Harvest Market Candles from each Exchange's own history (ADR 0046).",
    )
    ap.add_argument("--fixture", type=int, metavar="ID", help="one Fixture only")
    ap.add_argument("--force", action="store_true",
                    help="re-harvest markets already marked complete")
    ap.add_argument("--limit", type=int, metavar="N", help="at most N markets")
    ap.add_argument("--dry-run", action="store_true", help="fetch and count; write nothing")
    args = ap.parse_args(argv)

    started = datetime.now(timezone.utc)
    try:
        counts = run(fixture_id=args.fixture, force=args.force, limit=args.limit,
                     dry_run=args.dry_run)
    finally:
        store.close_conn()

    took = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\n{started:%Y-%m-%d %H:%M:%SZ}  ({took:.0f}s)"
          f"{'  (dry run — nothing written)' if args.dry_run else ''}")
    for key in sorted(counts):
        print(f"  {key:<22} {counts[key]:>9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
