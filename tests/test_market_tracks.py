"""Polymarket and Market Track invariants that break without raising (ADR 0033/0043).

Everything here shares the property the suite exists for: the wrong behaviour renders a
card or a graph. Nothing throws, no page 500s, and the damage is a picture that is
quietly untrue.

Five were real forks in the design and each has a test below that fails if it is undone:

  **The kickoff instant does not resolve anything on its own.** Six pairs of Leagues Cup
  Fixtures share a kickoff instant in a single week. Keyed on time alone, one of each
  pair attaches to the other's market — a preview that is confidently, invisibly wrong.

  **A Polymarket team id is per league, not per club.** CF Monterrey is 115320 in `mex`
  and 3268366 in `lec`. A registry keyed on the bare id merges two clubs' entries the
  first time the id spaces touch.

  **The slug is dated and lies.** `mls-ner-hou-2026-03-07` is an August kickoff, because
  a Polymarket event outlives a postponement and keeps its birth date. Reading the slug
  looks like it works — most events were never postponed.

  **An hour missing a leg is a gap.** Normalising over two of three legs, or carrying the
  previous hour across, draws a probability nobody quoted at a timestamp the Exchange was
  silent.

  **Kalshi's candlesticks are read from the book, not from `price`.** The `price` block is
  `{}` on any period with no trade — most of them — while the book is quoted throughout.
  Reading `price` yields an empty Track and looks exactly like "not listed yet", which is
  the mistake ADR 0040 recorded as fact and ADR 0043 corrects.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from football_blog import polymarket as pm
from football_blog import track

UTC = timezone.utc
KICKOFF = datetime(2026, 8, 15, 23, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _leg(event_slug: str, suffix: str, yes: str | None, *, volume: str = "100.0",
         bid: float | None = 0.30, ask: float | None = 0.32) -> dict:
    prices = json.dumps([yes, "0.5"]) if yes is not None else None
    return {
        "slug": f"{event_slug}-{suffix}",
        "sportsMarketType": "moneyline",
        "groupItemTitle": suffix.upper(),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": prices,
        "clobTokenIds": json.dumps([f"tok-{suffix}", f"tok-{suffix}-no"]),
        "bestBid": bid, "bestAsk": ask, "lastTradePrice": 0.31, "volume": volume,
    }


def _event(slug: str, *, start: datetime = KICKOFF, home=("hom", 900, "Home FC"),
           away=("awy", 901, "Away FC"), yes=("0.50", "0.25", "0.25")) -> dict:
    ha, hid, hname = home
    aa, aid, aname = away
    return {
        "slug": slug,
        "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "teams": [{"id": hid, "name": hname, "abbreviation": ha, "ordering": "home"},
                  {"id": aid, "name": aname, "abbreviation": aa, "ordering": "away"}],
        "markets": [_leg(slug, ha, yes[0]), _leg(slug, "draw", yes[1]),
                    _leg(slug, aa, yes[2])],
    }


def _registry(**teams) -> pm.Registry:
    return pm.Registry(
        leagues={"lec": {"competition_id": 772, "series_slug": "soccer-lec"},
                 "mex": {"competition_id": 262, "series_slug": "mex-2025"}},
        teams=teams.get("teams", {("lec", 900): 11, ("lec", 901): 22}))


# --------------------------------------------------------------------------- #
# The instant narrows; the team pair decides
# --------------------------------------------------------------------------- #
def test_two_fixtures_at_the_same_instant_do_not_collide():
    """Six pairs share a kickoff instant in one Leagues Cup week (ADR 0043)."""
    reg = pm.Registry(leagues=_registry().leagues,
                      teams={("lec", 900): 11, ("lec", 901): 22,
                             ("lec", 902): 33, ("lec", 903): 44})
    a = _event("lec-hom-awy-2026-08-15")
    b = _event("lec-cha-atl-2026-08-15",
               home=("cha", 902, "Charlotte"), away=("atl", 903, "Atlas"))
    index, _ = pm.index_by_team_pair([a, b], "lec", reg)

    assert len(index) == 2, "two simultaneous Fixtures must not overwrite one another"
    assert pm.attach(11, 22, KICKOFF, index).event_slug == "lec-hom-awy-2026-08-15"
    assert pm.attach(33, 44, KICKOFF, index).event_slug == "lec-cha-atl-2026-08-15"


def test_a_fixture_at_a_different_instant_does_not_attach():
    index, _ = pm.index_by_team_pair([_event("lec-hom-awy-2026-08-15")], "lec", _registry())
    assert pm.attach(11, 22, KICKOFF, index) is not None
    assert pm.attach(11, 22, KICKOFF + timedelta(hours=1), index) is None


def test_the_slug_date_is_never_the_anchor():
    """`mls-ner-hou-2026-03-07` is an August kickoff — the event outlived a postponement."""
    event = _event("mls-ner-hou-2026-03-07", start=datetime(2026, 8, 8, 20, 30, tzinfo=UTC))
    index, _ = pm.index_by_team_pair([event], "lec", _registry())
    assert pm.attach(11, 22, datetime(2026, 8, 8, 20, 30, tzinfo=UTC), index) is not None
    assert pm.attach(11, 22, datetime(2026, 3, 7, 20, 30, tzinfo=UTC), index) is None


def test_a_naive_kickoff_is_read_as_utc_not_as_local_time():
    index, _ = pm.index_by_team_pair([_event("lec-hom-awy-2026-08-15")], "lec", _registry())
    assert pm.attach(11, 22, KICKOFF.replace(tzinfo=None), index) is not None


# --------------------------------------------------------------------------- #
# Identity: per league, and never by name
# --------------------------------------------------------------------------- #
def test_a_team_id_is_scoped_to_its_league():
    """CF Monterrey is 115320 in mex and 3268366 in lec — the id alone is not identity."""
    reg = pm.Registry(leagues=_registry().leagues, teams={("mex", 900): 99})
    assert reg.team_id("mex", 900) == 99
    assert reg.team_id("lec", 900) is None, "an id must not resolve in another league"


def test_one_unmapped_club_refuses_the_whole_market():
    """Never half-attached: the second club is never inferred from the first."""
    reg = pm.Registry(leagues=_registry().leagues, teams={("lec", 900): 11})
    index, unmapped = pm.index_by_team_pair([_event("lec-hom-awy-2026-08-15")], "lec", reg)
    assert index == {}
    assert [u["polymarket_team"] for u in unmapped] == [901]


def test_the_draw_is_recognised_structurally_not_by_the_word_draw():
    """Polymarket gives the draw no team, so it is the `-draw` leg of the event slug."""
    event = _event("lec-hom-awy-2026-08-15")
    for m in event["markets"]:
        m["groupItemTitle"] = "Empate"          # a Spanish Publication, or a rename
    index, _ = pm.index_by_team_pair([event], "lec", _registry())
    market = pm.label_sides(pm.attach(11, 22, KICKOFF, index), 11, 22)
    assert [o["side"] for o in market.outcomes] == ["home", "draw", "away"]
    assert next(o for o in market.outcomes if o["side"] == "draw")["team_id"] is None


def test_a_two_way_market_is_not_a_winner_market():
    """Polymarket's 2025 Leagues Cup events had no draw leg — not this product."""
    event = _event("lec-hom-awy-2026-08-15")
    event["markets"] = [m for m in event["markets"] if not m["slug"].endswith("-draw")]
    index, _ = pm.index_by_team_pair([event], "lec", _registry())
    assert index == {}


