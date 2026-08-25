"""The **Market Store** — four Postgres tables, their DDL, and every write path (ADR 0046).

Lives in `football_prod` beside the **Published Store** and is not part of it. None of
these table names appears in `football.publish.pg.FOOTBALL_TABLES` or
`COMMENTARY_TABLES`, so a wholesale publish moves its ten tables into `public` around
these four without touching them.

**Nothing here has a foreign key, and that is structural rather than stylistic.** The
wholesale publish runs `DROP TABLE IF EXISTS public.fixture CASCADE` inside its
blue-green swap, and `CASCADE` silently drops every FK constraint pointing at the table
being dropped. A `REFERENCES public.fixture` added here would vanish on the next reset
with no error at all, leaving a schema that reads stronger than it is — on the one store
in this project that cannot be rebuilt. So a Fixture id is a **bridge, never a key**,
exactly as on a Narrated Match, and `tests/test_market_store.py` asserts the DDL says so.

**Two kinds of row, two different guarantees**, which is why they are two tables:

  `market_candle`       the Exchange's own published history. Re-fetchable forever, on
                        open and settled markets alike, so the backfill is idempotent and
                        every gap is repairable. `period_seconds` is in the primary key
                        because the same minute is legitimately held at both one-minute
                        and one-hour resolution.
  `market_quote`        what our poller read at an instant it stamped itself. The only
                        copy there will ever be: Kalshi's top-of-book sizes appear in no
                        candlestick and Polymarket publishes no book history at all, so a
                        missed minute is missed forever.

Merging them would put a repairable fact and an unrepairable one behind one guarantee,
and the weaker one would win silently.

A **Market Probability** is deliberately absent. It is the mid normalised across one
Exchange's three legs — cheap to derive from rows held here, and a stored copy is one more
thing that can quietly disagree with the numbers it came from. `api.py` derives it per
request. This is the half of ADR 0043 that survives.

Run:
    uv run python -m markets.store --init     # create the schema (idempotent)
    uv run python -m markets.store --counts   # row counts, for eyeballing a run
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence

import psycopg
from psycopg.rows import dict_row

from football.config import load_pg_url

#: The Competitions both Exchanges list (ADR 0043): the Premier League, La Liga, MLS,
#: Liga MX, Leagues Cup. Every covered Fixture in these is enrolled — not only the ones
#: with a Publication, because enrolment is the deadline and a Fixture nobody chose to
#: write about today may be one somebody wants a chart of next week.
COVERED_COMPETITIONS = (39, 140, 253, 262, 772)

#: The four tables, in the order a reader meets them. Named here so the test that guards
#: against a publish eating them has one list to check rather than a regex over the DDL.
TABLES = ("winner_market", "winner_market_leg", "market_quote", "market_candle")

#: The order the three legs are always in, matching football_blog.track.SIDES.
SIDES = ("home", "draw", "away")

#: The In-Play Window (CONTEXT.md): kickoff - 15m to kickoff + 150m. It ends long after
#: the 90 minutes a Winner Market settles on, because settlement is not instant and the
#: convergence to 1 and 0 is part of the picture rather than noise after it.
IN_PLAY_LEAD_S = 15 * 60
IN_PLAY_TAIL_S = 150 * 60

#: Candle resolutions, in seconds. One minute inside the In-Play Window, one hour across
#: the run-up. 86400 is offered by Kalshi and never used: a daily bar of a five-day market
#: is four points.
RESOLUTION_IN_PLAY_S = 60
RESOLUTION_RUNUP_S = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS winner_market (
    fixture_id      bigint  NOT NULL,
    exchange        text    NOT NULL CHECK (exchange IN ('kalshi', 'polymarket')),
    competition_id  integer,
    kickoff_utc     timestamptz,
    local_date      date,
    home_team_id    integer,
    home_team_name  text,
    away_team_id    integer,
    away_team_name  text,
    series_ticker   text,
    event_ticker    text,
    event_slug      text,
    league          text,
    state           text NOT NULL DEFAULT 'tracked'
        CHECK (state IN ('tracked', 'not_listed', 'not_covered', 'unmapped')),
    backfill_state  text NOT NULL DEFAULT 'pending'
        CHECK (backfill_state IN ('pending', 'partial', 'complete', 'unreachable')),
    enrolled_at     timestamptz NOT NULL,
    last_seen_at    timestamptz,
    backfilled_at   timestamptz,
    PRIMARY KEY (fixture_id, exchange)
);

CREATE TABLE IF NOT EXISTS winner_market_leg (
    fixture_id          bigint NOT NULL,
    exchange            text   NOT NULL CHECK (exchange IN ('kalshi', 'polymarket')),
    side                text   NOT NULL CHECK (side IN ('home', 'draw', 'away')),
    team_id             integer,
    exchange_team_id    text,
    exchange_team_name  text,
    market_ticker       text,
    token_id            text,
    market_slug         text,
    PRIMARY KEY (fixture_id, exchange, side)
);

CREATE TABLE IF NOT EXISTS market_quote (
    fixture_id     bigint      NOT NULL,
    exchange       text        NOT NULL CHECK (exchange IN ('kalshi', 'polymarket')),
    side           text        NOT NULL CHECK (side IN ('home', 'draw', 'away')),
    observed_at    timestamptz NOT NULL,
    yes_bid        double precision,
    yes_ask        double precision,
    no_bid         double precision,
    no_ask         double precision,
    last           double precision,
    mid            double precision,
    yes_bid_size   double precision,
    yes_ask_size   double precision,
    volume         double precision,
    volume_unit    text CHECK (volume_unit IN ('contracts', 'usd')),
    volume_24h     double precision,
    open_interest  double precision,
    liquidity      double precision,
    PRIMARY KEY (fixture_id, exchange, side, observed_at)
);

CREATE TABLE IF NOT EXISTS market_candle (
    fixture_id      bigint      NOT NULL,
    exchange        text        NOT NULL CHECK (exchange IN ('kalshi', 'polymarket')),
    side            text        NOT NULL CHECK (side IN ('home', 'draw', 'away')),
    period_start    timestamptz NOT NULL,
    period_seconds  integer     NOT NULL,
    mid_close       double precision,
    yes_bid_open    double precision,
    yes_bid_high    double precision,
    yes_bid_low     double precision,
    yes_bid_close   double precision,
    yes_ask_open    double precision,
    yes_ask_high    double precision,
    yes_ask_low     double precision,
    yes_ask_close   double precision,
    price_open      double precision,
    price_high      double precision,
    price_low       double precision,
    price_close     double precision,
    volume          double precision,
    open_interest   double precision,
    PRIMARY KEY (fixture_id, exchange, side, period_start, period_seconds)
);

CREATE INDEX IF NOT EXISTS ix_quote_series
    ON market_quote (fixture_id, exchange, observed_at);
CREATE INDEX IF NOT EXISTS ix_candle_series
    ON market_candle (fixture_id, exchange, period_seconds, period_start);
CREATE INDEX IF NOT EXISTS ix_winner_market_kickoff
    ON winner_market (kickoff_utc);
CREATE INDEX IF NOT EXISTS ix_winner_market_backfill
    ON winner_market (backfill_state, kickoff_utc);
"""


