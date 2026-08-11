"""**Market Tracks** — what each **Exchange** thought, hour by hour, into kickoff (ADR 0043).

A Market Track is the ordered hourly series of one Exchange's **Market Probabilities**
for one **Winner Market**. There is one per Winner Market, so at most two per Fixture,
and they are drawn side by side and never merged.

## It is fetched, and nothing here stores it

Both Exchanges serve their own history on demand and keep serving it after settlement —
142 candlesticks on a settled Kalshi market, 323 points on a closed Polymarket one. So a
Track costs six unauthenticated GETs at read time and introduces no store, no cadence,
no record and no freeze. A settled **Match Preview**'s graph stays drawable forever
because the Exchange kept the data, not because we copied it.

This corrects the premise ADR 0040 froze on. `preview.py` said Kalshi's candlesticks come
back empty for these series; they return hourly bid/ask OHLC, on open and settled markets
alike. The freeze stands for a different reason: a **Quote** does not stop at kickoff, it
converges on the result and settles at 1 and 0, so re-running a settled Fixture would
overwrite a *forecast* with an *outcome*.

## Two transports, one rule

Kalshi publishes bid/ask OHLC per period and Polymarket publishes a single mid per point,
so the legs arrive differently and are reduced to the same thing: **one mid per outcome
per hour**. Then, at each hour:

  - all three legs present -> normalise across them, exactly as the live card does;
  - any leg missing -> a **gap**, never an interpolation or a carried-forward value.

Bucketing to the hour is required rather than tidy. Kalshi's timestamps are already
hour-aligned (`ts % 3600 == 0`); Polymarket's drift by 3–372 seconds and share an exact
timestamp **0% of the time**, so three raw series would intersect nowhere at all.

## The two Tracks are not the same length, and that is the point

Polymarket lists a Fixture around four weeks out, Kalshi two to five days. On one MLS
game: Polymarket 743 points over 30d 23h, Kalshi 5 over 4h. Both are drawn on **one
shared axis** — `TrackPair` computes it — and the span before an Exchange listed the
Fixture is reported as `listed_from` so the surface can mark it *not listed* rather than
leaving it blank. When an Exchange began having an opinion is part of what the pair shows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import httpx

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2"
CLOB_URL = "https://clob.polymarket.com"

HOUR = 3600

#: How far back a Track is asked for. Comfortably beyond Polymarket's ~4-week listing
#: horizon; Kalshi ignores the excess because its market did not exist.
LOOKBACK_DAYS = 60

#: The order the three legs are always in, on both Exchanges and in every payload here.
SIDES = ("home", "draw", "away")


def _bucket(ts: int) -> int:
    """Unix seconds -> the hour it belongs to. The only alignment either Exchange gets."""
    return int(ts) // HOUR * HOUR


@dataclass
class TrackPoint:
    """One hour's three **Market Probabilities**, normalised within one Exchange."""
    t: int                              # unix seconds, hour-aligned
    home: float
    draw: float
    away: float

    def as_dict(self) -> dict[str, Any]:
        return {"t": self.t, "home": round(self.home, 4),
                "draw": round(self.draw, 4), "away": round(self.away, 4)}


@dataclass
class MarketTrack:
    """One Exchange's Track for one Winner Market.

    `state` is the honest vocabulary of ADR 0043, and the three absences are different
    things: **not_covered** is permanent and needs no human, **not_listed** resolves
    itself when the Exchange opens the market, and **unmapped** is the only one anybody
    can act on.
    """
    exchange: str
    points: list[TrackPoint] = field(default_factory=list)
    state: str = "tracked"              # tracked | not_listed | not_covered | unmapped
    #: First hour this Exchange quoted all three legs. None when it never did.
    listed_from: Optional[int] = None
    #: Hours inside the listed span where a leg was missing — gaps, drawn as breaks.
    gaps: int = 0

    @property
    def span_hours(self) -> int:
        return 0 if len(self.points) < 2 else (self.points[-1].t - self.points[0].t) // HOUR

    def as_dict(self) -> dict[str, Any]:
        return {"exchange": self.exchange, "state": self.state,
                "listed_from": self.listed_from, "gaps": self.gaps,
                "span_hours": self.span_hours,
                "points": [p.as_dict() for p in self.points]}


