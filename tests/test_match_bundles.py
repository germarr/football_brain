"""Match Bundle and Fixture Row invariants that break without raising (ADR 0033/0044).

Same rule as `test_match_previews.py`: everything here renders a page. Nothing throws, no
route 500s, and what the reader gets is quietly untrue.

Five are worth stating outright, because each is a fork the design actually took:

  **`commentary` must never reach the Editorial Store.** ESPN key-moment lines are
  drafting input for the model; the TypeScript `FullFixture` has no field for them, so
  shipping them is invisible on the page — it is third-party text sitting in a collection
  whose list rule is public.

  **Every timestamp leaves as ISO-8601 with a `Z`.** `fixture.date` is naive UTC in
  Postgres. Drop the offset and `new Date(...)` reads it as local time: kickoff moves by
  hours, the page renders perfectly, and nothing anywhere raises.

  **A form guide is strictly before its own Fixture.** A non-strict cutoff puts the match
  being previewed at the top of its own last-five, where it reads as an ordinary row.

  **`gf`/`ga` invert for an away result.** Getting this backwards turns every away win
  into a loss — five plausible letters, no error, and the strip still looks like a strip.

  **The Fixture Row pass deletes what left the window.** Skipping the delete is the one
  failure here that looks *more* correct the longer it runs: every record left behind is
  individually valid, and the collection just grows into the 9,514-row table the whole
  bundle/row split exists to avoid.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timedelta, timezone

import pytest

from football.status import ENDED_WITHOUT_RESULT, FINAL
from football_blog import bundle as bundle_mod
from football_blog.bundle import (
    WINDOW_AFTER_DAYS, WINDOW_BEFORE_DAYS, _iso_z, build_rows, recent_form,
    serialize_bundle,
)
# Lives on the client, not the builder: what it encodes is how PocketBase stores what you
# send it, and `preview.py` needs the same three coercions.
from football_blog.pocketbase import unchanged
from football_blog.types import (
    CommentaryLine, FixtureRow, FullFixture, MatchEventRow, PlayerRow,
    SquadEntryRow, TeamMatchStatRow, TeamProfileRow, VenueRow,
)

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
KICKOFF = datetime(2026, 8, 5, 23, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class _FakeCursor:
    """A cursor that hands back canned rows and records what it was asked."""

    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _FakePB:
    """Just enough PocketBase to exercise the row window without a server."""

    def __init__(self, publications, rows=()):
        self._publications = publications
        self.rows = {int(r["postgres_fixture_id"]): r for r in rows}
        self.upserted: list[dict] = []
        self.deleted: list[str] = []

    def published_publications(self):
        return self._publications

    def list_rows(self):
        return list(self.rows.values())

    def upsert_row(self, data, existing=None):
        self.upserted.append(data)
        return data

    def delete_row(self, record_id):
        self.deleted.append(record_id)


def _full_fixture(**overrides) -> FullFixture:
    """A minimal but structurally real `FullFixture`, commentary included."""
    fixture = FixtureRow(
        id=1530114, date=KICKOFF, season=2026, league_id=772,
        league_name="Leagues Cup", tournament="Group Stage", phase=None,
        group_label=None, stage=None, matchday=None, round="Group Stage",
        status="FT", venue_id=5, home_team_id=11, home_team_name="Home FC",
        away_team_id=22, away_team_name="Away FC", home_goals=2, away_goals=1,
        penalty_home=None, penalty_away=None,
    )
    full = FullFixture(
        fixture=fixture,
        # Deliberately out of order — the serializer is what must sort them.
        events=[
            MatchEventRow(1530114, 3, 22, 70, None, "Goal", "Normal Goal", 902, None, None),
            MatchEventRow(1530114, 1, 11, 12, None, "Goal", "Normal Goal", 901, None, None),
            MatchEventRow(1530114, 2, 11, 44, None, "Card", "Yellow Card", 901, None, None),
        ],
        squad=[
            SquadEntryRow(1530114, 901, 11, "started", 90, "F", 7.4, True, 1, 0,
                          3, 2, 30, 1, 1, 0, 5, 3, 2, 1, 2, 1, 1, 0, 0, 0),
            SquadEntryRow(1530114, 902, 22, "started", 90, "M", 6.9, False, 1, 0,
                          2, 1, 40, 2, 3, 1, 6, 2, 1, 0, 1, 2, 0, 0, 0, 0),
        ],
        team_stats=[
            TeamMatchStatRow(1530114, 11, 55, 12, 5, 4, 3, 8, 4, 6, 2, 10,
                             1, 0, 3, 400, 350, 87, 1.8, 0.2),
        ],
        home_profile=TeamProfileRow(11, "Home FC", "HOM", "USA", 1996, False,
                                    "https://example.test/11.png", 253, "MLS", "USA",
                                    "North & Central America"),
        away_profile=TeamProfileRow(22, "Away FC", "AWY", "Mexico", 1943, False,
                                    "https://example.test/22.png", 262, "Liga MX",
                                    "Mexico", "North & Central America"),
        venue=VenueRow(5, "Estadio Test", "Testville", 999),
        players={
            901: PlayerRow(901, "A Player", "A", "Player", "USA"),
            902: PlayerRow(902, "B Player", "B", "Player", "Mexico"),
        },
        commentary=[
            CommentaryLine(1, "12'", 720.0, "home", "goal", "espn", "Goal! Home FC score."),
            CommentaryLine(2, "70'", 4200.0, "away", "goal", "espn", "Goal! Away FC pull one back."),
        ],
    )
    for k, v in overrides.items():
        setattr(full, k, v)
    return full


def _iter_strings(value):
    """Every string anywhere in a nested structure, with its key path."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_strings(v)
    elif isinstance(value, str):
        yield value


