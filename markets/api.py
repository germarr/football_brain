"""The public read API over the **Market Store** (ADR 0046).

The one process in this repo the internet is allowed to reach, and the separation is the
security model rather than a deployment detail. Every app under `surfaces/` binds to
`127.0.0.1` with a docstring explaining that it must never be exposed: they spawn
subprocesses, spend API-Football quota and Anthropic tokens, and write the Editorial
Store. No route guard fixes that, because exposability is a property of what a process
*can* do, not of what its handlers happen to call. So this is its own app, and it has:

  * no `POST`, `PUT`, `PATCH` or `DELETE` route — `tests/test_market_api.py` asserts it;
  * no subprocess, no job runner, no writer of any kind;
  * one Postgres connection, read-only by construction (it only ever SELECTs).

It also carries the first `CORSMiddleware` in the project, allowing exactly one origin
and exactly `GET`.

## Three rules the serialiser enforces, each a lie the glossary already forbids

**Never blend the two Exchanges.** No consensus number, no primary Exchange, no shared
volume axis — Kalshi counts contracts and Polymarket counts dollars, so `volume_unit`
rides on every Exchange block and the two never appear in one array.

**A bucket missing a leg is a gap.** Normalisation runs through `football_blog.track
.normalise`, the same function the `/previews` board's live Track uses, so there is one
implementation of the rule rather than two that agree until they don't.

**Absence is typed, never null.** `not_covered` is permanent and needs no human,
`not_listed` resolves itself when the Exchange opens the market, `unmapped` is the only
one anyone can act on, and `no_data` means enrolled but nothing harvested yet.

## Where the numbers come from

A **Market Probability** is derived here and stored nowhere — the half of ADR 0043 that
survives. The probability line reads **Market Candles**, because they reach back to the
moment the Exchange listed the Fixture; the book series reads **Market Observations**,
because a Candle has no book. At `resolution=auto` the line is hourly across the run-up
and per-minute across the **In-Play Window**, stitched at the window's left edge.

Run:
    uv run python -m markets                       # 127.0.0.1:8010
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row

from football_blog.postgres import get_conn as published_conn
from football_blog.track import SIDES, normalise

from . import store

#: The origins allowed to read this. The blog is the only consumer; localhost is here so
#: the page can be developed against the real API without a proxy.
ALLOWED_ORIGINS = [
    "https://lacancha.gerardomarr.com",
    "http://localhost:4321",
    "http://localhost:3000",
]

#: Volume's unit per Exchange. Kalshi counts contracts and Polymarket counts dollars
#: traded; `139` and `41,814` on one Fixture are two different measurements with no
#: conversion between them, so the unit travels with the number, always.
VOLUME_UNIT = {"kalshi": "contracts", "polymarket": "usd"}

#: Event types worth a marker on a probability line. A substitution moves a market far
#: less than a goal and would crowd the axis; this is a whitelist rather than a blacklist
#: for the same reason the blog whitelists fixture statuses.
MARKER_TYPES = ("Goal", "Card", "Var")

app = FastAPI(
    title="La Cancha — prediction markets",
    description="Read-only Market Store: what Kalshi and Polymarket thought, over time.",
    # Everything this service exposes lives under /api/, including its own documentation,
    # so the tunnel hostname has exactly one prefix to reason about. All three are
    # overridden together: setting only `docs_url` leaves ReDoc at FastAPI's default
    # `/redoc`, outside the prefix and reachable by anyone who guesses it.
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _ts(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# Reads                                                                        #
# --------------------------------------------------------------------------- #
def _fixture(fixture_id: int) -> Optional[dict]:
    """The football half, from the **Published Store**. Nightly vintage, and said so."""
    with published_conn().cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT f.id, f.date, f.status, f.season, f.tournament, f.round, "
            "       f.home_team_id, f.home_team_name, f.away_team_id, f.away_team_name, "
            "       f.home_goals, f.away_goals, f.league_id, c.name AS league_name "
            "FROM fixture f LEFT JOIN competition c ON c.id = f.league_id "
            "WHERE f.id = %s",
            (fixture_id,),
        ).fetchone()


def _events(fixture_id: int, kickoff: Optional[datetime]) -> list[dict]:
    """Goal, card and VAR markers, placed on the same axis as the probability line.

    A minute is turned into an instant by adding it to kickoff, which is an approximation
    and the only one available: the Published Store records the match minute, not the wall
    clock, and stoppage time means minute 45+3 and minute 48 are not the same moment. It
    is close enough to sit a marker beside the step it explains, and `t_estimated: true`
    says so rather than letting a reader take it for a measurement.
    """
    if kickoff is None:
        return []
    base = _ts(kickoff) or 0
    with published_conn().cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT e.minute, e.extra, e.type, e.detail, e.team_id, "
            "       p.name AS player_name "
            "FROM event e LEFT JOIN player p ON p.id = e.player_id "
            "WHERE e.fixture_id = %s AND e.type = ANY(%s) "
            "ORDER BY e.minute, e.event_index",
            (fixture_id, list(MARKER_TYPES)),
        ).fetchall()
    out = []
    for r in rows:
        minute = (r["minute"] or 0) + (r["extra"] or 0)
        out.append({
            "t": base + minute * 60,
            "t_estimated": True,
            "minute": r["minute"],
            "extra": r["extra"],
            "type": r["type"],
            "detail": r["detail"],
            "team_id": r["team_id"],
            "player": r["player_name"],
        })
    return out


def _markets(conn, fixture_id: int) -> dict[str, dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT exchange, state, backfill_state, series_ticker, event_ticker, "
            "       event_slug, league, kickoff_utc, enrolled_at, last_seen_at "
            "FROM winner_market WHERE fixture_id = %s",
            (fixture_id,),
        ).fetchall()
    return {r["exchange"]: r for r in rows}


def _candle_mids(conn, fixture_id: int, exchange: str, resolution: str,
                 in_play_from: Optional[int]) -> tuple[dict[str, dict[int, float]], int]:
    """`{side: {bucket: mid}}` from **Market Candles**, plus the period actually used.

    `auto` stitches the two resolutions at the left edge of the In-Play Window: hourly
    before it, per-minute inside it. The two never overlap in the output, so a reader
    plotting `t` gets one continuous line that simply gets denser at kickoff.
    """
    if resolution == "hour":
        clause, params = "period_seconds = %s", [store.RESOLUTION_RUNUP_S]
    elif resolution == "minute":
        clause, params = "period_seconds = %s", [store.RESOLUTION_IN_PLAY_S]
    elif in_play_from is None:
        clause, params = "period_seconds = %s", [store.RESOLUTION_RUNUP_S]
    else:
        edge = datetime.fromtimestamp(in_play_from, tz=timezone.utc)
        clause = ("((period_seconds = %s AND period_start < %s) OR "
                  " (period_seconds = %s AND period_start >= %s))")
        params = [store.RESOLUTION_RUNUP_S, edge, store.RESOLUTION_IN_PLAY_S, edge]

    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"SELECT side, period_start, period_seconds, mid_close, volume, open_interest "
            f"FROM market_candle "
            f"WHERE fixture_id = %s AND exchange = %s AND mid_close IS NOT NULL AND {clause} "
            f"ORDER BY period_start",
            [fixture_id, exchange, *params],
        ).fetchall()

    mids: dict[str, dict[int, float]] = {s: {} for s in SIDES}
    for r in rows:
        if r["side"] in mids:
            mids[r["side"]][_ts(r["period_start"])] = float(r["mid_close"])
    return mids, len(rows)


def _candle_depth(conn, fixture_id: int, exchange: str) -> list[dict]:
    """Per-bucket volume and open interest, summed across the three legs.

    Summed because a Winner Market's depth is the depth of all three contracts — a reader
    asking "was anyone trading this game?" is not asking about the draw leg alone. Kalshi
    only: Polymarket publishes no per-period volume, so its depth comes from Observations.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT period_start, period_seconds, sum(volume) AS volume, "
            "       sum(open_interest) AS open_interest "
            "FROM market_candle "
            "WHERE fixture_id = %s AND exchange = %s AND volume IS NOT NULL "
            "GROUP BY period_start, period_seconds ORDER BY period_start",
            (fixture_id, exchange),
        ).fetchall()
    return [{"t": _ts(r["period_start"]), "period_s": r["period_seconds"],
             "volume": float(r["volume"]) if r["volume"] is not None else None,
             "open_interest": float(r["open_interest"])
             if r["open_interest"] is not None else None}
            for r in rows]