def test_an_event_with_no_start_time_is_skipped():
    event = _event("lec-hom-awy-2026-08-15")
    event["startTime"] = None
    index, _ = pm.index_by_team_pair([event], "lec", _registry())
    assert index == {}


# --------------------------------------------------------------------------- #
# Quotes and probabilities
# --------------------------------------------------------------------------- #
def test_probabilities_normalise_within_one_exchange():
    quotes = [{"mid": 0.50}, {"mid": 0.28}, {"mid": 0.25}]
    probs, overround = pm.market_probabilities(quotes)
    assert sum(probs) == pytest.approx(1.0)
    assert overround == pytest.approx(1.03)


def test_an_overround_below_one_is_ordinary_here():
    """Kalshi's legs sum to 1.005–1.025; Polymarket's to 0.995–1.065 (ADR 0043)."""
    probs, overround = pm.market_probabilities(
        [{"mid": 0.395}, {"mid": 0.275}, {"mid": 0.325}])
    assert overround == pytest.approx(0.995)
    assert sum(probs) == pytest.approx(1.0)


def test_a_missing_leg_yields_no_probabilities_at_all():
    probs, overround = pm.market_probabilities([{"mid": 0.5}, {"mid": None}, {"mid": 0.25}])
    assert probs is None and overround is None


