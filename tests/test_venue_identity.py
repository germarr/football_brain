"""Venue identity folds, merges and displays as ADR 0042 says — every guard here is
written against a failure that would otherwise be *silent*.

Four of them have already happened, or came within one commit of happening:

- `NFKD -> encode("ascii", "ignore")` deletes `æ` outright, folding `Næsby` to `nsby`.
  A fold that mangles a letter matches nothing, so the duplicate it exists to find stays
  hidden and the census under-reports. Nothing raises.
- `mint()` used to rebuild each record from a `dict[VenueKey, int]`, so any field written
  into `venues.json` beyond `{id, name, city}` was discarded by the next nightly build —
  which is why merges live in their own file. Nothing raises.
- Electing the lowest id unconditionally elects the *cityless* entry when it happens to be
  lower (`Stade Pierre Pibarot`: null at 5912, `Alès` at 5913), publishing a ground with no
  city. Nothing raises.
- Several spellings resolving to one id makes `_build_venues` insert that primary key twice.
  This one *does* raise — at the end of a full rebuild, after the sweep.
"""
from __future__ import annotations

import json

import pytest

from football.build import venues


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A registry and merge list on disk, isolated from the committed ones."""
    def _make(entries, merges=(), derive=True):
        reg, mrg = tmp_path / "venues.json", tmp_path / "merges.json"
        reg.write_text(json.dumps(entries))
        mrg.write_text(json.dumps(list(merges)))
        monkeypatch.setattr(venues, "REGISTRY_FILE", reg)
        monkeypatch.setattr(venues, "MERGES_FILE", mrg)
        monkeypatch.setattr(venues, "_LOCK_FILE", tmp_path / "venues.json.lock")
        monkeypatch.setattr(venues, "DERIVE_SPELLING_MERGES", derive)
        return reg, mrg
    return _make


# --- the fold --------------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("ALPI Arena Næsby", "ALPI Arena Naesby"),      # NFKD does NOT decompose æ
    ("Tilst Idrætsanlæg", "Tilst Idraetsanlaeg"),
    ("Brøndby", "Brondby"),
    ("Arena do Grêmio", "Arena do Gremio"),         # ordinary diacritics
    ("Beşiktaş Park", "Besiktas Park"),
    ("Osmanlı Stadı", "Osmanli Stadi"),             # Turkish dotless i
    ("St. Andrew's Stadium", "St Andrews Stadium"), # elided apostrophe and period
    ("Marc'Antonio Bentegodi", "Marcantonio Bentegodi"),
    ("An der Alten Försterei", "an der alten forsterei"),
])
def test_fold_matches_the_same_ground(a, b):
    assert venues.fold(a) == venues.fold(b) != ""


def test_fold_does_not_delete_letters():
    """The specific bug: a fold that drops æ instead of mapping it silently mismatches."""
    assert venues.fold("Næsby") == "naesby"
    assert "nsby" != venues.fold("Næsby")


@pytest.mark.parametrize("a, b", [
    ("Wembley Stadium", "Wembley Arena"),
    ("Estadio Azteca", "Estadio Akron"),
])
def test_fold_keeps_different_grounds_apart(a, b):
    assert venues.fold(a) != venues.fold(b)


def test_absent_city_does_not_fold_onto_a_present_one():
    """The fold is a fact about strings; 'no city means that city' is an inference, and it
    is decided in the merge list instead — never derived here."""
    assert venues.fold(None) == venues.fold("") == ""
    assert venues.fold(None) != venues.fold("Tallinn")


# --- resolution ------------------------------------------------------------

def test_spelling_duplicates_resolve_to_the_lowest_id(registry):
    registry([{"id": 211, "name": "Arena De Pernambuco", "city": "Sao Lourenco da Mata"},
              {"id": 7849, "name": "Arena de Pernambuco", "city": "São Lourenço da Mata"}])
    resolved = venues.load()
    assert set(resolved.values()) == {211}
    assert len(resolved) == 2, "every stored spelling must still resolve"


def test_derived_merges_are_off_until_the_merge_window(registry):
    """The switch is the staging (ADR 0042): the mechanism ships before the merge does."""
    registry([{"id": 211, "name": "Arena De Pernambuco", "city": "Sao Lourenco da Mata"},
              {"id": 7849, "name": "Arena de Pernambuco", "city": "São Lourenço da Mata"}],
             derive=False)
    assert set(venues.load().values()) == {211, 7849}


def test_a_decided_merge_overrules_lowest_id(registry):
    """Pibarot: the cityless entry holds the *lower* id, so lowest-id-wins alone would elect
    a ground with no city. An id named as a `from` can never be canonical."""
    registry([{"id": 5912, "name": "Stade Pierre Pibarot", "city": None},
              {"id": 5913, "name": "Stade Pierre Pibarot", "city": "Alès"}],
             merges=[{"from": 5912, "into": 5913, "class": "missing-city"}])
    assert venues.load()[("Stade Pierre Pibarot", None)] == 5913
    assert venues.displays()[5913] == ("Stade Pierre Pibarot", "Alès")


def test_merge_chains_resolve(registry):
    registry([{"id": 1, "name": "A", "city": None},
              {"id": 2, "name": "A", "city": "X"},
              {"id": 3, "name": "A", "city": "Y"}],
             merges=[{"from": 1, "into": 2}, {"from": 2, "into": 3}])
    assert set(venues.load().values()) == {3}


def test_an_unknown_id_in_the_merge_list_is_ignored(registry):
    """A merge naming an id no longer in the registry must not crash a nightly build."""
    registry([{"id": 1, "name": "A", "city": "X"}], merges=[{"from": 999, "into": 1}])
    assert venues.load() == {("A", "X"): 1}


# --- display ---------------------------------------------------------------

def test_display_prefers_the_richest_spelling_not_the_surviving_id(registry):
    """In 89 of 129 spelling clusters the lowest id holds the mangled name, so taking the
    strings from the surviving entry would fix the merge and display `Arena do Gremio`."""
    registry([{"id": 250, "name": "Arena do Gremio", "city": "Porto Alegre"},
              {"id": 251, "name": "Arena do Grêmio", "city": "Porto Alegre"}])
    assert venues.load()[("Arena do Gremio", "Porto Alegre")] == 250
    assert venues.displays()[250] == ("Arena do Grêmio", "Porto Alegre")


def test_display_does_not_shout(registry):
    """The provider writes some names in caps. Richness alone cannot see that — both
    spellings hold zero non-ASCII — so the lowest id would elect `SIGNAL IDUNA PARK`."""
    registry([{"id": 4663, "name": "SIGNAL IDUNA PARK", "city": "Dortmund"},
              {"id": 4824, "name": "Signal Iduna Park", "city": "Dortmund"},
              {"id": 4826, "name": "Signal-Iduna-Park", "city": "Dortmund"}])
    assert venues.load()[("SIGNAL IDUNA PARK", "Dortmund")] == 4663, "identity is the lowest id"
    assert venues.displays()[4663] == ("Signal Iduna Park", "Dortmund"), "display is not"


def test_display_override_beats_the_heuristic(registry):
    """`Doosan Arena` / `Doosan Aréna` in `Plzeň`: richest-spelling picks the city correctly
    and the name wrongly, since only the city takes the háček."""
    registry([{"id": 1191, "name": "Doosan Arena", "city": "Plzen"},
              {"id": 1193, "name": "Doosan Aréna", "city": "Plzeň"}],
             merges=[{"from": 1193, "into": 1191,
                      "display": {"name": "Doosan Arena", "city": "Plzeň"}}])
    assert venues.displays()[1191] == ("Doosan Arena", "Plzeň")


def test_display_prefers_an_entry_that_has_a_city(registry):
    registry([{"id": 5912, "name": "Stade Pierre Pibarot", "city": None},
              {"id": 5913, "name": "Stade Pierre Pibarot", "city": "Alès"}],
             merges=[{"from": 5912, "into": 5913}])
    assert venues.displays()[5913][1] == "Alès"


# --- minting ---------------------------------------------------------------

def test_mint_appends_a_new_spelling_but_not_a_new_identity(registry):
    reg, _ = registry([{"id": 1, "name": "Arena do Gremio", "city": "Porto Alegre"}])
    resolved = venues.mint([("Arena do Grêmio", "Porto Alegre")])
    stored = json.loads(reg.read_text())
    assert [e["id"] for e in stored] == [1, 2], "the spelling is recorded"
    assert resolved[("Arena do Grêmio", "Porto Alegre")] == 1, "but shares the identity"


def test_mint_never_writes_the_resolved_map_back(registry):
    """`_write_raw` persists stored records; writing `load()` back would collapse two
    entries onto one id and corrupt the registry permanently."""
    reg, _ = registry([{"id": 1, "name": "Arena do Gremio", "city": "Porto Alegre"},
                       {"id": 2, "name": "Arena do Grêmio", "city": "Porto Alegre"}])
    venues.mint([("Somewhere New", "Town")])
    stored = json.loads(reg.read_text())
    assert [e["id"] for e in stored] == [1, 2, 3]
    assert len({e["id"] for e in stored}) == 3, "no id may be written twice"


def test_mint_is_idempotent(registry):
    reg, _ = registry([{"id": 1, "name": "A", "city": "X"}])
    venues.mint([("A", "X")])
    assert json.loads(reg.read_text()) == [{"id": 1, "name": "A", "city": "X"}]


# --- proposals -------------------------------------------------------------

def test_propose_offers_the_single_town_case(registry):
    registry([{"id": 11, "name": "A. Le Coq Arena", "city": None},
              {"id": 12, "name": "A. Le Coq Arena", "city": "Tallinn"}])
    assert venues.propose() == [{"from": 11, "into": 12, "class": "missing-city"}]


def test_propose_refuses_the_ambiguous_case(registry):
    """`Allianz Stadium` in Sydney and Torino — no rule decides which the cityless one is."""
    registry([{"id": 130, "name": "Allianz Stadium", "city": None},
              {"id": 131, "name": "Allianz Stadium", "city": "Sydney"},
              {"id": 132, "name": "Allianz Stadium", "city": "Torino"}])
    assert venues.propose() == []


def test_propose_uses_the_loose_city_fold(registry):
    """`Washington` and `Washington, District of Columbia` are one town, so this is a
    single-town cluster the strict fold would have called ambiguous."""
    registry([{"id": 292, "name": "Audi Field", "city": None},
              {"id": 294, "name": "Audi Field", "city": "Washington"},
              {"id": 295, "name": "Audi Field", "city": "Washington, District of Columbia"}])
    assert venues.propose() == [{"from": 292, "into": 294, "class": "missing-city"}]


def test_propose_never_relitigates_a_frozen_decision(registry):
    """A confirmed merge is frozen; re-proposing it would let a later contradiction move
    ids that are already published."""
    registry([{"id": 5912, "name": "Stade Pierre Pibarot", "city": None},
              {"id": 5913, "name": "Stade Pierre Pibarot", "city": "Alès"}],
             merges=[{"from": 5912, "into": 5913, "class": "missing-city"}])
    assert venues.propose() == []


def test_write_proposals_leaves_existing_rows_untouched(registry):
    _, mrg = registry([{"id": 1, "name": "A", "city": None},
                       {"id": 2, "name": "A", "city": "X"}],
                      merges=[{"from": 90, "into": 91, "class": "reviewed"}])
    venues.write_proposals(venues.propose())
    rows = json.loads(mrg.read_text())
    assert rows[0] == {"from": 90, "into": 91, "class": "reviewed"}
    assert {"from": 1, "into": 2, "class": "missing-city"} in rows


# --- the committed registry ------------------------------------------------

def test_the_preflight_guards_the_merge_list(tmp_path, monkeypatch):
    """An absent merge list un-merges every decided ground silently — `load_merges()`
    returns `[]` rather than raising, so only the preflight can catch it."""
    from scripts import preflight

    monkeypatch.setattr(venues, "MERGES_FILE", tmp_path / "gone.json")
    problems = preflight.check_registries()
    assert any("Venue merge list" in p and "no file" in p for p in problems), problems
    assert venues.load_merges() == [], "load_merges is silent by design — the preflight is not"


def test_the_committed_merge_list_is_well_formed():
    """Every row names ids that exist, and no id is merged away twice."""
    entries = {e["id"] for e in venues._load_raw()}
    froms = [m["from"] for m in venues.load_merges()]
    for m in venues.load_merges():
        assert m["from"] in entries and m["into"] in entries, m
        assert m["from"] != m["into"], m
    assert len(froms) == len(set(froms)), "an id may be merged away only once"


def test_the_committed_registry_resolves_to_itself_today():
    """Nothing is merged yet — the mechanism ships before the merge (ADR 0042). This is the
    guard that a stray flip of DERIVE_SPELLING_MERGES, or a merge row landing without its
    rebuild and wholesale publish, does not reach the store unnoticed."""
    assert venues.DERIVE_SPELLING_MERGES is False
    assert venues.load_merges() == []
    resolved = venues.load()
    assert len(set(resolved.values())) == len(resolved)
