"""Verify an operator-supplied `fixture_id` against the rest of the store — read-only.

This module is the only contact between the commentary pipeline and the rest of the
store, and the contract is defined by **kind, not by count**: every such contact is
read-only, verification-only, and opened *only* when a `--fixture-id` is supplied.
Ingest without one never reaches outside this package at all.

Two sources now answer the question, and neither is ever written. `football.db` is
opened through a `mode=ro` URI, so a write is impossible rather than merely absent;
the **Published Store** is opened only by `verify_fixture_pg`, which imports `psycopg`
and `football.config` *inside the function* so that a plain `football.db` ingest keeps
this package's stdlib-and-`requests` dependency profile (see ADR 0029).

Why verification exists at all: a **Narrated Match** is keyed on ESPN's game id
and usually has no Fixture — most competitions ESPN narrates are not ones we
collect. `fixture_id` is an optional bridge the operator types by hand, and a
typo would link commentary to the wrong match silently, which nothing downstream
could detect. So this module refuses rather than guesses, mirroring `join.py`.

What is compared:

- **Kickoff**, which is the real key, in UTC, within `KICKOFF_TOLERANCE` (15
  min). The two providers usually agree *exactly* (ESPN `2026-07-12T15:00Z` ==
  football.db `2026-07-12 15:00:00.000000`), but not always: ESPN rounds to the
  hour where API-Football keeps the broadcast minute, so a true Liga MX link can
  read `03:00Z` against `03:05` (ADR 0030). Fixture ids are globally unique.
- **Team names**, as confirmation. Exact match after normalising case/whitespace.
  National teams agree readily ("France"/"Spain"); clubs may not, and a
  disagreement is reported verbatim so the operator can see both sides rather
  than being told a vague "mismatch".

The two are not independent: an *exact* kickoff stands on its own, but a merely
*close* one must be anchored by at least one team name agreeing exactly. That
keeps the tolerance from compounding with `--force-link` into a link where
neither field was really checked.

`data/football.db` alone is searched: it is a strict superset holding all 122,100
fixture ids, including every id in `liga-mx.db` and `world-cup.db`, so ADR 0011's
per-competition split does not require searching each one.

## The staleness problem, and the second source

`football.db` is rebuilt by the nightly Refresh (ADR 0018) and by nothing else.
That is fine for the usual workflow — a Fixture played today was already in the
cache as `NS` at last night's rebuild, carrying the kickoff and both team names,
which is everything compared here. It breaks in exactly two cases: a Fixture the
provider added to the schedule after the rebuild, and one whose kickoff was moved.
Both surface as a refusal that a re-run cannot fix until 04:00 the next morning.

So `verify_fixture_pg` offers the same check against the **Published Store** (ADR
0027), which `football_blog.pipeline` refreshes moments before it ingests, and
`verify_fixture_any` tries `football.db` first and falls back to it. The contract
is unchanged — two sources that must each either agree or be absent, never a
guess — and the fallback only ever *adds* a chance to confirm, never to waive:
a Fixture that disagrees in both is still refused, quoting both.

`psycopg` is imported inside `verify_fixture_pg`, so plain `commentary.ingest`
against `football.db` keeps its SQLite-and-stdlib-only dependency profile.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

FOOTBALL_DB = Path(__file__).resolve().parent.parent / "data" / "football.db"

#: How far the two providers' kickoffs may disagree and still describe the same
#: match. ESPN rounds a Liga MX kickoff to the hour where API-Football keeps the
#: broadcast minute (`03:00Z` vs `03:05`); 139 of the 2026 Liga MX fixtures sit
#: off the hour, so exact equality refuses a whole recurring class of true links.
#: Kept small deliberately: consecutive fixtures in one Competition are hours
#: apart, so 15 minutes cannot reach the neighbouring match, and a *simultaneous*
#: one is caught by the team-name anchor below rather than by this bound.
KICKOFF_TOLERANCE = timedelta(minutes=15)


class FixtureMismatch(Exception):
    """The supplied fixture_id is not this ESPN match. Never store the link."""


def _norm_team(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def _parse_espn_date(value: str | None) -> datetime | None:
    """ESPN kickoff ('2026-07-14T19:00Z') -> naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%MZ")
    except ValueError:
        try:  # tolerate a seconds-bearing variant if ESPN ever sends one
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None


