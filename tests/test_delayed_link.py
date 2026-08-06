"""The delayed-match link path, and the boundaries that keep it safe (ADR 0038).

A wrong link is undetectable downstream — ADR 0026 said so and nothing since has
changed it — so the whole guard is the anchor. These assert it from both sides: what the
85-minute Leagues Cup delay must now admit, and what must still be refused however hard
it is forced.

The case that prompted it: Monterrey v Orlando City, 2026-08-05. ESPN recorded
`2026-08-06T00:55Z`, API-Football kept the scheduled `23:30`. Every other Leagues Cup
match on that slate agreed exactly, so it was a delay, not a rounding convention.
"""
from __future__ import annotations

import pytest

from commentary.fixture_link import (
    DELAY_TOLERANCE,
    KICKOFF_TOLERANCE,
    FixtureMismatch,
    _compare,
)

HOME, AWAY = "Monterrey", "Orlando City SC"


def espn(date: str, home: str = HOME, away: str = AWAY) -> dict:
    return {
        "game_id": "401863559", "date": date, "league": "Leagues Cup",
        "home": {"team": home, "score": "1"}, "away": {"team": away, "score": "2"},
    }


def ours(date: str = "2026-08-05 23:30:00", home: str = HOME, away: str = AWAY) -> dict:
    return {
        "id": 1530116, "date": date, "league_name": "Leagues Cup",
        "home_team_name": home, "away_team_name": away,
        "status": "FT", "home_goals": 1, "away_goals": 2,
    }


def link(match, row, force=False):
    return _compare(1530116, row, match, force=force, source="test.db")


# --------------------------------------------------------------------------- #
# The case this exists for
# --------------------------------------------------------------------------- #
def test_the_real_leagues_cup_delay_links(capsys):
    """85 minutes apart, both names agreeing — exactly what was refused before."""
    out = link(espn("2026-08-06T00:55Z"), ours())
    assert out["name_mismatch"] is False
    printed = capsys.readouterr().out
    assert "85 min" in printed and "delayed match" in printed, (
        "a delayed link must announce its drift, not report a clean verification"
    )


def test_a_delayed_link_needs_no_flag():
    """Automatic by design: there is no judgment to assert when both names agree."""
    assert link(espn("2026-08-06T00:55Z"), ours(), force=False)


# --------------------------------------------------------------------------- #
# The boundaries
# --------------------------------------------------------------------------- #
def test_beyond_the_delay_window_is_refused():
    """Seven hours is not a delay; it is our data being wrong, which we want to hear."""
    with pytest.raises(FixtureMismatch) as e:
        link(espn("2026-08-06T06:30Z"), ours())
    assert "beyond both" in str(e.value)


def test_one_agreeing_name_is_not_enough_for_a_delay():
    """The ADR 0030 anchor (one name) is deliberately too weak at this range."""
    with pytest.raises(FixtureMismatch) as e:
        link(espn("2026-08-06T00:55Z", away="Orlando City"), ours())
    assert "BOTH team names" in str(e.value)


def test_force_cannot_buy_a_delayed_link():
    """`--force-link` waives naming, and a naming disagreement is exactly what
    disqualifies a match from this path. Forcing must not compound the two."""
    with pytest.raises(FixtureMismatch):
        link(espn("2026-08-06T00:55Z", away="Orlando City"), ours(), force=True)


def test_neither_name_agreeing_is_refused_at_delay_range():
    with pytest.raises(FixtureMismatch) as e:
        link(espn("2026-08-06T00:55Z", home="Tigres", away="Seattle"), ours())
    assert "neither does" in str(e.value)


# --------------------------------------------------------------------------- #
# ADR 0030's rules are untouched
# --------------------------------------------------------------------------- #
def test_exact_kickoff_still_links():
    assert link(espn("2026-08-05T23:30Z"), ours())["name_mismatch"] is False


def test_the_liga_mx_rounding_case_still_links_on_one_name():
    """5 minutes apart with one name differing: inside KICKOFF_TOLERANCE, anchored by
    the agreeing name, needing --force-link for the spelling. ADR 0030's whole point."""
    m, r = espn("2026-08-02T03:00Z", home="Cruz Azul", away="Atlante"), ours(
        "2026-08-02 03:05:00", home="Cruz Azul", away="Atlante FC")
    with pytest.raises(FixtureMismatch):
        link(m, r)                      # names disagree -> refused without force
    assert link(m, r, force=True)       # ...and forced, it links


def test_close_kickoff_with_no_shared_name_is_still_refused_even_forced():
    m, r = espn("2026-08-05T23:35Z", home="Tigres", away="Seattle"), ours()
    with pytest.raises(FixtureMismatch) as e:
        link(m, r, force=True)
    assert "nothing left" in str(e.value)


def test_the_two_windows_stay_distinct():
    """If these are ever merged, ADR 0030's neighbour-safety argument is gone."""
    assert KICKOFF_TOLERANCE < DELAY_TOLERANCE
    assert KICKOFF_TOLERANCE.total_seconds() == 15 * 60


# --------------------------------------------------------------------------- #
# The refusal names its own remedy (ADR 0038)
# --------------------------------------------------------------------------- #
def test_a_kickoff_refusal_says_force_link_cannot_help():
    """The defect that started this: the operator was sent to an impossible flag."""
    with pytest.raises(FixtureMismatch) as e:
        link(espn("2026-08-06T06:30Z"), ours())
    msg = str(e.value)
    assert "--force-link cannot help" in msg
    assert "omit --fixture-id" in msg