def test_volume_carries_its_unit_because_it_is_dollars_not_contracts():
    """A bare `volume` on two panels invites a comparison that has no conversion."""
    quote = pm.quote_of(_leg("lec-hom-awy-2026-08-15", "hom", "0.50", volume="84067.6"))
    assert quote["volume"] == 84067.6
    assert quote["volume_unit"] == "usd"


def test_the_published_mid_is_preferred_over_one_we_recompute():
    """The CLOB history series *is* `outcomePrices`, so a Track ends where the card is."""
    quote = pm.quote_of(_leg("e", "hom", "0.565", bid=0.56, ask=0.58))
    assert quote["mid"] == 0.565, "not (0.56 + 0.58) / 2 = 0.57"


def test_the_yes_leg_is_found_by_name_not_by_position():
    market = _leg("e", "hom", "0.50")
    market["outcomes"] = json.dumps(["No", "Yes"])
    market["outcomePrices"] = json.dumps(["0.60", "0.40"])
    assert pm.quote_of(market)["mid"] == 0.40


# --------------------------------------------------------------------------- #
# Market Tracks
# --------------------------------------------------------------------------- #
H = track.HOUR


def test_an_hour_missing_a_leg_is_a_gap_not_an_interpolation():
    points, gaps = track.normalise({
        "home": {0: 0.40, H: 0.40, 2 * H: 0.42},
        "draw": {0: 0.28, H: 0.28, 2 * H: 0.27},
        "away": {0: 0.32, 2 * H: 0.31},          # hour H is silent
    })
    assert [p.t for p in points] == [0, 2 * H], "the silent hour must not be drawn"
    assert gaps == 1


def test_every_point_sums_to_one():
    points, _ = track.normalise({"home": {0: 0.395}, "draw": {0: 0.275}, "away": {0: 0.325}})
    p = points[0]
    assert p.home + p.draw + p.away == pytest.approx(1.0)
    assert p.home == pytest.approx(0.395 / 0.995)


def test_hours_before_an_exchange_listed_anything_are_absence_not_gaps():
    """A Kalshi Track starting four hours before kickoff has not "missed" a month."""
    points, gaps = track.normalise({
        "home": {5 * H: 0.40, 6 * H: 0.40},
        "draw": {5 * H: 0.28, 6 * H: 0.28},
        "away": {5 * H: 0.32, 6 * H: 0.32},
    })
    assert len(points) == 2 and gaps == 0


def test_timestamps_bucket_to_the_hour():
    """Polymarket's three legs share an exact timestamp 0% of the time (ADR 0043)."""
    assert track._bucket(1785448806) == track._bucket(1785448804) == 1785448800


def test_a_kalshi_mid_comes_from_the_book_not_from_price(monkeypatch):
    """`price` is `{}` on any period with no trade — most of them on these series."""
    class _R:
        @staticmethod
        def raise_for_status(): pass
        @staticmethod
        def json():
            return {"candlesticks": [{
                "end_period_ts": 3 * H,
                "price": {},                       # no trade this hour
                "yes_bid": {"close_dollars": "0.3900"},
                "yes_ask": {"close_dollars": "0.4100"},
            }]}

    class _C:
        def get(self, *a, **k): return _R()

    mids = track._kalshi_mids(_C(), "KXMLSGAME", "KXMLSGAME-X", 0, 9 * H)
    assert mids == {3 * H: pytest.approx(0.40)}, "the book is quoted even with no trades"