def _book(conn, fixture_id: int, exchange: str) -> list[dict]:
    """The **Market Observation** series, one wide row per instant.

    Wide because an Observation *is* three legs at one instant — a partial one is not a
    smaller Observation, it is not one — so the wire shape says that rather than leaving a
    client to re-group three narrow rows and decide for itself what a missing leg means.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT observed_at, side, yes_bid, yes_ask, no_bid, no_ask, last, mid, "
            "       yes_bid_size, yes_ask_size, volume, volume_24h, open_interest, "
            "       liquidity "
            "FROM market_quote WHERE fixture_id = %s AND exchange = %s "
            "ORDER BY observed_at, side",
            (fixture_id, exchange),
        ).fetchall()

    by_instant: dict[int, dict[str, Any]] = {}
    for r in rows:
        t = _ts(r["observed_at"])
        point = by_instant.setdefault(t, {"t": t})
        point[r["side"]] = {
            "yes_bid": r["yes_bid"], "yes_ask": r["yes_ask"],
            "no_bid": r["no_bid"], "no_ask": r["no_ask"],
            "last": r["last"], "mid": r["mid"],
            "yes_bid_size": r["yes_bid_size"], "yes_ask_size": r["yes_ask_size"],
            "volume": r["volume"], "volume_24h": r["volume_24h"],
            "open_interest": r["open_interest"], "liquidity": r["liquidity"],
            "spread": (r["yes_ask"] - r["yes_bid"])
            if r["yes_ask"] is not None and r["yes_bid"] is not None else None,
        }
    return [p for _t, p in sorted(by_instant.items())
            if all(s in p for s in SIDES)]


# --------------------------------------------------------------------------- #
# Assembly                                                                     #
# --------------------------------------------------------------------------- #
def _exchange_block(conn, fixture_id: int, exchange: str, market: Optional[dict],
                    resolution: str, in_play_from: Optional[int]) -> dict:
    """One Exchange's whole panel. Never merged with the other's, at any depth."""
    if market is None:
        return {"state": "not_listed", "volume_unit": VOLUME_UNIT[exchange],
                "listed_from": None, "gaps": 0, "resolution": resolution,
                "probability": [], "book": [], "depth": []}

    mids, candle_rows = _candle_mids(conn, fixture_id, exchange, resolution, in_play_from)
    points, gaps = normalise(mids)
    book = _book(conn, fixture_id, exchange)
    state = market["state"]
    if state == "tracked" and not points and not book:
        # Enrolled, and nothing harvested yet. Distinct from *not listed*: the Exchange
        # does list this Fixture, we simply have not read it. It resolves itself on the
        # next backfill, so it must not be reported as the Exchange's silence.
        state = "no_data"

    return {
        "state": state,
        "backfill_state": market["backfill_state"],
        "volume_unit": VOLUME_UNIT[exchange],
        "resolution": resolution,
        "listed_from": points[0].t if points else None,
        "enrolled_at": _ts(market["enrolled_at"]),
        "last_seen_at": _ts(market["last_seen_at"]),
        "gaps": gaps,
        "candles": candle_rows,
        "identifiers": {
            "series_ticker": market["series_ticker"],
            "event_ticker": market["event_ticker"],
            "event_slug": market["event_slug"],
            "league": market["league"],
        },
        "probability": [p.as_dict() for p in points],
        "book": book,
        "depth": _candle_depth(conn, fixture_id, exchange) if exchange == "kalshi" else [],
    }


def _dashboard(fixture_id: int, resolution: str) -> Optional[dict]:
    conn = store.get_conn()
    markets = _markets(conn, fixture_id)
    fixture = _fixture(fixture_id)
    if not markets and not fixture:
        return None

    kickoff = fixture["date"] if fixture else None
    if kickoff is None and markets:
        kickoff = next(iter(markets.values()))["kickoff_utc"]
    kickoff_ts = _ts(kickoff)
    in_play_from = kickoff_ts - store.IN_PLAY_LEAD_S if kickoff_ts else None
    in_play_to = kickoff_ts + store.IN_PLAY_TAIL_S if kickoff_ts else None

    blocks = {
        ex: _exchange_block(conn, fixture_id, ex, markets.get(ex), resolution, in_play_from)
        for ex in ("kalshi", "polymarket")
    }

    # One shared axis, computed here rather than left to the client. The two Tracks are
    # routinely 190x different in span, and two panels auto-scaled independently render a
    # month-long drift and a four-hour wobble at the same width — the lie by omission the
    # glossary forbids for volume, made again with time.
    firsts = [b["probability"][0]["t"] for b in blocks.values() if b["probability"]]
    # Kickoff is the right-hand edge even when the last point predates it — the axis is
    # the run-up and the match, not the extent of what happened to be traded. It can also
    # be absent, on a Fixture the Published Store has no date for, so it is filtered
    # rather than assumed alongside the points.
    lasts = [b["probability"][-1]["t"] for b in blocks.values() if b["probability"]]
    edges = [t for t in (*lasts, in_play_to) if t is not None]
    t_from = min(firsts) if firsts else None
    t_to = max(edges) if edges else None

    return {
        "fixture_id": fixture_id,
        "kickoff": kickoff_ts,
        "kickoff_iso": _iso(kickoff),
        "competition": {"id": fixture["league_id"], "name": fixture["league_name"]}
        if fixture else None,
        "home": {"team_id": fixture["home_team_id"], "name": fixture["home_team_name"]}
        if fixture else None,
        "away": {"team_id": fixture["away_team_id"], "name": fixture["away_team_name"]}
        if fixture else None,
        "status": fixture["status"] if fixture else None,
        "score": {"home": fixture["home_goals"], "away": fixture["away_goals"]}
        if fixture else None,
        "axis": {"t_from": t_from, "t_to": t_to, "kickoff": kickoff_ts,
                 "in_play_from": in_play_from, "in_play_to": in_play_to},
        "exchanges": blocks,
        "events": _events(fixture_id, kickoff),
        # The football half of this response advances at 04:00 and only for Fixtures that
        # are Final. During a match the line moves and no marker appears beside it; they
        # arrive the next morning. Stated rather than inferred, for the same reason ADR
        # 0044 refused a permanently-null live_minute: an absent marker must not read as
        # "nothing happened".
        "events_vintage": "nightly",
    }


# --------------------------------------------------------------------------- #
# Routes — GET only, every one of them                                         #
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/fixtures")
def fixtures(
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    competition: Optional[int] = None,
) -> Any:
    """Which Fixtures have market data, in a kickoff window. The thin index.

    Defaults to a −3d/+14d window, matching the **Fixture Row** window the blog already
    uses, so the two feeds cover the same games.
    """
    now = datetime.now(timezone.utc)
    start = datetime.combine(from_, datetime.min.time(), tzinfo=timezone.utc) \
        if from_ else now - timedelta(days=3)
    end = datetime.combine(to, datetime.max.time(), tzinfo=timezone.utc) \
        if to else now + timedelta(days=14)

    conn = store.get_conn()
    sql = ("SELECT m.fixture_id, min(m.kickoff_utc) AS kickoff, "
           "       min(m.competition_id) AS competition_id, "
           "       min(m.home_team_id) AS home_team_id, min(m.home_team_name) AS home, "
           "       min(m.away_team_id) AS away_team_id, min(m.away_team_name) AS away, "
           "       array_agg(m.exchange ORDER BY m.exchange) AS exchanges "
           "FROM winner_market m "
           "WHERE m.kickoff_utc >= %s AND m.kickoff_utc <= %s AND m.state = 'tracked' ")
    params: list[Any] = [start, end]
    if competition is not None:
        sql += "AND m.competition_id = %s "
        params.append(competition)
    sql += "GROUP BY m.fixture_id ORDER BY min(m.kickoff_utc)"

    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(sql, params).fetchall()
    return {
        "from": start.date().isoformat(),
        "to": end.date().isoformat(),
        "count": len(rows),
        "fixtures": [{
            "fixture_id": r["fixture_id"],
            "kickoff": _ts(r["kickoff"]),
            "kickoff_iso": _iso(r["kickoff"]),
            "competition_id": r["competition_id"],
            "home": {"team_id": r["home_team_id"], "name": r["home"]},
            "away": {"team_id": r["away_team_id"], "name": r["away"]},
            "exchanges": r["exchanges"],
        } for r in rows],
    }


@app.get("/api/fixtures/{fixture_id}/markets")
def markets(fixture_id: int,
            resolution: str = Query("auto", pattern="^(auto|hour|minute)$")) -> Any:
    """The dashboard payload: one Fixture's whole story, both Exchanges, one axis."""
    payload = _dashboard(fixture_id, resolution)
    if payload is None:
        return JSONResponse(
            {"error": f"No Fixture {fixture_id} in the Market Store or the Published "
                      f"Store. Only MLS, Liga MX and Leagues Cup are covered by either "
                      f"Exchange."},
            status_code=404,
        )
    return payload


