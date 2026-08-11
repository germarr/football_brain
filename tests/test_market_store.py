"""Market Store invariants that break without raising (ADR 0033/0046).

Every guard here covers a failure that returns HTTP 200, writes a row, and draws a chart
that is quietly untrue. Nothing in this file touches an Exchange.

  **A foreign key into the Published Store would vanish on the next publish.** The
  wholesale publish runs `DROP TABLE public.fixture CASCADE`, and CASCADE drops the
  constraints pointing at it. The schema would read stronger than it is, on the one store
  that cannot be rebuilt — and nothing would say so, because dropping a constraint is not
  an error. Same for a table name colliding with a published one: it would be swapped out
  from under us and the loss would look like a collector that stopped writing.

  **Two legs are not a small Observation.** Normalising over two of three legs invents a
  distribution nobody quoted. The refusal has to live at the write, because a partial row
  in the store is a row every reader has to remember to discard.

  **A settled market must not enter as an Observation.** Its published prices are 1 and 0
  — the outcome, not a forecast — and stamped with the moment we swept it they would read
  as somebody having quoted a certainty months after the match.

  **The two Exchanges publish different subsets, and null means "not published".** Writing
  Polymarket's `no_bid` as `1 - yes_ask`, or Kalshi's `liquidity` as its literal `0.0000`,
  puts a number in a column that promises what the Exchange said.

  **A Kalshi candle is filed by the start of its period, not the end.** `end_period_ts`
  is the close; filing by it would put a Kalshi bar and the Polymarket point covering the
  same real minute an hour apart, and the two lines would disagree for no reason.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from markets import backfill, exchanges, store

UTC = timezone.utc
KICKOFF = datetime(2026, 8, 8, 20, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The store must survive a wholesale publish                                   #
# --------------------------------------------------------------------------- #
def test_schema_declares_no_foreign_keys():
    """A REFERENCES here is dropped, silently, by the next `publish_pg`."""
    assert "REFERENCES" not in store.SCHEMA.upper(), (
        "The Market Store must reference the Published Store by integer only. "
        "`football/publish/pg.py` drops `public.fixture` with CASCADE on every wholesale "
        "publish, which takes any FK pointing at it and reports nothing.")


def test_no_table_name_collides_with_a_published_table():
    from football.publish.pg import COMMENTARY_TABLES, FOOTBALL_TABLES
    published = set(FOOTBALL_TABLES) | set(COMMENTARY_TABLES)
    assert not (set(store.TABLES) & published)


def test_every_declared_table_is_in_TABLES():
    """`TABLES` is what the guards above check. A table created but not listed is
    unguarded — and would be the one a publish eats."""
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", store.SCHEMA))
    assert created == set(store.TABLES)


# --------------------------------------------------------------------------- #
# Three legs or none                                                           #
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, sink): self.sink = sink
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def executemany(self, sql, rows): self.sink.extend(rows)
    def execute(self, sql, params=None): return self


class _FakeTransaction:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeConn:
    """Just enough psycopg to see what a writer tried to write."""
    def __init__(self): self.rows: list = []
    def cursor(self, **kw): return _FakeCursor(self.rows)
    def execute(self, sql, params=None): return self
    def transaction(self): return _FakeTransaction()


def _quotes(sides=("home", "draw", "away")):
    return [store.Quote(side=s, yes_bid=0.4, yes_ask=0.42, mid=0.41,
                        volume=10.0, volume_unit="contracts") for s in sides]


@pytest.mark.parametrize("sides", [
    ("home", "draw"),                 # a leg the sweep did not carry
    ("home", "away"),                 # the draw missing — the leg most easily forgotten
    ("home", "draw", "draw"),         # three rows, two distinct sides
    (),
])
def test_a_partial_observation_is_not_written(sides):
    conn = _FakeConn()
    assert store.write_observation(conn, fixture_id=1, exchange="kalshi",
                                   observed_at=KICKOFF, quotes=_quotes(sides)) == 0
    assert conn.rows == []


def test_a_complete_observation_writes_one_row_per_leg():
    conn = _FakeConn()
    assert store.write_observation(conn, fixture_id=1, exchange="kalshi",
                                   observed_at=KICKOFF, quotes=_quotes()) == 3
    assert len(conn.rows) == 3


def test_settled_enrolment_writes_no_observation():
    """`--settled` sweeps markets quoting 1 and 0. Those are outcomes, and stamping them
    with the sweep time would file a certainty as a forecast (ADR 0040's freeze, again)."""
    from markets import watch
    sweep = exchanges.Sweep(exchange="kalshi", raw={})
    market = type("M", (), {"outcomes": [{"side": s, "market_ticker": f"T-{s}"}
                                         for s in ("home", "draw", "away")]})()
    _, written = watch._enrol(
        _FakeConn(), sweep, market, fixture_id=1, competition_id=253, kickoff_utc=KICKOFF,
        home=(1, "H"), away=(2, "A"), now=KICKOFF, dry_run=False, observe=False)
    assert written == 0


# --------------------------------------------------------------------------- #
# Null means "not published"                                                   #
# --------------------------------------------------------------------------- #
def test_kalshi_quote_reads_the_dollar_fields_and_both_sizes():
    raw = {
        "yes_bid_dollars": "0.1800", "yes_ask_dollars": "0.6800",
        "no_bid_dollars": "0.3200", "no_ask_dollars": "0.8200",
        "last_price_dollars": "0.0000", "volume_fp": "34197.00",
        "open_interest_fp": "162.00", "volume_24h_fp": "12.00",
        "yes_bid_size_fp": "30.00", "yes_ask_size_fp": "100.00",
        "liquidity_dollars": "0.0000",
        # The dead integer-cent twins (ADR 0041). Reading these yields None on every
        # market now, and a Quote built from them looks exactly like an unquoted market.
        "yes_bid": None, "yes_ask": None, "volume": None,
    }
    q = exchanges.kalshi_quote("home", raw)
    assert (q.yes_bid, q.yes_ask, q.no_bid, q.no_ask) == (0.18, 0.68, 0.32, 0.82)
    assert (q.yes_bid_size, q.yes_ask_size) == (30.0, 100.0)
    assert q.volume == 34197.0 and q.volume_unit == "contracts"
    assert q.open_interest == 162.0
    assert q.mid == pytest.approx((0.18 + 0.68) / 2)
    assert q.liquidity is None, "Kalshi's liquidity_dollars reads 0.0000 on a market with "\
                                "34,197 contracts traded — storing it asserts no depth"


def test_polymarket_quote_leaves_the_no_side_and_open_interest_null():
    raw = {"bestBid": "0.39", "bestAsk": "0.40", "lastTradePrice": "0.395",
           "volume": "41814.17", "volume24hr": "23.22", "liquidity": "1485.01",
           "outcomes": '["Yes", "No"]', "outcomePrices": '["0.395", "0.605"]'}
    q = exchanges.polymarket_quote("away", raw)
    assert q.volume_unit == "usd" and q.volume == 41814.17
    assert q.liquidity == 1485.01
    assert q.mid == 0.395, "the mid must be Polymarket's own outcomePrices — the CLOB "\
                           "price history IS that number, so a Candle and an Observation "\
                           "of one minute must be on one scale"
    assert q.no_bid is None and q.no_ask is None, "1 - yes_ask is arithmetic, not a quote"
    assert q.open_interest is None, "Gamma publishes none; 0 would claim an empty book"


def test_the_two_exchanges_never_share_a_volume_unit():
    k = exchanges.kalshi_quote("home", {"volume_fp": "139"})
    p = exchanges.polymarket_quote("home", {"volume": "41814.17"})
    assert k.volume_unit != p.volume_unit
    assert {k.volume_unit, p.volume_unit} == {"contracts", "usd"}


def test_quotes_for_refuses_a_leg_whose_payload_the_sweep_missed():
    outcomes = [{"side": s, "market_ticker": f"T-{s}"} for s in ("home", "draw", "away")]
    raw = {"T-home": {"yes_bid_dollars": "0.4"}, "T-draw": {"yes_bid_dollars": "0.3"}}
    assert exchanges.quotes_for("kalshi", outcomes, raw) == []


# --------------------------------------------------------------------------- #
# The In-Play Window, and the two resolutions                                  #
# --------------------------------------------------------------------------- #
def test_window_is_open_at_both_edges_and_shut_outside_them():
    lead, tail = store.IN_PLAY_LEAD_S, store.IN_PLAY_TAIL_S
    assert (lead, tail) == (15 * 60, 150 * 60)
    for offset, inside in [(-lead, True), (-lead - 60, False),
                           (0, True), (tail, True), (tail + 60, False)]:
        now = KICKOFF + timedelta(seconds=offset)
        open_now = (KICKOFF - timedelta(seconds=lead) <= now
                    <= KICKOFF + timedelta(seconds=tail))
        assert open_now is inside, f"{offset}s from kickoff"


def test_an_unfinished_fixture_gets_the_runup_only_and_stays_partial():
    spans, state = backfill._spans(KICKOFF, KICKOFF - timedelta(days=2))
    assert state == "partial"
    assert {p for _s, _e, p in spans} == {store.RESOLUTION_RUNUP_S}


def test_a_finished_fixture_gets_both_resolutions_and_completes():
    spans, state = backfill._spans(KICKOFF, KICKOFF + timedelta(days=1))
    assert state == "complete"
    assert {p for _s, _e, p in spans} == {store.RESOLUTION_RUNUP_S,
                                          store.RESOLUTION_IN_PLAY_S}
    runup = next(s for s in spans if s[2] == store.RESOLUTION_RUNUP_S)
    in_play = next(s for s in spans if s[2] == store.RESOLUTION_IN_PLAY_S)
    assert runup[1] == in_play[0], "the two spans must meet at the window's left edge, "\
                                   "so the stitched line has no hole and no overlap"


# --------------------------------------------------------------------------- #
# Candles                                                                      #
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload): self._payload = payload
    def raise_for_status(self): return None
    def json(self): return self._payload