def _parse_db_date(value: str | None) -> datetime | None:
    """football.db kickoff ('2026-07-14 19:00:00.000000') -> naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def verify_fixture(
    fixture_id: int,
    match: dict,
    *,
    db_path: Path | None = None,
    force: bool = False,
) -> dict:
    """Confirm `fixture_id` in football.db IS `match`. Raise FixtureMismatch if not.

    Returns the Fixture row, plus `name_mismatch` recording whether the team
    names actually agreed. Read-only; never writes.

    `force` (the CLI's `--force-link`) waives the **team-name** check only, for
    the common case where the two providers simply spell a team differently —
    ESPN says "United States" where API-Football says "USA". It does **not**
    waive the kickoff check or the existence check: those are what make the link
    checkable at all, and a forced link with a wrong kickoff is just a wrong link.
    Nor does it waive the anchor an inexact kickoff requires — if the kickoffs
    are merely close *and* no team name agrees, the refusal stands however hard
    it is forced, because then nothing at all was verified.
    Forcing is a human assertion that these two records are the same match, and
    nothing downstream can detect it if that assertion is wrong.
    """
    path = db_path or FOOTBALL_DB
    if not path.exists():
        raise FixtureMismatch(
            f"{path} does not exist, so --fixture-id {fixture_id} cannot be "
            f"verified. Re-run without --fixture-id to ingest without the link."
        )

    # mode=ro: SQLite itself refuses any write through this handle.
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id, date, league_name, home_team_name, away_team_name, status, "
            "       home_goals, away_goals "
            "FROM fixture WHERE id = ?",
            (fixture_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise FixtureMismatch(
            f"fixture_id {fixture_id} is not in {path.name}. That is a typo, not an "
            f"untracked competition: an untracked competition has no fixture id at "
            f"all, so omit --fixture-id entirely to ingest this match unlinked."
        )

    return _compare(fixture_id, dict(row), match, force=force, source=path.name)


def _compare(fixture_id: int, row: dict, match: dict, *, force: bool, source: str) -> dict:
    """The comparison itself, shared by both sources. `row` must carry `date`,
    `home_team_name`, `away_team_name`, `league_name` and both goal columns.

    Kept separate from the fetch so that adding a source cannot accidentally
    weaken the check: there is exactly one implementation of "is this the same
    match", and every caller goes through it.
    """
    espn_kickoff = _parse_espn_date(match.get("date"))
    db_kickoff = _coerce_db_date(row["date"])
    drift = (
        None
        if espn_kickoff is None or db_kickoff is None
        else abs(espn_kickoff - db_kickoff)
    )
    if drift is None or drift > KICKOFF_TOLERANCE:
        raise FixtureMismatch(
            f"kickoff disagrees — refusing to link.\n"
            f"  ESPN  {match['game_id']}: {match.get('date')!r} "
            f"({match['home']['team']} v {match['away']['team']})\n"
            f"  fixture {fixture_id} ({source}): {str(row['date'])!r} "
            f"({row['home_team_name']} v {row['away_team_name']}, {row['league_name']})"
        )

    espn_teams = {_norm_team(match["home"]["team"]), _norm_team(match["away"]["team"])}
    db_teams = {_norm_team(row["home_team_name"]), _norm_team(row["away_team_name"])}
    name_mismatch = espn_teams != db_teams
    shared = espn_teams & db_teams

    # A kickoff *within* tolerance but not identical is weaker evidence than an
    # exact one, so it must be anchored: at least one team name has to agree
    # exactly. Without that anchor an inexact kickoff plus --force-link would
    # waive both checks at once, and a typo'd id pointing at a genuinely
    # different match in the same broadcast slot (Liga MX routinely runs two)
    # would link silently. `force` cannot waive this — it is not a naming
    # disagreement, it is the absence of anything to verify against.
    if drift and not shared:
        raise FixtureMismatch(
            f"kickoff is close but not exact and no team name agrees — refusing "
            f"to link. The providers differ by {int(drift.total_seconds() // 60)} "
            f"min, which is within the {int(KICKOFF_TOLERANCE.total_seconds() // 60)} "
            f"min tolerance, but with neither team in common there is nothing left "
            f"to confirm this is the same match — check the fixture id.\n"
            f"  ESPN  {match['game_id']}: {match.get('date')!r} "
            f"({match['home']['team']} v {match['away']['team']})\n"
            f"  fixture {fixture_id} ({source}): {str(row['date'])!r} "
            f"({row['home_team_name']} v {row['away_team_name']}, {row['league_name']})"
        )

    if name_mismatch and not force:
        raise FixtureMismatch(
            f"kickoff matches but teams do not — refusing to link. The two providers "
            f"may simply name this team differently; if so, re-run with --force-link "
            f"once you are certain it is the same match.\n"
            f"  ESPN  {match['game_id']}: {match['home']['team']} v {match['away']['team']}"
            f"  ({match['home']['score']}–{match['away']['score']})\n"
            f"  fixture {fixture_id} ({source}): {row['home_team_name']} v "
            f"{row['away_team_name']} ({row['league_name']})"
            f"  ({row['home_goals']}–{row['away_goals']})\n"
            f"  agreeing on: {', '.join(sorted(shared)) or 'NEITHER team — check the id'}"
        )

    out = dict(row)
    out["name_mismatch"] = name_mismatch
    out["verified_against"] = source
    return out


def _coerce_db_date(value) -> datetime | None:
    """Accept either store's kickoff: SQLite hands back the text it stored, the
    Published Store a naive-UTC `datetime` (ADR 0005 — `fixture.date` is
    TIMESTAMP WITHOUT TIME ZONE, read as naive)."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    return _parse_db_date(value)


def verify_fixture_pg(fixture_id: int, match: dict, *, force: bool = False) -> dict:
    """`verify_fixture`, against the Published Store instead of `football.db`.

    Same contract, same refusals — only the source differs. Useful when
    `football.db` is stale relative to the Postgres store, which is the normal
    state of affairs between a `refresh_pg`/pipeline run and the 04:00 rebuild.
    """
    import psycopg  # local: keeps plain football.db ingest stdlib-only

    from football.config import load_pg_url

    with psycopg.connect(load_pg_url(), connect_timeout=30) as pg:
        row = pg.execute(
            "SELECT id, date, league_name, home_team_name, away_team_name, status, "
            "       home_goals, away_goals "
            "FROM public.fixture WHERE id = %s",
            (fixture_id,),
        ).fetchone()

    if row is None:
        raise FixtureMismatch(
            f"fixture_id {fixture_id} is not in the Published Store either. Either it "
            f"is a typo, or its Competition has never been published to Postgres — "
            f"publish it first (`uv run python -m football.refresh_pg <league_id>`), "
            f"or omit --fixture-id to ingest this match unlinked."
        )

    cols = ("id", "date", "league_name", "home_team_name", "away_team_name",
            "status", "home_goals", "away_goals")
    return _compare(fixture_id, dict(zip(cols, row)), match,
                    force=force, source="football_prod")


def verify_fixture_any(fixture_id: int, match: dict, *, force: bool = False) -> dict:
    """Try `football.db`, then the Published Store. Refuse only if both refuse.

    The fallback exists for the two cases `football.db` cannot answer for a
    same-day match — a Fixture added to the schedule since the last nightly
    rebuild, or one whose kickoff moved. It is strictly additive: a second source
    can confirm a link the first could not, and neither can waive the other's
    disagreement, because a refusal is raised unless *some* source agreed.
    """
    try:
        return verify_fixture(fixture_id, match, force=force)
    except FixtureMismatch as first:
        try:
            result = verify_fixture_pg(fixture_id, match, force=force)
        except FixtureMismatch as second:
            raise FixtureMismatch(
                f"neither source could confirm fixture {fixture_id}.\n\n"
                f"  [football.db]     {first}\n\n"
                f"  [Published Store] {second}"
            ) from second
        return result
