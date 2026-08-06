"""The Live Mirror stops shadowing a settled fixture (ADR 0024, widened).

The silent failure: a `livepoll` row is a *switch*. While one exists, the Viewer serves
the provisional Mirror for that fixture and ignores serve.db entirely (ADR 0022). So a
row that is never cleared does not error — it quietly keeps showing a half-finished poll
of a match that ended hours or days ago, on both the week table and the Match Tracker.

Two ways that used to happen, one test each below:

  * the fixture ended **without** a Final — postponed, cancelled, abandoned. The clear
    matched only ('FT','AET','PEN'), so nothing ever revisited it;
  * the fixture **aged out of the published window**. The clear inner-joined serve.db, and
    a fixture older than `--before` days is not in serve.db at all, so the join dropped
    the row and it stayed provisional permanently. Reachable whenever no publish runs for
    more than three days.

Both are guarded here rather than in an integration test because neither needs a real
publish: `_clear_settled_live_rows` takes the two stores and the window edge, and that
is the whole contract.
"""
from __future__ import annotations

import sqlite3

import pytest

from serving.publish import _clear_settled_live_rows

WINDOW_START = "2026-08-03 00:00:00"   # the published window's past edge


def _make_serve(path, fixtures):
    """A serve.db carrying just what the clear reads: fixture(id, status)."""
    con = sqlite3.connect(path)
    con.execute("create table fixture (id integer primary key, status text)")
    con.executemany("insert into fixture values (?,?)", fixtures)
    con.commit()
    con.close()


def _make_live(path, polls):
    """A live.db with the three tables the clear deletes from.
    `polls` is (fixture_id, kickoff_date, polled_at)."""
    con = sqlite3.connect(path)
    con.execute("create table fixture (id integer primary key, date text)")
    con.execute("create table event (fixture_id integer)")
    con.execute("create table livepoll (fixture_id integer primary key, polled_at text, status text)")
    for fid, date, polled in polls:
        con.execute("insert into fixture values (?,?)", (fid, date))
        con.execute("insert into event values (?)", (fid,))
        con.execute("insert into livepoll values (?,?,?)", (fid, polled, "1H"))
    con.commit()
    con.close()


def _remaining(path):
    con = sqlite3.connect(path)
    try:
        return {
            "livepoll": {r[0] for r in con.execute("select fixture_id from livepoll")},
            "fixture": {r[0] for r in con.execute("select id from fixture")},
            "event": {r[0] for r in con.execute("select fixture_id from event")},
        }
    finally:
        con.close()


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """A serve.db + live.db pair, with `config.ROOT` pointed at their parent."""
    import serving.publish as pub
    monkeypatch.setattr(pub.config, "ROOT", tmp_path)
    (tmp_path / "live").mkdir()
    return tmp_path / "serve.db", tmp_path / "live" / "live.db"


@pytest.mark.parametrize("status", ["FT", "AET", "PEN"])
def test_final_is_cleared(stores, status):
    """The original ADR 0024 behaviour, still intact."""
    serve, live = stores
    _make_serve(serve, [(1, status)])
    _make_live(live, [(1, "2026-08-05 20:00:00", "2026-08-05 21:50:00")])

    assert _clear_settled_live_rows(serve, WINDOW_START) == 1
    assert _remaining(live) == {"livepoll": set(), "fixture": set(), "event": set()}


@pytest.mark.parametrize("status", ["PST", "CANC", "ABD", "AWD", "WO"])
def test_ended_without_a_final_is_cleared(stores, status):
    """Ended, but not Final. Clearing only on Final left these provisional forever."""
    serve, live = stores
    _make_serve(serve, [(1, status)])
    _make_live(live, [(1, "2026-08-05 20:00:00", "2026-08-05 20:30:00")])

    assert _clear_settled_live_rows(serve, WINDOW_START) == 1
    assert _remaining(live)["livepoll"] == set()


@pytest.mark.parametrize("status", ["NS", "1H", "HT", "2H"])
def test_unsettled_is_kept(stores, status):
    """A match still in play must keep its overlay — that is the whole point of it."""
    serve, live = stores
    _make_serve(serve, [(1, status)])
    _make_live(live, [(1, "2026-08-05 20:00:00", "2026-08-05 20:30:00")])

    assert _clear_settled_live_rows(serve, WINDOW_START) == 0
    assert _remaining(live)["livepoll"] == {1}


def test_fixture_aged_out_of_the_window_is_cleared(stores):
    """serve.db will never carry this fixture again, so nothing else can ever clear it.

    The fixture is absent from serve.db entirely and kicked off before the window's past
    edge. Under the inner join this row survived every future publish.
    """
    serve, live = stores
    _make_serve(serve, [])                                   # published window missed it
    _make_live(live, [(1, "2026-07-28 20:00:00", "2026-07-28 21:00:00")])

    assert _clear_settled_live_rows(serve, WINDOW_START) == 1
    assert _remaining(live) == {"livepoll": set(), "fixture": set(), "event": set()}


def test_absent_but_still_inside_the_window_is_kept(stores):
    """Absent from serve.db but not yet aged out — a later publish may still adjudicate
    it, so clearing now would throw away a live overlay of a match in progress."""
    serve, live = stores
    _make_serve(serve, [])
    _make_live(live, [(1, "2026-08-06 20:00:00", "2026-08-06 20:30:00")])

    assert _clear_settled_live_rows(serve, WINDOW_START) == 0
    assert _remaining(live)["livepoll"] == {1}


def test_mixed_batch_clears_only_what_it_should(stores):
    """The three cases together, since they are decided by one SQL statement."""
    serve, live = stores
    _make_serve(serve, [(1, "FT"), (2, "PST"), (3, "2H")])
    _make_live(live, [
        (1, "2026-08-05 20:00:00", "2026-08-05 21:50:00"),   # Final        -> cleared
        (2, "2026-08-05 20:00:00", "2026-08-05 20:10:00"),   # postponed    -> cleared
        (3, "2026-08-06 19:00:00", "2026-08-06 19:45:00"),   # in play      -> kept
        (4, "2026-07-28 20:00:00", "2026-07-28 21:00:00"),   # aged out     -> cleared
        (5, "2026-08-06 22:00:00", "2026-08-06 22:05:00"),   # not yet published -> kept
    ])

    assert _clear_settled_live_rows(serve, WINDOW_START) == 3
    assert _remaining(live) == {"livepoll": {3, 5}, "fixture": {3, 5}, "event": {3, 5}}


def test_missing_live_db_is_not_an_error(stores):
    """No Mirror at all is the normal case on a machine that never polls."""
    serve, live = stores
    _make_serve(serve, [(1, "FT")])
    assert not live.exists()
    assert _clear_settled_live_rows(serve, WINDOW_START) == 0