# --------------------------------------------------------------------------- #
# Connection                                                                   #
# --------------------------------------------------------------------------- #
_conn: Optional[psycopg.Connection] = None


def _assert_market_store(conn: psycopg.Connection) -> None:
    """Refuse a connection to a database that is not the Published Store's.

    The same guard `football_blog.postgres` makes, for the same reason and against the
    same mistake: this project's `.env` defines the bare `PG*` vars twice, so a URL
    assembled from them lands on the YouTube database instead. `fixture` is the probe
    rather than one of our own tables because it is what tells us we are in
    `football_prod` *before* `--init` has ever run — checking for `winner_market` would
    pass happily on a brand-new empty database anywhere.
    """
    if conn.execute("SELECT to_regclass('public.fixture')").fetchone()[0]:
        return
    dbname, user = conn.execute("SELECT current_database(), current_user").fetchone()
    raise RuntimeError(
        f"Connected to database {dbname!r} as {user!r}, but it has no `public.fixture` "
        f"table — this is not the database the Published Store lives in.\n"
        f"    Check FOOTBALL_DATABASE_URL in the project-root .env; the bare PG* vars "
        f"are defined twice and resolve to the wrong database from Python."
    )


def get_conn() -> psycopg.Connection:
    """The process-wide connection. Autocommit, like every other Postgres reader here.

    Writers that need several statements to land together take an explicit transaction
    with `with conn.transaction():` rather than relying on connection-level batching.
    """
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    _conn = psycopg.connect(load_pg_url(), connect_timeout=30, autocommit=True)
    try:
        _assert_market_store(_conn)
    except Exception:
        _conn.close()
        _conn = None
        raise
    return _conn