def test_a_one_sided_kalshi_book_yields_nothing_for_that_hour():
    class _R:
        @staticmethod
        def raise_for_status(): pass
        @staticmethod
        def json():
            return {"candlesticks": [{"end_period_ts": H, "price": {},
                                      "yes_bid": {"close_dollars": "0.3900"},
                                      "yes_ask": {}}]}

    class _C:
        def get(self, *a, **k): return _R()

    assert track._kalshi_mids(_C(), "S", "T", 0, 9 * H) == {}


# --------------------------------------------------------------------------- #
# The pair, and the axis they share
# --------------------------------------------------------------------------- #
def _track(exchange: str, hours: list[int]) -> track.MarketTrack:
    pts = [track.TrackPoint(t=h, home=0.4, draw=0.28, away=0.32) for h in hours]
    return track.MarketTrack(exchange=exchange, points=pts,
                             listed_from=pts[0].t if pts else None)


def test_both_panels_share_one_axis():
    """190x different spans rendered at the same width is the lie-by-omission again."""
    pair = track.TrackPair(fixture_id=1, kickoff=800 * H,
                           kalshi=_track("kalshi", [700 * H, 701 * H]),
                           polymarket=_track("polymarket", [0, H, 700 * H]))
    assert pair.t_from == 0, "the axis starts at the earlier Exchange's first point"
    assert pair.t_to == 800 * H, "and ends at kickoff, not at the last Quote"
    assert pair.both


def test_the_axis_survives_one_exchange_having_nothing():
    pair = track.TrackPair(fixture_id=1, kickoff=10 * H,
                           kalshi=track.MarketTrack(exchange="kalshi", state="not_listed"),
                           polymarket=_track("polymarket", [0, H]))
    assert pair.t_from == 0 and pair.t_to == 10 * H
    assert not pair.both


def test_not_covered_is_a_different_state_from_not_listed():
    """One is permanent and needs no human; the other resolves itself (ADR 0043)."""
    covered = pm.Registry(leagues={"lec": {"competition_id": 772, "series_slug": "soccer-lec"}},
                          teams={})
    assert covered.covers(772)
    assert not covered.covers(253)


def test_a_registry_key_round_trips():
    assert pm.registry_key("lec", 3268357) == "lec:3268357"


# --------------------------------------------------------------------------- #
# The record carries one Winner Market per Exchange
# --------------------------------------------------------------------------- #
def test_an_absent_polymarket_market_still_writes_a_block_with_its_reason():
    """`null` would make "not covered", "not listed" and "not built" the same on a card."""
    from football_blog.preview import _polymarket_block
    block = _polymarket_block(None, "not_covered")
    assert block["state"] == "absent" and block["reason"] == "not_covered"
    assert block["source"] == "polymarket"


def test_both_blocks_record_that_they_settle_on_regulation():
    """The one property the two Exchanges share exactly — and what makes them comparable."""
    from football_blog.preview import _polymarket_block
    market = pm.WinnerMarket(event_slug="e", league="lec", kickoff_utc=KICKOFF,
                             outcomes=[], overround=1.01, state="quoted")
    assert _polymarket_block(market, "")["settles_on"] == "regulation"


def test_the_card_reads_both_exchanges_and_keeps_their_volume_units_apart():
    """Kalshi counts contracts, Polymarket counts dollars — one scale would be a lie."""
    from surfaces.previews.board import _markets_of
    markets = _markets_of({
        "market_kalshi": {"state": "quoted", "outcomes": [
            {"side": "home", "market_probability": 0.40,
             "quote": {"volume": 139.0}},
            {"side": "draw", "market_probability": 0.27},
            {"side": "away", "market_probability": 0.33}]},
        "quote_read_at_kalshi": "2026-08-06T23:00:00Z",
        "market_polymarket": {"state": "quoted", "outcomes": [
            {"side": "home", "market_probability": 0.397,
             "quote": {"volume": 41814.17, "volume_unit": "usd"}},
            {"side": "draw", "market_probability": 0.276},
            {"side": "away", "market_probability": 0.327}]},
        "quote_read_at_polymarket": "2026-08-06T23:00:00Z",
    })
    assert set(markets) == {"kalshi", "polymarket"}
    assert markets["kalshi"]["volume_unit"] == "contracts"
    assert markets["polymarket"]["volume_unit"] == "usd"
    assert markets["kalshi"]["home"] == 0.40
    assert markets["polymarket"]["home"] == 0.397


