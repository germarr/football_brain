"""Match Preview invariants that break without raising (ADR 0033, guarding ADR 0040/0041).

Everything here shares one property: the wrong behaviour renders a card. Nothing throws,
no page 500s, and the damage is a number that is quietly untrue — which is the only kind
of failure this suite exists for.

Four of them are worth stating outright, because each was a real fork in the design:

  **The freeze must not rewrite the market half.** A settled Match Preview is the one
  record here that starts derived and stops being so: its football half stays computable
  forever, but nothing reconstructs what the market thought an hour before kickoff. A
  freeze that also refreshed the Quote would silently replace the interesting artifact
  with a post-hoc one, and the card would look completely normal afterwards.

  **A half-resolved Winner Market must be refused whole.** Attaching the one side that
  mapped, and inferring the other from the ticker or the date, is exactly the guess ADR
  0041 declines to make — and a market attributed to the wrong club reads as a confident,
  perfectly-formatted preview.

  **Team Leaders must always carry their scope.** "1 goal" is true of a three-game
  campaign and false of a season. Only the label distinguishes them, so a Leader without
  one is worse than no Leader at all.

  **Scheduled Fixtures are selected by whitelist.** The store spells cancelled two ways
  (`CANC`, `Canc`); a blacklist previews five cancelled matches and reports nothing wrong.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from football import standings as standings_mod
from football.status import ENDED_WITHOUT_RESULT, FINAL
from football_blog import kalshi
from football_blog.preview import SCHEDULED, _market_block, _team_block

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
DRAW_UUID = "111193d4-9b1f-4bd8-ab7c-9de252737f05"
HOME_UUID = "aaaaaaaa-0000-0000-0000-000000000001"
AWAY_UUID = "bbbbbbbb-0000-0000-0000-000000000002"


def _registry(tmp_path, teams: dict | None = None) -> kalshi.Registry:
    """A registry file on disk, so `load_registry` is exercised rather than bypassed."""
    doc = {
        "series": {"KXTESTGAME": 999},
        "draw_uuid": DRAW_UUID,
        "teams": teams if teams is not None else {
            HOME_UUID: {"team_id": 11, "kalshi_name": "Home", "our_name": "Home FC"},
            AWAY_UUID: {"team_id": 22, "kalshi_name": "Away", "our_name": "Away FC"},
        },
    }
    path = tmp_path / "kalshi_teams.json"
    path.write_text(json.dumps(doc))
    return kalshi.load_registry(path)


def _market(uuid: str, sub_title: str, bid: str, ask: str, volume: str = "1000.00") -> dict:
    return {
        "ticker": f"KXTESTGAME-26AUG15HOMAWY-{sub_title[:3].upper()}",
        "event_ticker": "KXTESTGAME-26AUG15HOMAWY",
        "yes_sub_title": sub_title,
        "custom_strike": {"soccer_team": uuid},
        "yes_bid_dollars": bid, "yes_ask_dollars": ask,
        "last_price_dollars": ask, "volume_fp": volume, "open_interest_fp": volume,
        # The dead integer-cent fields, present and null exactly as Kalshi returns them.
        "yes_bid": None, "yes_ask": None, "last_price": None, "volume": None,
    }


def _three_legs() -> list[dict]:
    return [
        _market(HOME_UUID, "Home", "0.60", "0.62"),
        _market(DRAW_UUID, "Tie", "0.20", "0.22"),
        _market(AWAY_UUID, "Away", "0.18", "0.20"),
    ]


# --------------------------------------------------------------------------- #
# The refusal: a Winner Market is attached whole or not at all
# --------------------------------------------------------------------------- #
def test_both_clubs_map_so_the_market_attaches(tmp_path):
    reg = _registry(tmp_path)
    index, unmapped = kalshi.index_by_team_pair(_three_legs(), "KXTESTGAME", reg)
    assert unmapped == []
    assert len(index) == 1
    market = index[(frozenset({11, 22}), date(2026, 8, 15))]
    assert market.state == "quoted"


def test_one_unmapped_club_refuses_the_whole_market(tmp_path):
    """The away club is missing, so *neither* side attaches.

    The tempting bug is to keep the home leg — it resolved, after all — and infer the
    away club from the ticker's `AWY` or from the two teams in the Fixture. That produces
    a card with a probability against a club nobody verified, and it looks fine.
    """
    reg = _registry(tmp_path, teams={
        HOME_UUID: {"team_id": 11, "kalshi_name": "Home", "our_name": "Home FC"},
    })
    index, unmapped = kalshi.index_by_team_pair(_three_legs(), "KXTESTGAME", reg)

    assert index == {}, "a half-resolved Winner Market must not attach at all"
    assert [u["kalshi_team"] for u in unmapped] == [AWAY_UUID]
    assert unmapped[0]["kalshi_name"] == "Away"


def test_the_draw_is_recognised_by_uuid_not_by_the_word_tie(tmp_path):
    """Kalshi could rename the label; the UUID is the identity (ADR 0041)."""
    legs = _three_legs()
    for leg in legs:
        if leg["custom_strike"]["soccer_team"] == DRAW_UUID:
            leg["yes_sub_title"] = "Empate"      # a rename must change nothing
    reg = _registry(tmp_path)
    index, unmapped = kalshi.index_by_team_pair(legs, "KXTESTGAME", reg)
    assert unmapped == []
    market = index[(frozenset({11, 22}), date(2026, 8, 15))]
    sides = {o["team_id"] for o in market.outcomes}
    assert sides == {11, 22, None}, "the renamed leg must still be the draw"


def test_a_team_named_tie_is_not_mistaken_for_the_draw(tmp_path):
    """The converse: matching on the word would swallow a real club called 'Tie'."""
    legs = _three_legs()
    for leg in legs:
        if leg["custom_strike"]["soccer_team"] == HOME_UUID:
            leg["yes_sub_title"] = "Tie"
    reg = _registry(tmp_path)
    index, _unmapped = kalshi.index_by_team_pair(legs, "KXTESTGAME", reg)
    market = index[(frozenset({11, 22}), date(2026, 8, 15))]
    drawn = [o for o in market.outcomes if o["team_id"] is None]
    assert len(drawn) == 1 and drawn[0]["kalshi_team"] == DRAW_UUID


# --------------------------------------------------------------------------- #
# Market Probability: ours, normalised, and never invented from a partial book
# --------------------------------------------------------------------------- #
def test_probabilities_normalise_to_one():
    quotes = [{"bid": 0.60, "ask": 0.62}, {"bid": 0.20, "ask": 0.22},
              {"bid": 0.18, "ask": 0.20}]
    probs, overround = kalshi.market_probabilities(quotes)
    assert probs is not None
    assert sum(probs) == pytest.approx(1.0)
    # The raw mids sum to 1.01 — the overround is kept so the normalisation is auditable.
    assert overround == pytest.approx(1.01)


def test_a_missing_leg_yields_no_probabilities_at_all():
    """Normalising over two of three legs invents a distribution nobody quoted."""
    quotes = [{"bid": 0.60, "ask": 0.62}, {"bid": None, "ask": None},
              {"bid": 0.18, "ask": 0.20}]
    probs, overround = kalshi.market_probabilities(quotes)
    assert probs is None and overround is None


def test_dead_cent_fields_are_never_read(tmp_path):
    """`yes_bid`/`volume` return null on every live market; only `_dollars`/`_fp` count.

    Reading the cent fields does not raise — it silently produces a card with no prices,
    which is what `predicitons/fastapi_server/` does today.
    """
    quote = kalshi.quote_of(_market(HOME_UUID, "Home", "0.66", "0.67", volume="34197.18"))
    assert quote["bid"] == 0.66 and quote["ask"] == 0.67
    assert quote["volume"] == 34197.18, "depth must come from volume_fp"


def test_volume_survives_onto_the_outcome(tmp_path):
    """A 34,197-contract Quote and a 162-contract one are not the same claim."""
    legs = _three_legs()
    legs[0]["volume_fp"] = "34197.18"
    reg = _registry(tmp_path)
    index, _ = kalshi.index_by_team_pair(legs, "KXTESTGAME", reg)
    market = index[(frozenset({11, 22}), date(2026, 8, 15))]
    home = next(o for o in market.outcomes if o["team_id"] == 11)
    assert home["quote"]["volume"] == 34197.18


# --------------------------------------------------------------------------- #
# The local match date, which is not the UTC date
# --------------------------------------------------------------------------- #
def test_ticker_date_is_parsed_without_a_locale():
    """`%b` is locale-dependent; a non-English machine would stop resolving everything."""
    assert kalshi.ticker_date("KXMLSGAME-26AUG15ATLNYRB", "KXMLSGAME") == date(2026, 8, 15)
    assert kalshi.ticker_date("KXMLSGAME-26XXX15ATLNYRB", "KXMLSGAME") is None
    assert kalshi.ticker_date("KXMLSGAME-26AUG15ATLNYRB", "KXLIGAMXGAME") is None


def test_a_kickoff_after_midnight_utc_belongs_to_the_previous_local_day():
    """The real case: fixture 1550922, Monterrey v Juárez.

    `2026-08-16 01:00Z` is Aug 15 in America/Mexico_City, and Kalshi's ticker says
    `26AUG15`. Matching on the UTC date misses it — and misses it *silently*, because
    the Fixture simply appears to have no market.
    """
    kickoff = datetime(2026, 8, 16, 1, 0, tzinfo=timezone.utc)
    assert kalshi.local_match_date(kickoff, "America/Mexico_City") == date(2026, 8, 15)
    assert kalshi.local_match_date(kickoff, "UTC") == date(2026, 8, 16)


def test_attach_requires_the_date_to_agree(tmp_path):
    """Two clubs meeting twice a season are told apart by the date, and nothing else."""
    reg = _registry(tmp_path)
    index, _ = kalshi.index_by_team_pair(_three_legs(), "KXTESTGAME", reg)
    right = datetime(2026, 8, 15, 18, 0, tzinfo=timezone.utc)
    wrong = datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)
    assert kalshi.attach(11, 22, right, "UTC", index) is not None
    assert kalshi.attach(11, 22, wrong, "UTC", index) is None


# --------------------------------------------------------------------------- #
# Team Leaders: never without a scope, and never grouped like the overall list
# --------------------------------------------------------------------------- #
def test_a_leader_always_carries_its_scope_and_games_played():
    scope = {"competition_id": 262, "competition": "Liga MX", "season": 2026,
             "tournament": "Apertura", "label": "Liga MX Apertura 2026",
             "is_fixture_tournament": False}
    computed = {"goals": {11: {"player_id": 1, "name": "A", "value": 1, "tied_with": 3}},
                "assists": {}, "played": {11: 3}}
    block = _team_block(11, {"name": "Home FC"}, [], [(scope, computed)])

    leader = block["leaders"][0]
    assert leader["scope"]["label"] == "Liga MX Apertura 2026"
    assert leader["scope"]["played"] == 3, "the sample size is part of the fact"
    assert leader["top_scorer"]["tied_with"] == 3, "one of four on 1 goal, not a sole leader"


def test_a_scope_with_no_leader_is_dropped_not_emptied():
    """Twelve games into a Leagues Cup group phase most clubs have no scorer at all.

    The card must fall back to the club's domestic campaign rather than render an empty
    block — which is the entire reason Team Leaders carry two scopes.
    """
    empty = {"goals": {}, "assists": {}, "played": {11: 0}}
    populated = {"goals": {11: {"player_id": 1, "name": "A", "value": 9, "tied_with": 0}},
                 "assists": {}, "played": {11: 18}}
    block = _team_block(11, {"name": "Home FC"}, [],
                        [({"label": "Leagues Cup Group Stage 2026"}, empty),
                         ({"label": "MLS Regular Season 2026"}, populated)])
    assert [l["scope"]["label"] for l in block["leaders"]] == ["MLS Regular Season 2026"]


def test_per_team_leaders_do_not_credit_a_transfer_to_the_wrong_club():
    """A player who moves inside one league scores for whichever club he scored for.

    `leaders_overall` sums a player's season across clubs on purpose — that is what a
    competition's top scorer means. Doing the same per Team would hand the new club the
    goals scored *against* it.
    """
    entries = [
        (11, 100, "Mover", 7, 0),      # 7 for the old club
        (22, 100, "Mover", 1, 0),      # 1 since the move
        (22, 200, "Stayer", 4, 0),
    ]
    by_team = standings_mod.leaders_by_team(entries, "goals")
    assert by_team[11]["value"] == 7
    assert by_team[22]["name"] == "Stayer" and by_team[22]["value"] == 4


def test_a_team_with_no_goals_has_no_leader_rather_than_a_zero():
    by_team = standings_mod.leaders_by_team([(11, 100, "Nobody", 0, 0)], "goals")
    assert 11 not in by_team


# --------------------------------------------------------------------------- #
# The table: absent for a knockout, and honest about being unofficial
# --------------------------------------------------------------------------- #
def test_a_knockout_fixture_reports_an_absent_table_with_a_reason():
    block = _team_block(11, {"name": "Home FC"}, [], [])
    assert block["table"] == {"state": "absent", "reason": "knockout"}


def test_a_table_row_carries_the_unofficial_ordering_footnote():
    """ADR 0025's caveat travels with the row, not with a template that may not use it."""
    rows = standings_mod.standings_rows([
        (11, "Home FC", 22, "Away FC", 2, 1, "FT"),
    ])
    block = _team_block(11, {"name": "Home FC"}, rows, [])
    assert block["table"]["state"] == "present"
    assert block["table"]["position"] == 1
    assert block["table"]["points"] == 3
    assert block["table"]["unofficial_ordering"] is True


