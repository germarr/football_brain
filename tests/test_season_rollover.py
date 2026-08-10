"""The Season rollover gate, and the registry write behind it (ADR 0045).

The silent failure this guards: `classify` decides whether a cron-visible verdict says
"roll", and every consequence of getting it wrong is quiet. Roll too early and Refresh
starts targeting a Season the provider has only announced — the old ADR 0018 objection.
Roll while the outgoing Season still has unplayed fixtures and those Finals are
*abandoned*, because Refresh only ever touches `max(seasons)`: nothing errors, the log
looks healthy, and the matches simply never arrive.

So the gate is tested as a pure function over provider-shaped payloads, and `add_season`
is tested for the one property that makes the registry diff reviewable — it adds an
integer and touches nothing else.
"""
from __future__ import annotations

import json

import pytest

from football import config
from football.onboard import rollover


def _record(*seasons: tuple[int, str]) -> dict:
    """A /leagues record carrying just what the gate reads: year and start date."""
    return {"seasons": [{"year": y, "start": s, "end": s} for y, s in seasons]}


def _fixture(status: str, date: str) -> dict:
    return {"fixture": {"status": {"short": status}, "date": f"{date}T19:00:00+00:00"}}


COMP = {"league_id": 203, "name": "Süper Lig", "seasons": [2024, 2025], "type": "league"}
TODAY = "2026-08-10"


def test_pinned_when_config_matches_the_provider():
    comp = {**COMP, "seasons": [2024, 2025, 2026]}
    v = rollover.classify(comp, _record((2026, "2026-08-14")), [], TODAY)
    assert v.action == "pinned" and v.newer is None


def test_rolls_when_the_season_is_imminent_and_the_frontier_is_closed():
    v = rollover.classify(
        COMP, _record((2025, "2025-08-08"), (2026, "2026-08-14")),
        [_fixture("FT", "2026-05-17"), _fixture("PST", "2026-05-10")], TODAY,
    )
    assert v.action == "roll" and v.newer == 2026 and v.starts == "2026-08-14"


def test_waits_on_a_season_the_provider_has_only_announced():
    """The Spanish Super Cup case: `current: true`, first match six months out.

    This is why the gate cannot be `max(year) > ours`, and why the provider's own
    `current` flag is not the signal — it means "query this by default"."""
    v = rollover.classify(COMP, _record((2026, "2027-02-02")), [], TODAY)
    assert v.action == "wait" and v.reason == "not-imminent"
    assert v.due == "2027-01-12"  # 21 days before the first match


def test_lead_days_is_the_boundary_and_it_is_inclusive():
    rec = _record((2026, "2026-08-31"))  # exactly 21 days out
    assert rollover.classify(COMP, rec, [], TODAY).action == "roll"
    assert rollover.classify(COMP, rec, [], "2026-08-09").action == "wait"


def test_an_unplayed_fixture_in_the_outgoing_season_blocks_the_roll():
    """The hazard ADR 0018 never named: rolling abandons the Season we leave behind."""
    v = rollover.classify(
        COMP, _record((2026, "2026-08-14")),
        [_fixture("FT", "2026-08-01"), _fixture("NS", "2026-08-12")], TODAY,
    )
    assert v.action == "wait" and v.reason == "frontier-open"


def test_a_year_old_unplayed_fixture_does_not_block_forever():
    """The FA Cup carried two `NS` fixtures dated 2025-08-05 into August 2026. A bare
    "any non-terminal" check would refuse that Competition every night, forever."""
    v = rollover.classify(
        COMP, _record((2026, "2026-08-14")), [_fixture("NS", "2025-08-05")], TODAY,
    )
    assert v.action == "roll"


@pytest.mark.parametrize("status", ["PST", "CANC", "ABD", "AWD", "WO"])
def test_a_settled_but_unplayed_fixture_does_not_block(status):
    """TERMINAL, not FINAL: a postponed match is never coming, so it strands nothing."""
    v = rollover.classify(
        COMP, _record((2026, "2026-08-14")), [_fixture(status, "2026-08-09")], TODAY,
    )
    assert v.action == "roll"


def test_no_record_is_unknown_rather_than_pinned():
    """A failed /leagues fetch must not read as "the provider agrees with config"."""
    v = rollover.classify(COMP, None, [], TODAY)
    assert v.action == "unknown" and v.newer is None


def test_every_verdict_renders_a_line():
    """`line()` is what the nightly log prints; an unhandled action would print nothing."""
    cases = [
        rollover.classify(COMP, _record((2026, "2026-08-14")), [], TODAY),
        rollover.classify(COMP, _record((2026, "2027-02-02")), [], TODAY),
        rollover.classify(COMP, _record((2026, "2026-08-14")),
                          [_fixture("NS", "2026-08-12")], TODAY),
        rollover.classify(COMP, None, [], TODAY),
        rollover.classify({**COMP, "seasons": [2026]}, _record((2026, "2026-08-14")),
                          [], TODAY),
    ]
    assert all(v.line().strip() for v in cases)


@pytest.fixture
def scratch_registry(tmp_path, monkeypatch):
    """A throwaway competitions file, with the module-level tables restored after."""
    path = tmp_path / "competitions.json"
    path.write_text(json.dumps([
        {"league_id": 203, "name": "Süper Lig", "seasons": [2024, 2025],
         "calendar_year": False, "type": "league"},
        {"league_id": 140, "name": "La Liga", "seasons": [2025, 2026],
         "calendar_year": False, "type": "league"},
    ], indent=2) + "\n")
    monkeypatch.setattr(config, "COMPETITIONS_FILE", path)
    config.reload_competitions()
    yield path
    monkeypatch.undo()
    config.reload_competitions()


def test_add_season_appends_one_integer_and_leaves_the_rest_alone(scratch_registry):
    before = json.loads(scratch_registry.read_text())
    assert rollover.add_season(203, 2026) is True
    after = json.loads(scratch_registry.read_text())

    assert after[0]["seasons"] == [2024, 2025, 2026]
    assert [r["league_id"] for r in after] == [r["league_id"] for r in before], "order moved"
    assert after[1] == before[1], "an unrelated Competition changed"
    assert {k: v for k, v in after[0].items() if k != "seasons"} == \
           {k: v for k, v in before[0].items() if k != "seasons"}


def test_add_season_is_idempotent(scratch_registry):
    assert rollover.add_season(140, 2026) is False
    assert json.loads(scratch_registry.read_text())[1]["seasons"] == [2025, 2026]


def test_add_season_refuses_an_untracked_competition(scratch_registry):
    with pytest.raises(SystemExit):
        rollover.add_season(999999, 2026)


def test_add_season_reloads_config_in_process(scratch_registry):
    """The caller acts on the new pin immediately — a stale table would re-roll it."""
    rollover.add_season(203, 2026)
    comp = next(c for c in config.COMPETITIONS if c["league_id"] == 203)
    assert max(comp["seasons"]) == 2026