@app.get("/api/fixtures/{fixture_id}/track")
def track(fixture_id: int,
          exchange: Optional[str] = Query(None, pattern="^(kalshi|polymarket)$"),
          resolution: str = Query("auto", pattern="^(auto|hour|minute)$")) -> Any:
    """Just the probability lines — the same numbers, without the book or the markers."""
    payload = _dashboard(fixture_id, resolution)
    if payload is None:
        return JSONResponse({"error": f"No Fixture {fixture_id}."}, status_code=404)
    wanted = [exchange] if exchange else ["kalshi", "polymarket"]
    return {
        "fixture_id": fixture_id, "kickoff": payload["kickoff"],
        "axis": payload["axis"],
        "exchanges": {
            ex: {k: payload["exchanges"][ex][k]
                 for k in ("state", "listed_from", "gaps", "resolution", "probability")}
            for ex in wanted
        },
    }


@app.get("/api/fixtures/{fixture_id}/book")
def book(fixture_id: int,
         exchange: Optional[str] = Query(None, pattern="^(kalshi|polymarket)$")) -> Any:
    """Just the observed book — bid, ask, spread, size, volume, open interest.

    Each Exchange's block carries its own `volume_unit` and they are never put in one
    array: Kalshi counts contracts, Polymarket counts dollars, and there is no conversion.
    """
    conn = store.get_conn()
    markets_ = _markets(conn, fixture_id)
    if not markets_:
        return JSONResponse({"error": f"No enrolled market for Fixture {fixture_id}."},
                            status_code=404)
    wanted = [exchange] if exchange else ["kalshi", "polymarket"]
    return {
        "fixture_id": fixture_id,
        "exchanges": {
            ex: {"state": markets_[ex]["state"] if ex in markets_ else "not_listed",
                 "volume_unit": VOLUME_UNIT[ex],
                 "book": _book(conn, fixture_id, ex) if ex in markets_ else []}
            for ex in wanted
        },
    }
