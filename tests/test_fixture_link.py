"""The kickoff tolerance and the team-name anchor (ADR 0030, guarded per ADR 0033).

The silent failure this protects against is the worst one in the project: a wrong
`--fixture-id` that still verifies links commentary to a different match, and nothing
downstream can detect it. The rules are subtle enough to be easy to weaken by
accident during a refactor — particularly that `--force-link` waives team names but
**not** the anchor an inexact kickoff requires.

`_compare` is the single implementation of "is this the same match", shared by
`football.db` and the Published Store, so testing it covers both sources.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from commentary.fixture_link import (
    DELAY_TOLERANCE,  # noqa: F401 — imported so a rename breaks this file loudly
    KICKOFF_TOLERANCE,
    FixtureMismatch,
    _compare,
    _coerce_db_date,
)

KICKOFF = datetime(2026, 8, 2, 3, 0)


def espn_match(*, date=None, home="Cruz Azul", away="Atlante FC") -> dict:
    return {
        "game_id": "401877036",
        "date": (date or KICKOFF).strftime("%Y-%m-%dT%H:%MZ"),
        "home": {"team": home, "score": 1},
        "away": {"team": away, "score": 0},
    }


def db_row(*, date=None, home="Cruz Azul", away="Atlante FC") -> dict:
    return {
        "id": 1550903,
        "date": (date or KICKOFF).isoformat(sep=" "),
        "league_name": "Liga MX",
        "home_team_name": home,
        "away_team_name": away,
        "status": "FT",
        "home_goals": 1,
        "away_goals": 0,
    }


def compare(match, row, *, force=False):
    return _compare(1550903, row, match, force=force, source="test.db")


def test_tolerance_is_fifteen_minutes():
    assert KICKOFF_TOLERANCE == timedelta(minutes=15)


def test_exact_kickoff_and_exact_names_verifies():
    out = compare(espn_match(), db_row())
    assert out["name_mismatch"] is False
    assert out["verified_against"] == "test.db"


def test_inexact_kickoff_with_both_names_agreeing_verifies():
    """The Liga MX case ADR 0030 exists for: ESPN rounds to the hour, API-Football
    keeps the broadcast minute. Both names agree, so no force is needed."""
    out = compare(espn_match(), db_row(date=KICKOFF + timedelta(minutes=5)))
    assert out["name_mismatch"] is False


def test_inexact_kickoff_with_one_name_differing_needs_force():
    """'Cruz Azul' agrees, 'Atlante' vs 'Atlante FC' does not — a naming
    disagreement, which is exactly what --force-link is for."""
    row = db_row(date=KICKOFF + timedelta(minutes=5), away="Atlante")
    with pytest.raises(FixtureMismatch, match="teams do not"):
        compare(espn_match(), row)
    assert compare(espn_match(), row, force=True)["name_mismatch"] is True


def test_force_cannot_waive_the_anchor():
    """The crux of ADR 0030. An inexact kickoff plus waived names is a link where
    nothing at all was verified — Liga MX routinely runs two fixtures in the same
    broadcast slot, so a typo'd id lands on a genuinely different match."""
    row = db_row(date=KICKOFF + timedelta(minutes=5),
                 home="Atletico San Luis", away="Tijuana")
    for force in (False, True):
        with pytest.raises(FixtureMismatch, match="close but not exact"):
            compare(espn_match(), row, force=force)


def test_exact_kickoff_stands_on_its_own_without_an_anchor():
    """An *exact* kickoff needs no shared name — only a merely-close one does. With
    force it verifies even though neither team agrees."""
    row = db_row(home="Atletico San Luis", away="Tijuana")
    with pytest.raises(FixtureMismatch, match="teams do not"):
        compare(espn_match(), row)
    assert compare(espn_match(), row, force=True)["name_mismatch"] is True