def normalise(mids_by_side: dict[str, dict[int, float]]) -> tuple[list[TrackPoint], int]:
    """Three per-side `{bucket: mid}` maps -> normalised points, and the gap count.

    A bucket missing any leg is dropped. Normalising over two of three legs would invent
    a distribution nobody quoted — the same refusal the live Market Probability makes.

    Bucket-size-agnostic on purpose: the keys are whatever the caller bucketed to, so the
    hourly Track this module fetches and the per-minute one `markets.api` reads out of the
    **Market Store** (ADR 0046) share one implementation of the gap rule. There must not
    be two — the project has `football/status.py` because six copies of four strings
    agreed right up until two drifted.
    """
    hours = sorted(set().union(*(set(m) for m in mids_by_side.values())) if mids_by_side else [])
    points: list[TrackPoint] = []
    gaps = 0
    for h in hours:
        legs = [mids_by_side.get(s, {}).get(h) for s in SIDES]
        if any(v is None for v in legs):
            gaps += 1
            continue
        total = sum(legs)
        if total <= 0:
            gaps += 1
            continue
        points.append(TrackPoint(t=h, home=legs[0] / total,
                                 draw=legs[1] / total, away=legs[2] / total))
    # Gaps before the Exchange listed anything are not gaps, they are absence.
    if points:
        first = points[0].t
        gaps = sum(1 for h in hours if h > first and h not in {p.t for p in points})
    else:
        gaps = 0
    return points, gaps