def test_a_penalty_shootout_is_a_draw_in_the_table():
    """`PEN` is Final and its on-pitch score is level — the shootout is not a scoreline
    (ADR 0012). Counting it as a win silently inflates one club by two points."""
    rows = standings_mod.standings_rows([(11, "A", 22, "B", 1, 1, "PEN")])
    assert {r["name"]: r["Pts"] for r in rows} == {"A": 1, "B": 1}


# --------------------------------------------------------------------------- #
# Selection: a whitelist, because cancelled is spelled two ways
# --------------------------------------------------------------------------- #
def test_scheduled_selection_is_a_whitelist_disjoint_from_terminal_states():
    assert set(SCHEDULED).isdisjoint(FINAL)
    assert set(SCHEDULED).isdisjoint(ENDED_WITHOUT_RESULT)


@pytest.mark.parametrize("cancelled", ["CANC", "Canc"])
def test_neither_spelling_of_cancelled_is_previewable(cancelled):
    """The store holds both. A blacklist (`status != 'CANC'`) previews five of them."""
    assert cancelled not in SCHEDULED


# --------------------------------------------------------------------------- #
# The market block always exists, and says why when it is empty
# --------------------------------------------------------------------------- #
def test_an_absent_market_still_writes_a_block_with_its_reason():
    """The /previews board reads this. `market: null` would make "no market" and "not
    built yet" indistinguishable on the card."""
    block = _market_block(None, "not_listed")
    assert block["state"] == "absent" and block["reason"] == "not_listed"