@pytest.mark.parametrize("minutes", [16, 20, 60, 24 * 60])
def test_kickoff_beyond_tolerance_is_refused_however_hard_it_is_forced(minutes):
    """Beyond the tolerance with a team name disagreeing, nothing gets you a link.

    The disagreeing name matters and is why this test reads as it does: with BOTH
    names agreeing, a drift up to DELAY_TOLERANCE is now a delayed match and links
    (ADR 0038, covered in tests/test_delayed_link.py). What must never be reachable
    is a link where the clock is beyond tolerance *and* the names do not both agree,
    because then nothing has been verified at all — and `--force-link` waiving the
    names is precisely what would compound into that.
    """
    row = db_row(date=KICKOFF + timedelta(minutes=minutes), away="Atlante")
    for force in (False, True):
        with pytest.raises(FixtureMismatch, match="kickoff disagrees"):
            compare(espn_match(), row, force=force)


@pytest.mark.parametrize("minutes", [16, 20, 60])
def test_the_same_drift_links_when_both_names_agree(minutes):
    """The counterpart, so the pair documents the whole rule: identical drift, and the
    only difference is whether both names agree (ADR 0038)."""
    row = db_row(date=KICKOFF + timedelta(minutes=minutes))
    assert compare(espn_match(), row)["name_mismatch"] is False


def test_beyond_the_delay_window_nothing_links():
    """24h is past DELAY_TOLERANCE, so even both names agreeing cannot buy it."""
    row = db_row(date=KICKOFF + timedelta(hours=24))
    with pytest.raises(FixtureMismatch, match="kickoff disagrees"):
        compare(espn_match(), row)


@pytest.mark.parametrize("delta,ok", [
    (timedelta(minutes=15), True),
    (timedelta(minutes=15, seconds=1), False),
])
def test_the_tolerance_boundary(delta, ok):
    """The 15-minute edge, probed where it still decides the outcome.

    With both names agreeing the edge no longer decides anything — either side of it
    links, by a different rule (ADR 0038). It still decides for a row with a
    disagreeing name: inside, the anchor holds and --force-link covers the spelling;
    outside, there is no delayed path to fall back on.
    """
    row = db_row(date=KICKOFF + delta, away="Atlante")
    if ok:
        assert compare(espn_match(), row, force=True)["name_mismatch"] is True
    else:
        with pytest.raises(FixtureMismatch, match="kickoff disagrees"):
            compare(espn_match(), row, force=True)


def test_drift_is_symmetric():
    """ESPN may round up or down; the comparison takes an absolute difference."""
    for sign in (1, -1):
        row = db_row(date=KICKOFF + sign * timedelta(minutes=7))
        assert compare(espn_match(), row)["name_mismatch"] is False


def test_an_unparseable_kickoff_is_refused_not_treated_as_agreeing():
    with pytest.raises(FixtureMismatch, match="kickoff disagrees"):
        compare(espn_match(), db_row() | {"date": "not a date"})
    match = espn_match() | {"date": None}
    with pytest.raises(FixtureMismatch, match="kickoff disagrees"):
        compare(match, db_row())


def test_team_names_compare_case_and_whitespace_insensitively():
    row = db_row(home="  cruz   azul ", away="ATLANTE FC")
    assert compare(espn_match(), row)["name_mismatch"] is False


@pytest.mark.parametrize("value,expected", [
    ("2026-08-02 03:05:00", datetime(2026, 8, 2, 3, 5)),
    ("2026-08-02 03:05:00.000000", datetime(2026, 8, 2, 3, 5)),
    (datetime(2026, 8, 2, 3, 5), datetime(2026, 8, 2, 3, 5)),
    ("nonsense", None),
    (None, None),
])
def test_coerce_db_date_accepts_both_stores(value, expected):
    """SQLite hands back the text it stored; the Published Store hands back a naive
    datetime (ADR 0029). Both sources reach the same comparison."""
    assert _coerce_db_date(value) == expected
