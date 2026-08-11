"""Market API invariants that break without raising (ADR 0033/0046).

This app is the only process in the repo the internet reaches, so its failure modes are
the ones that ship a wrong picture to a reader rather than a stack trace to a log.

  **A write route.** Every other app here binds to loopback with a docstring saying why it
  must never be exposed. This one is exposed, so the absence of a write path is not a
  convention to be respected later — it is the property the exposure rests on, and a
  `POST` added in six months would not fail any other test.

  **A shared axis quietly dropped.** The two Tracks are routinely 190x different in span.
  Two panels auto-scaled independently render a month-long drift and a four-hour wobble at
  the same width, which is the lie by omission the glossary forbids for volume, made again
  with time. The server computes the axis so a client cannot forget to.

  **Blending the Exchanges.** No consensus number, no primary Exchange, no shared volume
  array — Kalshi counts contracts and Polymarket counts dollars and there is no conversion.

  **A gap drawn as a line.** A bucket missing one leg must vanish from the series and be
  counted, never interpolated or carried forward. Normalising over two of three legs
  invents a distribution nobody quoted.

  **Absence typed as null.** `not_covered`, `not_listed`, `unmapped` and `no_data` are four
  different situations, only one of which anybody can act on. A null for all four reads as
  "no information" when three of them are a specific claim.

Nothing here touches Postgres or an Exchange; the payload assembly is driven through a
fake connection.
"""
from __future__ import annotations

import pytest

from football_blog.track import normalise
from markets import api

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# --------------------------------------------------------------------------- #
# What makes it safe to expose                                                 #
# --------------------------------------------------------------------------- #
def test_the_app_exposes_no_write_route():
    offenders = [
        (r.path, sorted(set(r.methods) & WRITE_METHODS))
        for r in api.app.routes
        if getattr(r, "methods", None) and set(r.methods) & WRITE_METHODS
    ]
    assert offenders == [], (
        f"{offenders} — this app is reachable from the internet. A write path here is not "
        f"a style question; it is the property the exposure rests on (ADR 0046).")


def test_cors_is_one_origin_and_get_only():
    cors = [m for m in api.app.user_middleware if "CORS" in m.cls.__name__]
    assert len(cors) == 1, "the API must declare CORS explicitly, not inherit a default"
    options = cors[0].kwargs
    assert options["allow_methods"] == ["GET"]
    assert options["allow_credentials"] is False
    assert "*" not in options["allow_origins"]
    assert "https://lacancha.gerardomarr.com" in options["allow_origins"]


def test_the_app_never_reaches_for_a_writer():
    """A read API that can reach a writer is one refactor away from calling one.

    Checked against the compiled code rather than the source text, so that prose
    *explaining* there is no subprocess does not read as a subprocess.
    """
    import types

    forbidden = {"write_observation", "write_candles", "upsert_market",
                 "set_backfill_state", "init", "subprocess", "Popen", "run"}

    def names(code) -> set[str]:
        out = set(code.co_names)
        for const in code.co_consts:
            if hasattr(const, "co_names"):
                out |= names(const)
        return out

    reached: set[str] = set()
    for value in vars(api).values():
        if isinstance(value, types.FunctionType):
            reached |= names(value.__code__)
    assert not (reached & forbidden), f"api.py can reach {sorted(reached & forbidden)}"
    assert not any(isinstance(v, types.ModuleType) and v.__name__ == "subprocess"
                   for v in vars(api).values())


# --------------------------------------------------------------------------- #
# The two Exchanges stay apart                                                 #
# --------------------------------------------------------------------------- #
def test_volume_units_are_per_exchange_and_differ():
    assert api.VOLUME_UNIT == {"kalshi": "contracts", "polymarket": "usd"}


def test_an_absent_market_is_typed_not_null():
    block = api._exchange_block(None, 1, "kalshi", None, "auto", None)
    assert block["state"] == "not_listed"
    assert block["volume_unit"] == "contracts"
    assert block["probability"] == [] and block["book"] == []


# --------------------------------------------------------------------------- #
# The gap rule, at both resolutions                                            #
# --------------------------------------------------------------------------- #
def test_a_bucket_missing_a_leg_is_a_gap_at_minute_resolution():
    """The same refusal `football_blog.track` already makes hourly. It is asserted again
    at 60s because the Market Store introduced a second bucket size, and a rule that only
    held at one of them would be worse than no rule."""
    points, gaps = normalise({
        "home": {0: 0.40, 60: 0.42, 120: 0.44},
        "draw": {0: 0.28, 120: 0.26},          # 60 missing
        "away": {0: 0.30, 60: 0.30, 120: 0.28},
    })
    assert [p.t for p in points] == [0, 120]
    assert gaps == 1
    assert all(abs((p.home + p.draw + p.away) - 1.0) < 1e-9 for p in points)


def test_a_gap_is_never_carried_forward():
    points, _ = normalise({"home": {0: 0.4, 60: 0.9}, "draw": {0: 0.3},
                           "away": {0: 0.3, 60: 0.1}})
    assert [p.t for p in points] == [0]


def test_absence_before_an_exchange_listed_the_fixture_is_not_a_gap():
    """Polymarket lists ~4 weeks out and Kalshi 2–5 days. Counting Kalshi's silence over
    the first three weeks as gaps would report the structural difference as a fault."""
    _points, gaps = normalise({"home": {0: 0.4, 60: 0.4}, "draw": {60: 0.3},
                               "away": {0: 0.3, 60: 0.3}})
    assert gaps == 0