def test_a_card_is_complete_on_one_exchange_because_coverage_differs():
    """Requiring both would mark a card incomplete for a gap nobody can close."""
    from surfaces.previews.board import PreviewCard
    card = PreviewCard(fixture_id=1, kickoff=KICKOFF, local_date=None,
                       league_id=772, league_name="Leagues Cup",
                       home_name="A", away_name="B")
    card.markets = {"kalshi": {"state": "quoted"},
                    "polymarket": {"state": "absent", "reason": "not_covered"}}
    assert card.has_market


def test_both_exchanges_always_render_in_a_fixed_order():
    """Position follows the Exchange, never whether it happens to be quoted."""
    from surfaces.previews.board import PreviewCard
    card = PreviewCard(fixture_id=1, kickoff=KICKOFF, local_date=None,
                       league_id=772, league_name="Leagues Cup",
                       home_name="A", away_name="B")
    card.markets = {"polymarket": {"state": "quoted"}}       # only the second resolved
    assert [m["exchange"] for m in card.exchanges] == ["kalshi", "polymarket"]
    assert card.exchanges[0]["state"] == "absent"


def test_a_settled_preview_finds_its_market_without_resolving_it():
    """Resolution sweeps only *open* markets, so a few hours after kickoff neither
    Exchange can see this Fixture — and that is exactly when the graph becomes the only
    place its history survives. The frozen record carries the identifiers instead."""
    stored = track.stored_market({
        "state": "quoted", "series_ticker": "KXLEAGUESCUPGAME",
        "event_ticker": "KXLEAGUESCUPGAME-26AUG06NYCSLA",
        "outcomes": [
            {"side": "home", "market_ticker": "KXLEAGUESCUPGAME-26AUG06NYCSLA-NYC"},
            {"side": "draw", "market_ticker": "KXLEAGUESCUPGAME-26AUG06NYCSLA-TIE"},
            {"side": "away", "market_ticker": "KXLEAGUESCUPGAME-26AUG06NYCSLA-SLA"}]})
    assert stored.series_ticker == "KXLEAGUESCUPGAME"
    assert [o["market_ticker"] for o in stored.outcomes][0].endswith("-NYC")

    pm_stored = track.stored_market({
        "state": "quoted", "event_slug": "lec-nyc-san-2026-08-06", "league": "lec",
        "outcomes": [{"side": "home", "token_id": "tok-a"},
                     {"side": "draw", "token_id": "tok-b"},
                     {"side": "away", "token_id": "tok-c"}]})
    assert pm_stored.event_slug == "lec-nyc-san-2026-08-06"
    assert [o["token_id"] for o in pm_stored.outcomes] == ["tok-a", "tok-b", "tok-c"]


def test_an_absent_block_falls_back_to_resolution_rather_than_faking_a_market():
    assert track.stored_market(None) is None
    assert track.stored_market({"state": "absent", "reason": "not_listed"}) is None
    assert track.stored_market({"state": "quoted", "outcomes": []}) is None


def test_the_record_fields_are_named_not_derived():
    """PocketBase silently ignores an unknown field on write, so a typo here writes
    nothing at all and every card reads `None` without a single error."""
    from surfaces.previews.board import _MARKET_FIELDS
    assert _MARKET_FIELDS["kalshi"] == ("market_kalshi", "quote_read_at_kalshi")
    assert _MARKET_FIELDS["polymarket"] == ("market_polymarket",
                                            "quote_read_at_polymarket")


def test_the_registry_file_on_disk_loads_and_names_every_covered_competition():
    """Guards the committed file, not a fake: a malformed key silently maps nothing."""
    registry = pm.load_registry()
    assert registry.teams, "the committed registry must not be empty"
    assert set(registry.competition_ids) == {253, 262, 772}
    for league in registry.league_keys:
        assert registry.series_slug(league), f"{league} needs a verified series slug"
    # A club in two leagues holds two rows, and they must not collide.
    assert registry.team_id("mex", 115320) == registry.team_id("lec", 3268366)