# --------------------------------------------------------------------------- #
# Serialization                                                                #
# --------------------------------------------------------------------------- #
def test_commentary_never_reaches_the_payload():
    """Drafting input, not render data — and the collection's list rule is public."""
    payload = serialize_bundle(_full_fixture())
    assert "commentary" not in payload
    text = " ".join(_iter_strings(payload))
    assert "pull one back" not in text, "an ESPN line survived somewhere in the payload"


def test_the_source_bundle_still_carries_commentary():
    """The strip is a property of the projection, not of `FullFixture`.

    If this ever fails the drafter has lost its reference colour — the same change, seen
    from the other side.
    """
    assert len(_full_fixture().commentary) == 2


def test_every_timestamp_is_iso_with_a_trailing_z():
    """A naive-looking timestamp is read as local time by the browser and never raises."""
    payload = serialize_bundle(_full_fixture())
    assert ISO_Z.match(payload["fixture"]["date"]), payload["fixture"]["date"]


def test_a_naive_datetime_is_treated_as_utc_not_as_local():
    naive = datetime(2026, 8, 5, 23, 30)
    assert _iso_z(naive) == "2026-08-05T23:30:00Z"


def test_a_non_utc_datetime_is_converted_rather_than_relabelled():
    """`-06:00` must move the clock, not just have its suffix swapped for a Z."""
    cdmx = datetime(2026, 8, 5, 17, 30, tzinfo=timezone(timedelta(hours=-6)))
    assert _iso_z(cdmx) == "2026-08-05T23:30:00Z"


def test_player_keys_are_strings_and_resolve_from_the_squad():
    payload = serialize_bundle(_full_fixture())
    assert all(isinstance(k, str) for k in payload["players"])
    for entry in payload["squad"]:
        assert str(entry["player_id"]) in payload["players"]


def test_events_are_sorted_by_event_index():
    """The order is the provider's and nothing downstream re-derives it."""
    payload = serialize_bundle(_full_fixture())
    assert [e["event_index"] for e in payload["events"]] == [1, 2, 3]


def test_the_payload_shape_matches_the_dataclass_minus_commentary():
    """Guards the mirror with `blog/la-cancha/src/lib/types.ts`, which is by declaration.

    A field added to `FullFixture` and not to the TS twin shows up here rather than as a
    missing section on a rendered page.
    """
    expected = {f.name for f in dataclasses.fields(FullFixture)} - {"commentary"}
    assert set(serialize_bundle(_full_fixture())) == expected