class _FakeClient:
    def __init__(self, payload): self.payload, self.calls = payload, []
    def get(self, url, params=None):
        self.calls.append((url, params or {}))
        return _FakeResponse(self.payload)


def test_a_kalshi_candle_is_filed_by_the_start_of_its_period():
    end = 1786402800                       # hour-aligned, as Kalshi's always are
    client = _FakeClient({"candlesticks": [{
        "end_period_ts": end,
        "yes_bid": {"open_dollars": "0.18", "high_dollars": "0.18",
                    "low_dollars": "0.18", "close_dollars": "0.18"},
        "yes_ask": {"open_dollars": "0.71", "high_dollars": "0.71",
                    "low_dollars": "0.69", "close_dollars": "0.70"},
        "price": {},                        # no trade this period — most periods
        "volume_fp": "0.00", "open_interest_fp": "0.00",
    }]})
    candles = backfill.kalshi_candles(client, "KXMLSGAME", "T", "home",
                                      end - 7200, end, 3600)
    assert len(candles) == 1
    c = candles[0]
    assert c.period_start == datetime.fromtimestamp(end - 3600, tz=UTC)
    assert c.mid_close == pytest.approx((0.18 + 0.70) / 2)
    assert c.price_close is None, "an empty `price` block is no trade, not a price of 0 "\
                                  "— reading it as data is the mistake ADR 0043 corrected"


