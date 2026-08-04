"""A build from the committed cache slice produces a populated store (ADR 0033).

The silent failure: `config.RAW_DIR` is a symlink to another volume (ADR 0002
addendum), and the parser is cache-only — `CachedClient(max_live_requests=0)`, so a
miss raises rather than fetching. Point it at the wrong place and the build still
"succeeds", just hollow. Nothing distinguishes an empty store from a legitimately
quiet one except counting rows, which is what this does.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import (
    SLICE_FIXTURES,
    SLICE_LEAGUE_ID,
    SLICE_LEAGUE_NAME,
    SLICE_SEASON,
    SLICE_TEAMS,
    SLICE_VENUES,
)

TARGETS = [(SLICE_LEAGUE_ID, SLICE_LEAGUE_NAME, SLICE_SEASON)]


def _counts(db_path) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        con.close()


@pytest.fixture
def built_db(carved_cache, temp_venue_registry, tmp_path):
    """One build, shared by the assertions below — it is the slow part of the suite."""
    from football import parse

    db_path = tmp_path / "test.db"
    parse.build(db_path=db_path, targets=TARGETS, register=True)
    return db_path


def test_build_produces_a_database(built_db):
    assert built_db.exists() and built_db.stat().st_size > 0


def test_structural_counts_match_the_slice(built_db):
    """Exact, because these are fixed by the carve and a drift means the slice
    changed under the suite rather than the parser regressing quietly."""
    counts = _counts(built_db)
    assert counts["competition"] == 1
    assert counts["fixture"] == SLICE_FIXTURES
    assert counts["team"] == SLICE_TEAMS
    assert counts["venue"] == SLICE_VENUES


@pytest.mark.parametrize("table", [
    "player", "squadentry", "event", "teammatchstat", "playerteam", "teamprofile",
])
def test_derived_tables_are_populated(built_db, table):
    """Non-zero rather than exact: these scale with the slice and their precise
    counts are not what a mis-resolved cache would change — their *presence* is."""
    assert _counts(built_db)[table] > 0


def test_every_fixture_carries_a_venue(built_db):
    """The Venue registry minted ids for this build (register=True against a temp
    registry), so a fixture with a null venue_id means the registry path broke."""
    con = sqlite3.connect(built_db)
    try:
        null_venues = con.execute(
            "SELECT count(*) FROM fixture WHERE venue_id IS NULL").fetchone()[0]
    finally:
        con.close()
    assert null_venues == 0


def test_the_temp_registry_was_written_not_the_committed_one(built_db, temp_venue_registry):
    """Guards the guard: if this fails, the suite has been minting venue ids into the
    real committed registry on every run."""
    assert temp_venue_registry.exists() and temp_venue_registry.stat().st_size > 0


def test_a_wrong_cache_path_fails_loudly(monkeypatch, temp_venue_registry, tmp_path):
    """A mis-resolved cache is one of the few dangers here that is *not* silent.

    ADR 0033 assumed a wrong `RAW_DIR` would quietly hollow the store out. It does
    not: the parser's client is `CachedClient(max_live_requests=0)`, so the first
    cache miss raises `QuotaExceeded` instead of fetching. Pinned here because that
    loudness is load-bearing — it is the reason ADR 0031 can move `parse.py` without
    risking a hollow rebuild, and a future change that gave the parser a live budget
    would turn this into exactly the silent failure the ADR feared.
    """
    from football import config, parse
    from football.client import QuotaExceeded

    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "wrong-place")
    with pytest.raises(QuotaExceeded):
        parse.build(db_path=tmp_path / "hollow.db", targets=TARGETS, register=True)


def test_a_read_only_build_silently_yields_no_venues(
    carved_cache, temp_venue_registry, tmp_path,
):
    """The silent hollow that *is* real, and the reason venue counts are asserted.

    `register=False` (the delta publish's read-only path, ADR 0028) leaves every
    ground unregistered against an empty registry — so the build succeeds, reports
    success, and produces zero venues with every fixture's venue_id null. No error.
    Only a row count distinguishes it from a correct build.
    """
    from football import parse

    db_path = tmp_path / "readonly.db"
    parse.build(db_path=db_path, targets=TARGETS, register=False)

    counts = _counts(db_path)
    assert counts["fixture"] == SLICE_FIXTURES, "fixtures still parse"
    assert counts["venue"] == 0, "no venue is minted on the read-only path"