# --------------------------------------------------------------------------- #
# Recent form — the last-five strip                                            #
# --------------------------------------------------------------------------- #
def _form_row(fid, when, home_id, away_id, hg, ag, status="FT"):
    return (fid, when, status, home_id, f"Team {home_id}", away_id, f"Team {away_id}", hg, ag)


def test_form_is_taken_strictly_before_the_subject_fixture():
    """A non-strict cutoff puts the match being rendered at the top of its own form."""
    cur = _FakeCursor([])
    recent_form(cur, 11, KICKOFF)
    sql, params = cur.calls[0]
    assert "date < %s" in sql, sql
    assert KICKOFF.replace(tzinfo=None) in params


def test_form_only_counts_final_fixtures():
    """A cancelled Fixture has no result, and an `AWD` has a scoreline nobody played."""
    cur = _FakeCursor([])
    recent_form(cur, 11, KICKOFF)
    _sql, params = cur.calls[0]
    assert list(FINAL) in params
    for junk in ENDED_WITHOUT_RESULT:
        assert junk not in [s for p in params if isinstance(p, list) for s in p]


def test_goals_for_and_against_invert_for_an_away_result():
    """The one way a form strip can be wrong while still looking like a form strip."""
    cur = _FakeCursor([_form_row(1, KICKOFF - timedelta(days=7), 99, 11, 0, 1)])
    (row,) = recent_form(cur, 11, KICKOFF)
    assert row["is_home"] is False
    assert (row["gf"], row["ga"]) == (1, 0)
    assert row["outcome"] == "W"
    assert row["opponent_id"] == 99


def test_goals_read_straight_through_for_a_home_result():
    cur = _FakeCursor([_form_row(1, KICKOFF - timedelta(days=7), 11, 99, 3, 1)])
    (row,) = recent_form(cur, 11, KICKOFF)
    assert row["is_home"] is True
    assert (row["gf"], row["ga"], row["outcome"]) == (3, 1, "W")


@pytest.mark.parametrize("hg,ag,outcome", [(2, 1, "W"), (1, 1, "D"), (0, 2, "L")])
def test_outcome_follows_the_goals(hg, ag, outcome):
    cur = _FakeCursor([_form_row(1, KICKOFF - timedelta(days=7), 11, 99, hg, ag)])
    assert recent_form(cur, 11, KICKOFF)[0]["outcome"] == outcome


def test_a_shootout_win_is_still_a_draw_on_the_form_strip():
    """`home_goals`/`away_goals` are the on-pitch result; a shootout is not in them.

    A `PEN` tie is level on goals, and the form strip reports what was played.
    """
    cur = _FakeCursor([_form_row(1, KICKOFF - timedelta(days=7), 11, 99, 1, 1, status="PEN")])
    assert recent_form(cur, 11, KICKOFF)[0]["outcome"] == "D"


def test_form_is_ordered_newest_first_and_capped():
    cur = _FakeCursor([])
    recent_form(cur, 11, KICKOFF, count=5)
    sql, params = cur.calls[0]
    assert "ORDER BY date DESC" in sql, sql
    assert 5 in params


def test_a_team_with_no_prior_fixtures_gets_an_empty_strip_not_a_zero_row():
    """Real case: a newly-promoted club whose first Fixture in the store is this one."""
    assert recent_form(_FakeCursor([]), 11, KICKOFF) == []


def test_form_dates_are_iso_z():
    cur = _FakeCursor([_form_row(1, KICKOFF - timedelta(days=7), 11, 99, 1, 0)])
    assert ISO_Z.match(recent_form(cur, 11, KICKOFF)[0]["date"])


# --------------------------------------------------------------------------- #
# The Fixture Row window                                                       #
# --------------------------------------------------------------------------- #
def _window_row(fid, when, status="NS", league_id=262, hg=None, ag=None):
    return (fid, when, league_id, "Liga MX",
            11, "Home FC", "https://example.test/11.png",
            22, "Away FC", "https://example.test/22.png",
            status, hg, ag, None, None)


def _run_rows(monkeypatch, pb, rows, now):
    cur = _FakeCursor(rows)
    monkeypatch.setattr(bundle_mod, "get_conn", lambda: _FakeConn(cur))
    return build_rows(pb, now=now), cur