def test_a_kalshi_span_over_the_response_ceiling_raises_rather_than_truncating():
    client = _FakeClient({"candlesticks": []})
    with pytest.raises(ValueError, match="ceiling"):
        backfill.kalshi_candles(client, "S", "T", "home", 0, 60 * 86400, 60)


def test_polymarket_asks_for_a_bounded_span_only_when_the_clob_accepts_one():
    """Measured limit: 14 days returns 200, 20 days returns
    `400 invalid filters: 'startTs' and 'endTs' interval is too long`."""
    client = _FakeClient({"history": []})
    backfill.polymarket_candles(client, "tok", "home", 0, 10 * 86400, 3600)
    backfill.polymarket_candles(client, "tok", "home", 0, 30 * 86400, 3600)
    narrow, wide = client.calls[0][1], client.calls[1][1]
    assert "startTs" in narrow and "interval" not in narrow
    assert wide.get("interval") == "max" and "startTs" not in wide


def test_polymarket_points_outside_the_asked_span_are_dropped():
    """`interval=max` returns the token's whole history, so trimming is not optional."""
    client = _FakeClient({"history": [{"t": 100, "p": 0.4}, {"t": 100_000, "p": 0.6}]})
    candles = backfill.polymarket_candles(client, "tok", "home", 0, 3600, 60)
    assert [c.mid_close for c in candles] == [0.4]


def test_the_later_point_in_a_bucket_wins():
    client = _FakeClient({"history": [{"t": 10, "p": 0.4}, {"t": 50, "p": 0.9}]})
    candles = backfill.polymarket_candles(client, "tok", "home", 0, 60, 60)
    assert [c.mid_close for c in candles] == [0.9]


# --------------------------------------------------------------------------- #
# The declared schema against the live one                                     #
# --------------------------------------------------------------------------- #
def test_the_live_schema_matches_the_declared_one():
    """Guards drift the other way round: a column added to Postgres by hand, or a
    `--init` never re-run after `SCHEMA` changed, both leave writers inserting into a
    table that is not the one this file describes."""
    try:
        conn = store.get_conn()
    except Exception as exc:                                   # pragma: no cover
        pytest.skip(f"Postgres not reachable ({type(exc).__name__}) — "
                    f"this guard needs the real store")
    try:
        for table in store.TABLES:
            live = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s", (table,)).fetchall()}
            if not live:
                pytest.skip(f"{table} does not exist yet — run "
                            f"`python -m markets.store --init`")
            block = store.SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1]
            block = block.split("\n);", 1)[0]
            # Column lines are indented four spaces; so is `PRIMARY KEY (...)`, which is
            # a constraint rather than a column.
            declared = {m for m in re.findall(r"^\s{4}([a-z_][a-z0-9_]*)\s", block, re.M)}
            assert declared == live, f"{table}: declared {declared ^ live} differs"
    finally:
        store.close_conn()