# --------------------------------------------------------------------------- #
# The shared axis                                                              #
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, rows): self._rows, self._out = rows, []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        for needle, rows in self._rows.items():
            if needle in sql:
                self._out = rows
                return self
        self._out = []
        return self
    def fetchall(self): return self._out
    def fetchone(self): return self._out[0] if self._out else None


class _FakeConn:
    def __init__(self, rows): self._rows = rows
    def cursor(self, **kw): return _FakeCursor(self._rows)


def _candle(side, t, mid):
    from datetime import datetime, timezone
    return {"side": side, "period_start": datetime.fromtimestamp(t, tz=timezone.utc),
            "period_seconds": 3600, "mid_close": mid, "volume": None,
            "open_interest": None}


def test_both_panels_span_the_earlier_first_point(monkeypatch):
    """Kalshi's Track is routinely hours old beside a Polymarket Track of a month, and
    both must be drawn on the earlier of the two starts."""
    long_side = [_candle(s, t, v) for t in (0, 3600, 7200)
                 for s, v in (("home", 0.4), ("draw", 0.3), ("away", 0.3))]
    short_side = [_candle(s, 7200, v)
                  for s, v in (("home", 0.5), ("draw", 0.25), ("away", 0.25))]

    market = {"state": "tracked", "backfill_state": "complete", "series_ticker": "S",
              "event_ticker": "E", "event_slug": None, "league": None,
              "enrolled_at": None, "last_seen_at": None, "kickoff_utc": None}

    real_block = api._exchange_block

    def fake_block(conn, fid, exchange, mkt, resolution, in_play_from):
        rows = long_side if exchange == "polymarket" else short_side
        conn = _FakeConn({"FROM market_candle": rows, "FROM market_quote": []})
        return real_block(conn, fid, exchange, market, resolution, in_play_from)

    monkeypatch.setattr(api.store, "get_conn", lambda: _FakeConn({}))
    monkeypatch.setattr(api, "_markets", lambda conn, fid: {"kalshi": market,
                                                            "polymarket": market})
    monkeypatch.setattr(api, "_fixture", lambda fid: None)
    monkeypatch.setattr(api, "_events", lambda fid, kickoff: [])
    monkeypatch.setattr(api, "_exchange_block", fake_block)

    payload = api._dashboard(1, "auto")
    assert payload["axis"]["t_from"] == 0, "the axis must start at the EARLIER Exchange"
    assert payload["axis"]["t_to"] == 7200
    assert payload["exchanges"]["kalshi"]["listed_from"] == 7200
    assert payload["exchanges"]["polymarket"]["listed_from"] == 0


def test_the_two_exchanges_are_separate_objects_in_the_payload(monkeypatch):
    monkeypatch.setattr(api.store, "get_conn", lambda: _FakeConn({}))
    monkeypatch.setattr(api, "_markets", lambda conn, fid: {})
    monkeypatch.setattr(api, "_fixture", lambda fid: {
        "id": 1, "date": None, "status": "NS", "season": 2026, "tournament": None,
        "round": None, "home_team_id": 1, "home_team_name": "H", "away_team_id": 2,
        "away_team_name": "A", "home_goals": None, "away_goals": None,
        "league_id": 253, "league_name": "MLS"})
    monkeypatch.setattr(api, "_events", lambda fid, kickoff: [])

    payload = api._dashboard(1, "auto")
    assert set(payload["exchanges"]) == {"kalshi", "polymarket"}
    assert payload["exchanges"]["kalshi"] is not payload["exchanges"]["polymarket"]
    # Nothing anywhere in the payload merges them.
    assert "consensus" not in payload and "blended" not in payload


def test_the_event_overlay_declares_its_vintage(monkeypatch):
    """The Published Store advances at 04:00 and only for Final Fixtures, so a chart
    watched live moves with no markers. An absent marker must not read as 'nothing
    happened' (the reason ADR 0044 refused a permanently-null live_minute)."""
    monkeypatch.setattr(api.store, "get_conn", lambda: _FakeConn({}))
    monkeypatch.setattr(api, "_markets", lambda conn, fid: {})
    monkeypatch.setattr(api, "_fixture", lambda fid: None)
    monkeypatch.setattr(api, "_events", lambda fid, kickoff: [])
    assert api._dashboard(1, "auto") is None      # nothing known at all -> 404 upstream

    monkeypatch.setattr(api, "_markets", lambda conn, fid: {"kalshi": {
        "state": "tracked", "backfill_state": "pending", "series_ticker": None,
        "event_ticker": None, "event_slug": None, "league": None, "enrolled_at": None,
        "last_seen_at": None, "kickoff_utc": None}})
    payload = api._dashboard(1, "auto")
    assert payload["events_vintage"] == "nightly"


def test_an_event_marker_is_flagged_as_an_estimate():
    """A match minute is not a wall clock: 45+3 and 48 are different moments and the
    Published Store records only the minute. Placing the marker is fine; presenting the
    placement as measured is not."""
    import inspect
    assert '"t_estimated": True' in inspect.getsource(api._events)


# --------------------------------------------------------------------------- #
# Resolution stitching                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("resolution", ["auto", "hour", "minute"])
def test_every_documented_resolution_is_accepted(resolution):
    route = next(r for r in api.app.routes
                 if getattr(r, "path", "") == "/api/fixtures/{fixture_id}/markets")
    pattern = route.dependant.query_params[0].field_info.metadata[0].pattern
    import re
    assert re.match(pattern, resolution)