def close_conn() -> None:
    global _conn
    if _conn is not None and not _conn.closed:
        _conn.close()
    _conn = None


def init(conn: Optional[psycopg.Connection] = None) -> None:
    """Create the four tables and their indexes. Idempotent — every statement is
    `IF NOT EXISTS`, so collectors call it on start and a re-run is free."""
    (conn or get_conn()).execute(SCHEMA)


# --------------------------------------------------------------------------- #
# What a caller hands us                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    """One outcome of one **Winner Market**, and the pointer that outlives its listing.

    `market_ticker` is Kalshi's, `token_id` Polymarket's. Both are captured at enrolment
    and never re-derived: an Exchange stops listing a market once it settles, so this is
    the only way back to a played Fixture's history (ADR 0043).
    """
    side: str
    team_id: Optional[int] = None
    exchange_team_id: Optional[str] = None
    exchange_team_name: Optional[str] = None
    market_ticker: Optional[str] = None
    token_id: Optional[str] = None
    market_slug: Optional[str] = None


@dataclass
class Quote:
    """One leg of one **Market Observation** — what the Exchange published, verbatim.

    Every field is nullable because the two Exchanges publish different subsets and null
    means *not published*, which is a different claim from zero. Kalshi fills the `no_*`
    side and both sizes and leaves `liquidity` alone (its `liquidity_dollars` reads
    `0.0000` on markets with tens of thousands of contracts traded — ADR 0041).
    Polymarket fills `liquidity` and leaves the sizes and `open_interest` alone.
    """
    side: str
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    last: Optional[float] = None
    mid: Optional[float] = None
    yes_bid_size: Optional[float] = None
    yes_ask_size: Optional[float] = None
    volume: Optional[float] = None
    volume_unit: Optional[str] = None
    volume_24h: Optional[float] = None
    open_interest: Optional[float] = None
    liquidity: Optional[float] = None


@dataclass
class Candle:
    """One leg's period, as the Exchange published it.

    `mid_close` is the one field both Exchanges fill — Kalshi's derived from the close of
    each side of the book, Polymarket's being the only number it publishes at all. The
    OHLC columns are Kalshi-only, and null on a Polymarket row because Polymarket has
    nothing to put there, not because a read failed.
    """
    side: str
    period_start: datetime
    period_seconds: int
    mid_close: Optional[float] = None
    yes_bid_open: Optional[float] = None
    yes_bid_high: Optional[float] = None
    yes_bid_low: Optional[float] = None
    yes_bid_close: Optional[float] = None
    yes_ask_open: Optional[float] = None
    yes_ask_high: Optional[float] = None
    yes_ask_low: Optional[float] = None
    yes_ask_close: Optional[float] = None
    price_open: Optional[float] = None
    price_high: Optional[float] = None
    price_low: Optional[float] = None
    price_close: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None


# --------------------------------------------------------------------------- #
# Writes                                                                       #
# --------------------------------------------------------------------------- #
_MARKET_COLS = (
    "fixture_id", "exchange", "competition_id", "kickoff_utc", "local_date",
    "home_team_id", "home_team_name", "away_team_id", "away_team_name",
    "series_ticker", "event_ticker", "event_slug", "league", "state",
)

_LEG_COLS = (
    "fixture_id", "exchange", "side", "team_id", "exchange_team_id",
    "exchange_team_name", "market_ticker", "token_id", "market_slug",
)

_QUOTE_COLS = (
    "fixture_id", "exchange", "side", "observed_at", "yes_bid", "yes_ask", "no_bid",
    "no_ask", "last", "mid", "yes_bid_size", "yes_ask_size", "volume", "volume_unit",
    "volume_24h", "open_interest", "liquidity",
)

_CANDLE_COLS = (
    "fixture_id", "exchange", "side", "period_start", "period_seconds", "mid_close",
    "yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close",
    "yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close",
    "price_open", "price_high", "price_low", "price_close", "volume", "open_interest",
)


def _placeholders(cols: Sequence[str]) -> str:
    return ", ".join(["%s"] * len(cols))


def _collist(cols: Sequence[str]) -> str:
    return ", ".join(cols)


def upsert_market(conn: psycopg.Connection, *, fixture_id: int, exchange: str,
                  legs: Iterable[Leg], observed_at: datetime, **fields: Any) -> None:
    """Enrol (or re-confirm) one **Winner Market** and its three legs.

    `enrolled_at` is set once and never moved; `last_seen_at` moves every run. The pair
    answers two different questions — when we first had a pointer to this market, and
    whether the Exchange was still listing it an hour ago — and one column would answer
    neither. `backfill_state` is left alone on conflict: a market already harvested must
    not be dragged back to `pending` by an hourly re-confirmation.

    The legs are replaced wholesale rather than upserted. A leg's identity is its side,
    and if the Exchange ever re-issued a market under new tickers the old three are wrong
    together — a per-leg upsert would leave a mixture of the two and nothing would say so.
    """
    values = [fixture_id, exchange] + [fields.get(c) for c in _MARKET_COLS[2:]]
    with conn.transaction():
        conn.execute(
            f"INSERT INTO winner_market ({_collist(_MARKET_COLS)}, enrolled_at, last_seen_at) "
            f"VALUES ({_placeholders(_MARKET_COLS)}, %s, %s) "
            f"ON CONFLICT (fixture_id, exchange) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in _MARKET_COLS[2:])
            + ", last_seen_at = EXCLUDED.last_seen_at",
            (*values, observed_at, observed_at),
        )
        conn.execute(
            "DELETE FROM winner_market_leg WHERE fixture_id = %s AND exchange = %s",
            (fixture_id, exchange),
        )
        rows = [
            (fixture_id, exchange, leg.side, leg.team_id, leg.exchange_team_id,
             leg.exchange_team_name, leg.market_ticker, leg.token_id, leg.market_slug)
            for leg in legs
        ]
        if rows:
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO winner_market_leg ({_collist(_LEG_COLS)}) "
                    f"VALUES ({_placeholders(_LEG_COLS)})",
                    rows,
                )


def write_observation(conn: psycopg.Connection, *, fixture_id: int, exchange: str,
                      observed_at: datetime, quotes: Sequence[Quote]) -> int:
    """Write one **Market Observation** — all three legs at one instant, or none.

    Three legs or none is the whole rule. A partial Observation cannot be normalised into
    a Market Probability, so storing two legs would put a row in the store that every
    reader has to remember to discard; the refusal belongs here, once, where it is also
    counted. Returns the number of rows written (3, or 0).

    `ON CONFLICT DO NOTHING` rather than DO UPDATE: an Observation is stamped with the
    instant *we* read it, so a conflict means the same poll ran twice — two `flock`ed
    ticks racing, or a manual run beside the cron. The first write is the real one.
    """
    if len(quotes) != len(SIDES) or {q.side for q in quotes} != set(SIDES):
        return 0
    rows = [
        (fixture_id, exchange, q.side, observed_at, q.yes_bid, q.yes_ask, q.no_bid,
         q.no_ask, q.last, q.mid, q.yes_bid_size, q.yes_ask_size, q.volume,
         q.volume_unit, q.volume_24h, q.open_interest, q.liquidity)
        for q in quotes
    ]
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO market_quote ({_collist(_QUOTE_COLS)}) "
            f"VALUES ({_placeholders(_QUOTE_COLS)}) ON CONFLICT DO NOTHING",
            rows,
        )
    return len(rows)


def write_candles(conn: psycopg.Connection, *, fixture_id: int, exchange: str,
                  candles: Sequence[Candle]) -> int:
    """Upsert **Market Candles**. Idempotent: a re-run repairs a gap and changes nothing else.

    `DO UPDATE` rather than `DO NOTHING`, unlike an Observation, because a Candle is the
    Exchange's own record of a period and the Exchange is allowed to revise it — the last
    period of an open market is still being written when we first read it. Re-fetching is
    how that period gets its final values, so a second write must win.
    """
    if not candles:
        return 0
    rows = [
        (fixture_id, exchange, c.side, c.period_start, c.period_seconds, c.mid_close,
         c.yes_bid_open, c.yes_bid_high, c.yes_bid_low, c.yes_bid_close,
         c.yes_ask_open, c.yes_ask_high, c.yes_ask_low, c.yes_ask_close,
         c.price_open, c.price_high, c.price_low, c.price_close, c.volume,
         c.open_interest)
        for c in candles
    ]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _CANDLE_COLS[5:])
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO market_candle ({_collist(_CANDLE_COLS)}) "
            f"VALUES ({_placeholders(_CANDLE_COLS)}) "
            f"ON CONFLICT (fixture_id, exchange, side, period_start, period_seconds) "
            f"DO UPDATE SET {updates}",
            rows,
        )
    return len(rows)


def set_backfill_state(conn: psycopg.Connection, *, fixture_id: int, exchange: str,
                       state: str, at: datetime) -> None:
    conn.execute(
        "UPDATE winner_market SET backfill_state = %s, backfilled_at = %s "
        "WHERE fixture_id = %s AND exchange = %s",
        (state, at, fixture_id, exchange),
    )


# --------------------------------------------------------------------------- #
# Reads the collectors need (the API's own queries live in api.py)             #
# --------------------------------------------------------------------------- #
def markets_in_play(conn: psycopg.Connection, now: datetime) -> list[dict]:
    """Every enrolled **Winner Market** whose Fixture is inside the **In-Play Window**.

    The window is expressed against `kickoff_utc` rather than against a match status,
    deliberately: the Published Store's statuses only advance at 04:00, so asking it
    whether a match is in progress would answer about last night. A postponement
    therefore leaves the window running over a match nobody turned up for, which the
    Track shows as a flat line — an honest picture of what the Exchange did.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT m.fixture_id, m.exchange, m.series_ticker, m.event_slug, m.league, "
            "       m.competition_id, m.kickoff_utc "
            "FROM winner_market m "
            "WHERE m.state = 'tracked' "
            "  AND m.kickoff_utc IS NOT NULL "
            "  AND m.kickoff_utc - make_interval(secs => %s) <= %s "
            "  AND m.kickoff_utc + make_interval(secs => %s) >= %s "
            "ORDER BY m.kickoff_utc, m.fixture_id, m.exchange",
            (IN_PLAY_LEAD_S, now, IN_PLAY_TAIL_S, now),
        ).fetchall()


def legs_for(conn: psycopg.Connection, fixture_id: int, exchange: str) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT side, team_id, exchange_team_id, exchange_team_name, market_ticker, "
            "       token_id, market_slug FROM winner_market_leg "
            "WHERE fixture_id = %s AND exchange = %s",
            (fixture_id, exchange),
        ).fetchall()


def counts(conn: psycopg.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m markets.store",
        description="Create and inspect the Market Store (ADR 0046).",
    )
    ap.add_argument("--init", action="store_true", help="create the schema (idempotent)")
    ap.add_argument("--counts", action="store_true", help="row count per table")
    args = ap.parse_args(argv)
    if not (args.init or args.counts):
        ap.error("nothing to do — pass --init or --counts")

    conn = get_conn()
    try:
        if args.init:
            init(conn)
            print(f"Market Store ready in {conn.info.dbname}: {', '.join(TABLES)}")
        if args.counts:
            for table, n in counts(conn).items():
                print(f"  {table:<20} {n:>12,}")
    finally:
        close_conn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
