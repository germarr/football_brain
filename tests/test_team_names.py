"""Canonical team-name comparison, and the conflations it must never make (ADR 0039).

Two providers spell the same club differently in systematic ways — a club-type suffix,
an accent, acronym dots, a different word order — and exact-after-casefold saw none of
them. `_norm_team` reconciles exactly those, and nothing else.

The silent failure guarded here is the one ADR 0026 named and nothing since has fixed:
a wrong link is undetectable downstream. Loosening a name comparison is the easiest way
to cause one, because the *delayed-match* path (ADR 0038) rests its entire safety
argument on both names agreeing. So this file asserts the loosening from both sides —
what must now agree, and what must still not — and pins the token list, since every
longer one tested merged clubs that are genuinely distinct.

The case that prompted it: Charlotte v Pumas, Leagues Cup, 2026-08-05. ESPN recorded
`2026-08-05T01:22Z` against API-Football's scheduled `00:00`, an 82-minute delay well
inside DELAY_TOLERANCE — but ESPN says 'Charlotte FC' / 'Pumas UNAM' where we say
'Charlotte' / 'U.N.A.M. - Pumas', so neither name agreed and the delayed path was shut.
"""
from __future__ import annotations

import pytest

from commentary.fixture_link import (
    CLUB_TOKENS,
    FixtureMismatch,
    _compare,
    _norm_team,
    _unlinked_remedy,
)


# --------------------------------------------------------------------------- #
# The case this exists for
# --------------------------------------------------------------------------- #
def test_the_real_charlotte_pumas_link(capsys):
    """The exact refusal, end to end: both names reconciled, 82 min apart, linked."""
    match = {
        "game_id": "401863553", "date": "2026-08-05T01:22Z", "league": "Leagues Cup",
        "home": {"team": "Charlotte FC", "score": "3"},
        "away": {"team": "Pumas UNAM", "score": "0"},
    }
    row = {
        "id": 1530107, "date": "2026-08-05 00:00:00.000000", "league_name": "Leagues Cup",
        "home_team_name": "Charlotte", "away_team_name": "U.N.A.M. - Pumas",
        "status": "FT", "home_goals": 3, "away_goals": 0,
    }
    out = _compare(1530107, row, match, force=False, source="test.db")
    assert out["name_mismatch"] is False, "both names must agree, or the delay is refused"
    assert "82 min" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# What must now agree — one row per kind of difference
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("a,b,kind", [
    ("Charlotte FC", "Charlotte", "a club-type suffix"),
    ("FC Cincinnati", "Cincinnati", "the same suffix, as a prefix"),
    ("FC Cincinnati", "Cincinnati FC", "suffix on either side"),
    ("Pumas UNAM", "U.N.A.M. - Pumas", "acronym dots, and word order"),
    ("Penarol", "Peñarol", "an accent"),
    ("Hercules", "Hércules", "an accent"),
    ("Orlando City SC", "orlando  city   sc", "case and whitespace, as before"),
    ("Vancouver Whitecaps", "Whitecaps Vancouver", "word order alone"),
])
def test_the_same_club_spelled_two_ways_agrees(a, b, kind):
    assert _norm_team(a) == _norm_team(b), kind


# --------------------------------------------------------------------------- #
# What must still NOT agree — the reason the token list is only three long
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("a,b,why", [
    ("Liverpool", "AFC Liverpool", "fan-owned club, separate id, separate matches"),
    ("Blackpool", "AFC Blackpool", "likewise"),
    ("Bury", "Bury AFC", "the phoenix club is not the original"),
    ("Corinthians", "Corinthians USA", "different continent, same root name"),
    ("Lyon", "Club De Lyon", "a US club, not Olympique Lyonnais"),
    ("Toronto FC", "Toronto FC II", "a reserve side is not its first team"),
    ("Atlante FC", "Atlas", "two real and distinct Liga MX clubs"),
    ("Atletico San Luis", "San Luis", "a qualifier is not noise"),
    ("FC", "SC", "a name that is only a club token must not match every other"),
])
def test_distinct_clubs_still_disagree(a, b, why):
    assert _norm_team(a) != _norm_team(b), why


def test_the_token_list_stays_short():
    """Pinned deliberately (ADR 0033). Each of these was tested and rejected: adding
    any one of them merges clubs above that must stay apart, and the resulting mislink
    would be silent."""
    assert CLUB_TOKENS == {"fc", "cf", "sc"}
    for dangerous in ("afc", "usa", "de", "club", "ac", "cd", "ii"):
        assert dangerous not in CLUB_TOKENS


def test_normalisation_is_order_insensitive_but_not_content_insensitive():
    """Comparing token *sets* must not let a missing word slide."""
    assert _norm_team("Real Salt Lake") == _norm_team("Salt Lake Real")
    assert _norm_team("Real Salt Lake") != _norm_team("Salt Lake")


def test_an_empty_name_does_not_match_everything():
    assert _norm_team(None) == _norm_team("")
    assert _norm_team(None) != _norm_team("Charlotte")


# --------------------------------------------------------------------------- #
# The remedy a refusal offers depends on what the caller can actually do (ADR 0039)
# --------------------------------------------------------------------------- #
def test_a_refusal_does_not_advise_the_impossible():
    """`football_blog.pipeline` requires --fixture-id, so telling its operator to omit
    it is the same dead end ADR 0038 set out to remove."""
    assert "omit --fixture-id" in _unlinked_remedy(False)
    assert "omit --fixture-id" not in _unlinked_remedy(True)
    assert "--espn-id" in _unlinked_remedy(True)


def test_the_remedy_reaches_the_refusal_text():
    """Threaded, not merely defined — the flag is worthless if a raise site drops it."""
    match = {
        "game_id": "401863553", "date": "2026-08-07T01:22Z",
        "home": {"team": "Charlotte FC", "score": "3"},
        "away": {"team": "Pumas UNAM", "score": "0"},
    }
    row = {
        "id": 1530107, "date": "2026-08-05 00:00:00", "league_name": "Leagues Cup",
        "home_team_name": "Charlotte", "away_team_name": "U.N.A.M. - Pumas",
        "status": "FT", "home_goals": 3, "away_goals": 0,
    }
    with pytest.raises(FixtureMismatch) as e:
        _compare(1530107, row, match, force=False, source="test.db", link_required=True)
    assert "--espn-id" in str(e.value) and "omit --fixture-id" not in str(e.value)