def test_rows_that_left_the_window_are_deleted(monkeypatch):
    """The failure that looks more correct the longer it runs."""
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    pb = _FakePB({262: {"id": "PUB"}},
                 rows=[{"id": "keep", "postgres_fixture_id": 1},
                       {"id": "stale", "postgres_fixture_id": 999}])
    counts, _ = _run_rows(monkeypatch, pb, [_window_row(1, now)], now)
    assert pb.deleted == ["stale"]
    assert counts["deleted"] == 1
    assert counts["written"] == 1


def test_a_row_still_in_the_window_is_never_deleted(monkeypatch):
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    pb = _FakePB({262: {"id": "PUB"}}, rows=[{"id": "keep", "postgres_fixture_id": 1}])
    _run_rows(monkeypatch, pb, [_window_row(1, now)], now)
    assert pb.deleted == []


def test_the_window_is_not_filtered_by_status(monkeypatch):
    """The ribbon draws finished and cancelled matches too.

    Both spellings of cancelled are in the store (`CANC` 78 rows, `Canc` 5). A status
    filter here would drop exactly the cards this collection exists to draw — and where a
    filter *is* wanted downstream it must whitelist, never blacklist.
    """
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    pb = _FakePB({262: {"id": "PUB"}})
    rows = [_window_row(1, now, "FT", hg=2, ag=1), _window_row(2, now, "CANC"),
            _window_row(3, now, "Canc"), _window_row(4, now, "NS")]
    counts, cur = _run_rows(monkeypatch, pb, rows, now)
    assert counts["written"] == 4
    assert {r["status"] for r in pb.upserted} == {"FT", "CANC", "Canc", "NS"}
    sql, _params = cur.calls[0]
    where = sql.split("WHERE", 1)[1]
    assert "status" not in where, f"the window query must not filter on status: {where}"


def test_the_window_spans_the_documented_days(monkeypatch):
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    pb = _FakePB({262: {"id": "PUB"}})
    _counts, cur = _run_rows(monkeypatch, pb, [], now)
    _sql, params = cur.calls[0]
    assert (now - timedelta(days=WINDOW_BEFORE_DAYS)).replace(tzinfo=None) in params
    assert (now + timedelta(days=WINDOW_AFTER_DAYS)).replace(tzinfo=None) in params


def test_a_fixture_in_an_unpublished_competition_is_not_written(monkeypatch):
    """Defence in depth for the gate the collection's list rule also enforces."""
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    pb = _FakePB({262: {"id": "PUB"}})
    _counts, _cur = _run_rows(monkeypatch, pb, [_window_row(1, now, league_id=39)], now)
    assert pb.upserted == []


def test_row_kickoffs_are_iso_z(monkeypatch):
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    pb = _FakePB({262: {"id": "PUB"}})
    _run_rows(monkeypatch, pb, [_window_row(1, datetime(2026, 8, 8, 1, 0))], now)
    assert ISO_Z.match(pb.upserted[0]["kickoff_utc"])
    assert ISO_Z.match(pb.upserted[0]["computed_at"])


def test_a_dry_run_writes_and_deletes_nothing(monkeypatch):
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    pb = _FakePB({262: {"id": "PUB"}}, rows=[{"id": "stale", "postgres_fixture_id": 999}])
    cur = _FakeCursor([_window_row(1, now)])
    monkeypatch.setattr(bundle_mod, "get_conn", lambda: _FakeConn(cur))
    counts = build_rows(pb, dry_run=True, now=now)
    assert pb.upserted == [] and pb.deleted == []
    assert counts["written"] == 1 and counts["deleted"] == 1


# --------------------------------------------------------------------------- #
# Compare-before-PATCH                                                         #
# --------------------------------------------------------------------------- #
# Every test here guards the same pair of opposite failures, and both are silent:
# a comparison that never matches quietly rewrites everything and saves nothing, while
# one that always matches quietly stops publishing corrections. The three coercions
# below are each enough on their own to cause the first.
def test_a_null_number_matches_the_zero_pocketbase_stores():
    """PocketBase has no nullable number: an unplayed Fixture's goals come back as `0`."""
    assert unchanged({"home_goals": None}, {"home_goals": 0})