def test_an_attached_market_records_that_it_settles_on_regulation():
    """Kalshi resolves on 90'+stoppage; our scoreline is after extra time (ADR 0012).
    For a knockout tie past 90 the two disagree, so the basis is stated on the record."""
    market = kalshi.WinnerMarket(
        event_ticker="E", series_ticker="S", local_date=date(2026, 8, 15),
        outcomes=[], overround=1.01, state="quoted")
    assert _market_block(market, "")["settles_on"] == "regulation"


# --------------------------------------------------------------------------- #
# The freeze: a settled Match Preview is never rewritten
# --------------------------------------------------------------------------- #
class _FakePB:
    """Just enough PocketBase to exercise `freeze_kicked_off` without a server."""

    def __init__(self, records):
        self.records = records
        self.settled: list[str] = []
        self.patched: list[tuple[str, dict]] = []

    def list_previews(self, *, lifecycle=None, extra_filter=""):
        return [r for r in self.records
                if lifecycle is None or r["lifecycle"] == lifecycle]

    def settle_preview(self, record_id):
        self.settled.append(record_id)
        for r in self.records:
            if r["id"] == record_id:
                r["lifecycle"] = "settled"

    def upsert_preview(self, data, existing=None):
        self.patched.append((data.get("postgres_fixture_id"), data))
        return data