# --------------------------------------------------------------------------- #
# Kalshi                                                                       #
# --------------------------------------------------------------------------- #
def _kalshi_mids(client: httpx.Client, series_ticker: str, market_ticker: str,
                 start_ts: int, end_ts: int) -> dict[int, float]:
    """`{hour: mid}` from one market's candlesticks.

    The mid comes from `yes_bid.close_dollars` and `yes_ask.close_dollars`. The
    candlestick's own `price` block is **not** used: it is `{}` on any period with no
    trade, which is most of them on these series, while the book is quoted throughout.
    A period missing either side of the book yields nothing for that hour.
    """
    r = client.get(f"{KALSHI_URL}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
                   params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": 60})
    r.raise_for_status()
    out: dict[int, float] = {}
    for c in r.json().get("candlesticks") or []:
        bid = (c.get("yes_bid") or {}).get("close_dollars")
        ask = (c.get("yes_ask") or {}).get("close_dollars")
        if bid in (None, "") or ask in (None, ""):
            continue
        try:
            out[_bucket(c["end_period_ts"])] = (float(bid) + float(ask)) / 2.0
        except (TypeError, ValueError, KeyError):
            continue
    return out


@dataclass
class StoredMarket:
    """A **Winner Market** rebuilt from a stored block, for a Fixture already played.

    A Track is fetched from the Exchange, but *finding* the market is a resolution step
    — and resolution sweeps only open markets. A few hours after kickoff Kalshi moves
    the market to `settled` and Polymarket marks the event `closed`, at which point
    neither sweep can see it and a settled **Match Preview** would lose its graph.

    The frozen record is the answer: it already carries the identifiers the sweep would
    have produced (`series_ticker` plus each leg's `market_ticker`; each leg's
    `token_id`). Reading them back skips resolution entirely, which is both correct
    after settlement and cheaper before it.
    """
    outcomes: list[dict]
    series_ticker: Optional[str] = None
    event_slug: Optional[str] = None
    league: Optional[str] = None


def stored_market(block: Optional[dict]):
    """A stored `market_kalshi` / `market_polymarket` block -> something the Track
    builders can read, or None when the block names no market."""
    if not block or not block.get("outcomes"):
        return None
    return StoredMarket(outcomes=block["outcomes"],
                        series_ticker=block.get("series_ticker"),
                        event_slug=block.get("event_slug"),
                        league=block.get("league"))


def kalshi_track(market, *, client: Optional[httpx.Client] = None,
                 now: Optional[datetime] = None) -> MarketTrack:
    """The Kalshi **Market Track** for a resolved `kalshi.WinnerMarket`.

    Three GETs, one per leg. `market.outcomes` must already carry home/draw/away sides —
    `kalshi.label_sides` does that — because a Track's legs are its outcomes' and there
    is no second place to learn which club is at home.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    now = now or datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    start_ts = int((now - timedelta(days=LOOKBACK_DAYS)).timestamp())
    try:
        mids: dict[str, dict[int, float]] = {}
        for o in market.outcomes:
            side = o.get("side")
            ticker = o.get("market_ticker")
            if side not in SIDES or not ticker:
                continue
            mids[side] = _kalshi_mids(client, market.series_ticker, ticker,
                                      start_ts, end_ts)
    finally:
        if owns:
            client.close()

    points, gaps = normalise(mids)
    return MarketTrack(exchange="kalshi", points=points, gaps=gaps,
                       state="tracked" if points else "not_listed",
                       listed_from=points[0].t if points else None)


# --------------------------------------------------------------------------- #
# Polymarket                                                                   #
# --------------------------------------------------------------------------- #
def _polymarket_mids(client: httpx.Client, token_id: str) -> dict[int, float]:
    """`{hour: mid}` from one Yes token's CLOB price history.

    Each point's `p` **is** the mid — for an open market it equals `outcomePrices`,
    which is `(bestBid + bestAsk) / 2` — so nothing is recomputed and a Track's last
    point equals the number on the card. Where two points share an hour the later wins.
    """
    r = client.get(f"{CLOB_URL}/prices-history",
                   params={"market": token_id, "interval": "max", "fidelity": 60})
    r.raise_for_status()
    out: dict[int, float] = {}
    for point in r.json().get("history") or []:
        try:
            out[_bucket(point["t"])] = float(point["p"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def polymarket_track(market, *, client: Optional[httpx.Client] = None) -> MarketTrack:
    """The Polymarket **Market Track** for a resolved `polymarket.WinnerMarket`.

    Three GETs, one per Yes token. `httpx` is not incidental: the CLOB host 403s
    (`error code: 1010`) a `Python-urllib` user agent and passes httpx's untouched.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        mids: dict[str, dict[int, float]] = {}
        for o in market.outcomes:
            side, token = o.get("side"), o.get("token_id")
            if side not in SIDES or not token:
                continue
            mids[side] = _polymarket_mids(client, token)
    finally:
        if owns:
            client.close()

    points, gaps = normalise(mids)
    return MarketTrack(exchange="polymarket", points=points, gaps=gaps,
                       state="tracked" if points else "not_listed",
                       listed_from=points[0].t if points else None)


# --------------------------------------------------------------------------- #
# The pair, and the axis they share                                            #
# --------------------------------------------------------------------------- #
@dataclass
class TrackPair:
    """Two Market Tracks and the one time axis both are drawn on.

    The axis is shared deliberately. The two Tracks are routinely 190x different in
    span, and rendering a month-long drift and a four-hour wobble at the same width
    invites the same false read the glossary already forbids for volume. So both panels
    span `t_from`..`t_to`, and each Track's `listed_from` says where its *not listed*
    region ends.
    """
    fixture_id: int
    kickoff: Optional[int]
    kalshi: MarketTrack
    polymarket: MarketTrack

    @property
    def t_from(self) -> Optional[int]:
        firsts = [t.points[0].t for t in (self.kalshi, self.polymarket) if t.points]
        return min(firsts) if firsts else None

    @property
    def t_to(self) -> Optional[int]:
        lasts = [t.points[-1].t for t in (self.kalshi, self.polymarket) if t.points]
        if not lasts:
            return None
        #: Kickoff is the right-hand edge even when the last Quote predates it — the
        #: axis is the run-up to a match, not the extent of what happened to be traded.
        return max(max(lasts), self.kickoff) if self.kickoff else max(lasts)

    @property
    def both(self) -> bool:
        return bool(self.kalshi.points) and bool(self.polymarket.points)

    def as_dict(self) -> dict[str, Any]:
        return {"fixture_id": self.fixture_id, "kickoff": self.kickoff,
                "t_from": self.t_from, "t_to": self.t_to, "both": self.both,
                "kalshi": self.kalshi.as_dict(),
                "polymarket": self.polymarket.as_dict()}
