"""The two ways blog onboarding could fail without saying so (ADR 0033/0037).

Both are writes to stores that do not rebuild, and both would report success:

  * **`--yes` inventing a slug.** The slug is permanent — it is in every published URL —
    and a derived one is right about a third of the time (`mls`, not
    `major-league-soccer`). A run that quietly fell back to the derived value would mint
    a URL nobody chose and exit 0.
  * **create-if-absent regressing to an upsert.** `llm_prompt_overrides` is authored from
    the Desk (ADR 0034's layer 2) and the World Cup's holds over 1,500 characters. A
    second click that re-submitted form defaults over it would destroy that silently, in
    the Editorial Store, which has no rebuild path and no backup.

And one that is not silent but is worth pinning, because the whole design rests on it:
the derived publish set can never omit an existing Publication.

No network: PocketBase and Postgres are both faked, because what is under test is the
refusal logic, not the transport.
"""
from __future__ import annotations

import pytest

from football_blog import onboard


class FakePB:
    """Just enough PocketBase: publications by competition id."""

    base_url = "http://pb.invalid"

    def __init__(self, publications=None):
        self.publications = publications or {}
        self.posted = []

    def list_publications(self, only_published=False):
        return list(self.publications.values())

    def get_publication_by_competition_id(self, competition_id):
        return self.publications.get(int(competition_id))

    def _headers(self):
        return {}


@pytest.fixture
def derived(monkeypatch):
    """`derive_defaults` without Postgres."""
    monkeypatch.setattr(onboard, "derive_defaults", lambda cid: {
        "display_name": "Brasileirão", "slug": "brasileirao",
        "display_timezone": "America/Sao_Paulo", "name": "Brasileirão", "country": "Brazil",
    })


@pytest.fixture
def no_http(monkeypatch):
    """Fail loudly if anything tries to actually POST during a refusal test."""
    import httpx

    def boom(*a, **k):
        raise AssertionError("a refusal path reached the network")

    monkeypatch.setattr(httpx, "post", boom)


# --------------------------------------------------------------------------- #
# 1. It refuses to invent the fields that are permanent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("missing", ["slug", "language", "timezone_name"])
def test_refuses_to_invent_a_required_field(derived, no_http, missing):
    kwargs = {"slug": "brasileirao", "language": "es", "timezone_name": "America/Sao_Paulo"}
    kwargs[missing] = None
    with pytest.raises(onboard.PublicationFieldMissing) as e:
        onboard.create_publication(71, FakePB(), **kwargs)
    assert "slug" in str(e.value) or "language" in str(e.value) or "timezone" in str(e.value)


def test_the_refusal_still_shows_what_it_would_have_guessed(derived, no_http):
    """A refusal that hides the suggestion just makes the operator go find it."""
    with pytest.raises(onboard.PublicationFieldMissing) as e:
        onboard.create_publication(71, FakePB(), slug=None, language="es",
                                   timezone_name="America/Sao_Paulo")
    assert "brasileirao" in str(e.value)


def test_rejects_an_unknown_language(derived, no_http):
    with pytest.raises(onboard.PublicationFieldMissing):
        onboard.create_publication(71, FakePB(), slug="brasileirao", language="pt",
                                   timezone_name="America/Sao_Paulo")


# --------------------------------------------------------------------------- #
# 2. It never rewrites an existing Publication
# --------------------------------------------------------------------------- #
def test_existing_publication_is_returned_untouched(derived, no_http):
    """The second click. Nothing is posted, and the authored overrides survive."""
    existing = {"id": "abc", "postgres_competition_id": 1, "slug": "mundial-2026",
                "display_name": "Mundial 2026", "default_language": "es",
                "display_timezone": "America/Mexico_City", "published": True,
                "llm_prompt_overrides": "AUTHORED GUIDANCE " * 90}
    pb = FakePB({1: existing})

    record, created = onboard.create_publication(
        1, pb, slug="world-cup", display_name="World Cup", language="en",
        timezone_name="Europe/London", brand_color="#000000")

    assert created is False
    assert record is existing
    assert record["slug"] == "mundial-2026"
    assert record["default_language"] == "es"
    assert record["llm_prompt_overrides"].startswith("AUTHORED GUIDANCE")


def test_a_second_call_cannot_flip_published_off(derived, no_http):
    """`published` is a human act in both directions; this path must not touch it."""
    existing = {"id": "abc", "postgres_competition_id": 1, "slug": "mundial-2026",
                "published": True}
    record, created = onboard.create_publication(
        1, FakePB({1: existing}), slug="x", language="es", timezone_name="UTC")
    assert created is False and record["published"] is True


# --------------------------------------------------------------------------- #
# 3. The derived publish set cannot drop a Publication
# --------------------------------------------------------------------------- #
def test_derived_set_covers_every_publication_plus_the_new_one():
    pb = FakePB({
        262: {"postgres_competition_id": 262}, 253: {"postgres_competition_id": 253},
        1: {"postgres_competition_id": 1},
    })
    assert onboard.derived_publish_set(pb, 71) == [1, 71, 253, 262]


def test_derived_set_is_idempotent_for_an_already_covered_competition():
    pb = FakePB({262: {"postgres_competition_id": 262}, 253: {"postgres_competition_id": 253}})
    assert onboard.derived_publish_set(pb, 262) == [253, 262]


def test_derived_set_beats_the_stale_default():
    """The bug this replaces, pinned as a regression.

    `pg.DEFAULT_LEAGUE_IDS` is `[262, 253]` while three Publications exist, so a bare
    wholesale publish drops the World Cup and reports success. The derived set must
    never be a subset of that default when a third Publication exists.
    """
    from football.publish.pg import DEFAULT_LEAGUE_IDS
    pb = FakePB({262: {"postgres_competition_id": 262},
                 253: {"postgres_competition_id": 253},
                 1: {"postgres_competition_id": 1}})
    derived_ids = onboard.derived_publish_set(pb, 262)
    assert 1 in derived_ids
    assert set(derived_ids) - set(DEFAULT_LEAGUE_IDS), (
        "the derived set adds nothing over the stale default — the drop is back"
    )


# --------------------------------------------------------------------------- #
# 4. The board's argv is the command's argv
# --------------------------------------------------------------------------- #
def test_board_argv_never_contains_a_publish_flag():
    """ADR 0021: the UI is a second front door. ADR 0037: it stops at Draftable.

    There is no parameter on `build_onboard_argv` that could set `published`, so this
    asserts the absence structurally rather than trusting the caller.
    """
    from surfaces.competitions.app import build_onboard_argv
    argv = build_onboard_argv(71, slug="brasileirao", language="es",
                              timezone_name="America/Sao_Paulo",
                              display_name="Brasileirão", brand_color="#009C3B")
    assert "--yes" in argv
    assert not any("publish" in a and a != "--skip-publish" for a in argv)
    assert argv[1:4] == ["-m", "football_blog.onboard", "--league-id"]