def _record(rid, fixture_id, kickoff, lifecycle="upcoming"):
    return {"id": rid, "postgres_fixture_id": fixture_id,
            "kickoff_utc": kickoff.isoformat().replace("+00:00", "Z"),
            "lifecycle": lifecycle,
            "market": {"state": "quoted", "outcomes": [{"market_probability": 0.61}]},
            "quote_read_at": "2026-08-06T21:00:00Z"}


def test_only_kicked_off_previews_freeze():
    from football_blog.preview import freeze_kicked_off
    now = datetime(2026, 8, 6, 22, 0, tzinfo=timezone.utc)
    pb = _FakePB([
        _record("a", 1, now - timedelta(minutes=1)),   # kicked off
        _record("b", 2, now + timedelta(minutes=1)),   # still to come
    ])
    assert freeze_kicked_off(pb, now) == 1
    assert pb.settled == ["a"]


def test_the_freeze_writes_nothing_but_the_lifecycle():
    """The whole point of the freeze.

    A settled card keeps the last Quote read before kickoff, and nothing reconstructs
    it — Kalshi's candlesticks come back empty for these series. A freeze that also
    refreshed the market would replace the interesting artifact with a post-hoc one, and
    the card would look entirely normal afterwards.
    """
    from football_blog.preview import freeze_kicked_off
    now = datetime(2026, 8, 6, 22, 0, tzinfo=timezone.utc)
    pb = _FakePB([_record("a", 1, now - timedelta(hours=2))])
    before = json.dumps(pb.records[0]["market"], sort_keys=True)

    freeze_kicked_off(pb, now)

    assert pb.patched == [], "the freeze must not upsert a payload"
    assert json.dumps(pb.records[0]["market"], sort_keys=True) == before
    assert pb.records[0]["quote_read_at"] == "2026-08-06T21:00:00Z"