def test_a_null_text_matches_the_empty_string_pocketbase_stores():
    assert unchanged({"home_logo": None}, {"home_logo": ""})


def test_a_timestamp_matches_across_pocketbases_reserialization():
    """We send `...T23:00:00Z`; PocketBase returns `... 23:00:00.000Z`. Same instant."""
    assert unchanged({"kickoff_utc": "2026-08-15T23:00:00Z"},
                     {"kickoff_utc": "2026-08-15 23:00:00.000Z"})


def test_a_real_difference_is_still_detected():
    assert not unchanged({"home_goals": 2}, {"home_goals": 1})
    assert not unchanged({"status": "FT"}, {"status": "NS"})
    assert not unchanged({"kickoff_utc": "2026-08-15T23:00:00Z"},
                         {"kickoff_utc": "2026-08-15 21:00:00.000Z"})


def test_a_zero_is_not_confused_with_a_null():
    """A real 0–0 must not read as "no score" — the coercion goes one way only."""
    assert not unchanged({"home_goals": 0}, {"home_goals": 3})


def test_a_changed_json_field_is_detected():
    assert not unchanged({"events": [{"event_index": 1}]}, {"events": []})
    assert unchanged({"events": [{"event_index": 1}]}, {"events": [{"event_index": 1}]})


def test_an_absent_record_is_never_unchanged():
    assert not unchanged({"home_goals": 0}, None)


def test_ignored_timestamps_do_not_defeat_the_comparison():
    """`computed_at` moves every run by construction; comparing it means never matching."""
    payload = {"status": "NS", "computed_at": "2026-08-07T03:00:00Z"}
    record = {"status": "NS", "computed_at": "2026-08-07 02:00:00.000Z"}
    assert unchanged(payload, record, ignore=("computed_at",))
    assert not unchanged(payload, record)


def test_pocketbases_own_columns_are_not_counted_as_differences():
    """Only keys we send are compared, so `id`/`created`/`updated` cannot force a write."""
    assert unchanged({"status": "NS"},
                     {"status": "NS", "id": "abc", "created": "…", "updated": "…"})


def test_an_unchanged_window_writes_nothing(monkeypatch):
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    row = _window_row(1, now, "FT", hg=2, ag=1)
    stored = {
        "id": "keep", "postgres_fixture_id": 1, "publication": "PUB",
        "kickoff_utc": now.strftime("%Y-%m-%d %H:%M:%S.000Z"),
        "league_id": 262, "league_name": "Liga MX",
        "home_team_id": 11, "home_team_name": "Home FC",
        "home_logo": "https://example.test/11.png",
        "away_team_id": 22, "away_team_name": "Away FC",
        "away_logo": "https://example.test/22.png",
        "status": "FT", "home_goals": 2, "away_goals": 1,
        "penalty_home": 0, "penalty_away": 0,
        "computed_at": "2026-08-07 01:00:00.000Z",
    }
    pb = _FakePB({262: {"id": "PUB"}}, rows=[stored])
    counts, _ = _run_rows(monkeypatch, pb, [row], now)
    assert pb.upserted == [], "an identical row must not be rewritten"
    assert counts == {"in_window": 1, "written": 0, "unchanged": 1, "deleted": 0}


def test_a_changed_score_still_writes(monkeypatch):
    """The opposite failure: a comparison that always matches stops publishing scores."""
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    stored = {"id": "keep", "postgres_fixture_id": 1, "status": "NS",
              "home_goals": 0, "away_goals": 0}
    pb = _FakePB({262: {"id": "PUB"}}, rows=[stored])
    counts, _ = _run_rows(monkeypatch, pb, [_window_row(1, now, "FT", hg=2, ag=1)], now)
    assert len(pb.upserted) == 1
    assert counts["written"] == 1 and counts["unchanged"] == 0


def test_no_published_publication_is_a_no_op_not_a_wipe(monkeypatch):
    """An empty Publication set must not be read as "every row has left the window"."""
    now = datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)
    pb = _FakePB({}, rows=[{"id": "keep", "postgres_fixture_id": 1}])
    assert build_rows(pb, now=now)["deleted"] == 0
    assert pb.deleted == []
