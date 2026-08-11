"""The one place this package talks to an **Exchange** (ADR 0046).

Two jobs, and no third:

  **Sweep.** Enumerate a whole Competition's Winner Markets in one request per league and
  hand back the resolved index *plus the raw payloads*. `football_blog.kalshi` and
  `football_blog.polymarket` do the resolving — the team registries, the two match rules,
  the refusal of a half-mapped market (ADR 0041/0043). Nothing is re-implemented here.

  **Read a Quote off a raw market.** `football_blog`'s own `quote_of()` builds the Quote a
  **Match Preview** card needs and is right for that card. A **Market Observation** takes
  more, because it is the only copy that will ever exist: Kalshi also publishes the `no`
  side of the book and, crucially, `yes_bid_size_fp` / `yes_ask_size_fp` — the only
  top-of-book depth either Exchange gives, appearing in **no** candlestick and therefore
  unrecoverable the moment the minute passes. Polymarket also publishes `liquidity` and
  `volume24hr`. Rather than widen the blog's Quote and change what `match_preview` stores,
  the wider read lives here and the card keeps its narrower one.

**Cost is per Exchange-series, not per Fixture.** `/markets?series_ticker=` and
`/events?series_slug=` each return a whole Competition, so ten matches kicking off at once
cost the same six unauthenticated GETs as one. That is what makes a once-a-minute poll
defensible, and it is why nothing here ever fetches per Fixture.

**No custom User-Agent, deliberately.** ADR 0043 records that the Polymarket CLOB sits
behind Cloudflare and 403s a `Python-urllib` agent while httpx's default passes untouched.
Whatever in that default header set is being accepted is not documented anywhere, so a
politer, more identifying string is a change to the one thing known to work. The politeness
is spent on backoff instead, which is the part that actually protects the Exchange.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

import httpx

from football_blog import kalshi, polymarket

from .store import Quote

T = TypeVar("T")

#: Retry schedule for a sweep. The only evidence in this repo that Kalshi rate-limits at
#: all is the 429 handling in the abandoned `predicitons/` prototype, so this is written
#: for a limit nobody has measured: back off hard and give up rather than hammer. A sweep
#: is idempotent and cheap, so retrying the whole thing costs nothing worth optimising.
RETRY_DELAYS_S = (5, 15, 45)


def _with_retry(fn: Callable[[], T], *, what: str) -> T:
    """Run `fn`, retrying a 429 or a 5xx with a widening pause. Anything else is fatal.

    A 4xx that is not 429 means we asked wrongly — a retry would ask wrongly again — and a
    transport error is retried because it is usually the network rather than the Exchange.
    """
    last: Exception | None = None
    for attempt, delay in enumerate((*RETRY_DELAYS_S, None)):
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code != 429 and code < 500:
                raise
            last = exc
        except httpx.TransportError as exc:
            last = exc
        if delay is None:
            break
        print(f"  {what}: {type(last).__name__} — retrying in {delay}s "
              f"(attempt {attempt + 1}/{len(RETRY_DELAYS_S)})")
        time.sleep(delay)
    raise RuntimeError(f"{what} failed after {len(RETRY_DELAYS_S)} retries") from last


@dataclass
class Sweep:
    """One Exchange's whole covered universe, as of one moment.

    `index` is keyed the way that Exchange resolves — `(frozenset(team_ids), local_date)`
    for Kalshi, `(frozenset(team_ids), kickoff_instant)` for Polymarket — and the two are
    never merged, because the keys mean different things (ADR 0043).

    `raw` is what the index does not carry: the market payload per leg, keyed by the
    identifier the resolved outcome names. An Observation is read from these rather than
    from the index's reduced quotes.
    """
    exchange: str
    index: dict = field(default_factory=dict)
    unmapped: list[dict] = field(default_factory=list)
    raw: dict[str, dict] = field(default_factory=dict)
    #: Series that answered, and series that did not. A sweep of two leagues where one
    #: 500s is not a sweep of two leagues, and a caller that cannot tell would read the
    #: silence as "no markets listed" and enrol nothing for a whole Competition.
    swept: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def kalshi_sweep(registry: kalshi.Registry, only: Optional[set[str]] = None,
                 status: str = "open") -> Sweep:
    """Kalshi Winner Markets across the registry's series, indexed by team pair.

    `open` and `settled` are the two statuses that return anything: `unopened` and
    `closed` come back empty for these series, so a Fixture with no *open* market is
    genuinely *not listed yet* rather than hiding under another status.

    `settled` is what makes retroactive enrolment possible. ADR 0043 measured the two sets
    as disjoint — the market leaves `open` a few hours after kickoff and appears here —
    and a settled market still carries its `ticker`, which is the pointer the backfill
    needs. Sweeping it once recovers every past Fixture the Exchange still lists, instead
    of the store beginning empty on the day it is switched on.

    `only` narrows to the series a caller actually needs. The in-play poller passes the
    series its enrolled markets name, so a Saturday evening with only Liga MX kicking off
    costs one request rather than three.
    """
    sweep = Sweep(exchange="kalshi")
    client = kalshi.KalshiClient()
    try:
        for series in registry.series_tickers:
            if only is not None and series not in only:
                continue
            try:
                markets = _with_retry(lambda s=series: client.markets(s, status=status),
                                      what=f"kalshi {series} ({status})")
            except Exception as exc:
                print(f"  kalshi {series}: sweep failed ({exc})")
                sweep.failed.append(series)
                continue
            idx, un = kalshi.index_by_team_pair(markets, series, registry)
            sweep.index.update(idx)
            sweep.unmapped.extend(un)
            for m in markets:
                if m.get("ticker"):
                    sweep.raw[str(m["ticker"])] = m
            sweep.swept.append(series)
    finally:
        client.close()
    return sweep


def polymarket_sweep(registry: polymarket.Registry, only: Optional[set[str]] = None,
                     closed: bool = False) -> Sweep:
    """Polymarket Winner Markets across the registry's leagues.

    Enumerated by **series slug**, never by the league's name: `lec`'s series slug is
    `soccer-lec`, and `sport=lec` returns 100 events and zero moneylines, silently
    (ADR 0043). The registry stores the slug for exactly this reason.

    `closed=True` is the Polymarket half of retroactive enrolment, the mirror of Kalshi's
    `status="settled"`. The two sets are disjoint here too — `mls-2025` showed 234 open
    against 1,325 closed — and a closed event still carries its legs' `clobTokenIds`,
    which is all `/prices-history` needs.

    `only` narrows to the league keys a caller needs, as on the Kalshi side.
    """
    sweep = Sweep(exchange="polymarket")
    client = polymarket.PolymarketClient()
    try:
        for league in registry.league_keys:
            if only is not None and league not in only:
                continue
            slug = registry.series_slug(league)
            if not slug:
                continue
            try:
                events = _with_retry(lambda s=slug: client.events(s, closed=closed),
                                     what=f"polymarket {slug}"
                                          f"{' (closed)' if closed else ''}")
            except Exception as exc:
                print(f"  polymarket {slug}: sweep failed ({exc})")
                sweep.failed.append(league)
                continue
            idx, un = polymarket.index_by_team_pair(events, league, registry)
            sweep.index.update(idx)
            sweep.unmapped.extend(un)
            for event in events:
                for market in polymarket.moneyline_legs(event):
                    for token in polymarket._token_ids(market):
                        sweep.raw[token] = market
            sweep.swept.append(league)
    finally:
        client.close()
    return sweep


# --------------------------------------------------------------------------- #
# Raw market -> Quote                                                          #
# --------------------------------------------------------------------------- #
def _money(value: Any) -> Optional[float]:
    """A decimal-string money field (`'0.6600'`) -> float, or None for absent.

    None means *not published*, which is a different claim from zero, and both Exchanges
    use the empty string as well as a missing key to mean it.
    """
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mid_of(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    return (bid + ask) / 2.0 if bid is not None and ask is not None else None


def kalshi_quote(side: str, raw: dict[str, Any]) -> Quote:
    """One leg of a Kalshi **Market Observation**, from the raw `/markets` payload.

    Reads the `*_dollars` and `*_fp` fields only. Kalshi's integer-cent twins (`yes_bid`,
    `volume`, `last_price`) return None on every market now and are dead (ADR 0041), and
    `liquidity_dollars` reads `0.0000` on markets with tens of thousands of contracts
    traded, so it is left unread rather than stored as a misleading zero.
    """
    yes_bid = _money(raw.get("yes_bid_dollars"))
    yes_ask = _money(raw.get("yes_ask_dollars"))
    return Quote(
        side=side,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=_money(raw.get("no_bid_dollars")),
        no_ask=_money(raw.get("no_ask_dollars")),
        last=_money(raw.get("last_price_dollars")),
        mid=_mid_of(yes_bid, yes_ask),
        yes_bid_size=_money(raw.get("yes_bid_size_fp")),
        yes_ask_size=_money(raw.get("yes_ask_size_fp")),
        volume=_money(raw.get("volume_fp")),
        volume_unit="contracts",
        volume_24h=_money(raw.get("volume_24h_fp")),
        open_interest=_money(raw.get("open_interest_fp")),
        liquidity=None,
    )


def polymarket_quote(side: str, raw: dict[str, Any]) -> Quote:
    """One leg of a Polymarket **Market Observation**, from the raw Gamma market payload.

    `mid` is Polymarket's own `outcomePrices` rather than one recomputed from the book:
    the CLOB price history *is* that number, so taking it keeps a stored Observation and a
    backfilled **Market Candle** on the same scale instead of merely close to it.

    The `no_*` columns stay null. Polymarket's No leg is a separate token with its own
    book, and `1 - yes_ask` is arithmetic rather than a quote — writing it would put a
    number in a column that says "what the Exchange published" and have the Exchange never
    have published it. `open_interest` stays null because Gamma publishes none; `liquidity`
    is filled because, unlike Kalshi's, Polymarket's is a real figure.
    """
    bid, ask = _money(raw.get("bestBid")), _money(raw.get("bestAsk"))
    published_mid = polymarket._yes_price(raw)
    return Quote(
        side=side,
        yes_bid=bid,
        yes_ask=ask,
        no_bid=None,
        no_ask=None,
        last=_money(raw.get("lastTradePrice")),
        mid=published_mid if published_mid is not None else _mid_of(bid, ask),
        yes_bid_size=None,
        yes_ask_size=None,
        volume=_money(raw.get("volume")),
        volume_unit="usd",
        volume_24h=_money(raw.get("volume24hr")),
        open_interest=None,
        liquidity=_money(raw.get("liquidity")),
    )


def quotes_for(exchange: str, outcomes: list[dict], raw: dict[str, dict]) -> list[Quote]:
    """The three **Quotes** of one **Market Observation**, or an empty list.

    Empty rather than partial: three legs or none is the rule an Observation is stored
    under (`store.write_observation`), and a leg whose raw payload the sweep did not carry
    is a leg we cannot quote. Returning two would push the decision onto every caller.
    """
    out: list[Quote] = []
    for o in outcomes:
        side = o.get("side")
        key = o.get("market_ticker") if exchange == "kalshi" else o.get("token_id")
        payload = raw.get(str(key)) if key else None
        if side not in ("home", "draw", "away") or payload is None:
            return []
        out.append(kalshi_quote(side, payload) if exchange == "kalshi"
                   else polymarket_quote(side, payload))
    return out