def test_an_already_settled_preview_is_not_refrozen():
    from football_blog.preview import freeze_kicked_off
    now = datetime(2026, 8, 6, 22, 0, tzinfo=timezone.utc)
    pb = _FakePB([_record("a", 1, now - timedelta(hours=2), lifecycle="settled")])
    assert freeze_kicked_off(pb, now) == 0
    assert pb.settled == []


def test_the_leader_is_the_maximum_regardless_of_input_order():
    """Pinned after a mutation test slipped through.

    The first version of the transfer case listed the top scorer last, so an
    implementation that simply kept the *last* row it saw produced the right answer and
    the guard stayed green. Postgres returns a `GROUP BY` in no guaranteed order, so
    "last wins" would be a real bug that surfaced only when the plan changed.
    """
    ascending = [(22, 200, "Stayer", 4, 0), (22, 100, "Mover", 1, 0)]
    descending = list(reversed(ascending))
    for entries in (ascending, descending):
        best = standings_mod.leaders_by_team(entries, "goals")
        assert best[22]["name"] == "Stayer" and best[22]["value"] == 4


def test_a_tie_is_broken_by_player_id_so_the_order_is_stable():
    """Two players level: the same one must win every run, whatever order rows arrive in.

    `leaders_overall` breaks ties on the lower player id; per-Team must agree, or the
    same fixture renders a different 'leading scorer' between two runs of the same data.
    """
    rows = [(11, 500, "Later", 3, 0), (11, 100, "Earlier", 3, 0)]
    for entries in (rows, list(reversed(rows))):
        best = standings_mod.leaders_by_team(entries, "goals")
        assert best[11]["player_id"] == 100
        assert best[11]["tied_with"] == 1
