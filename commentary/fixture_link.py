"""Verify an operator-supplied `fixture_id` against `football.db` — read-only.

This is the *only* contact between the commentary pipeline and the rest of the
store, and it is a read. `football.db` is opened through a `mode=ro` URI, so a
write is impossible rather than merely absent, and it is opened **only** when a
`--fixture-id` is supplied. Ingest without one never touches it at all.

Why verification exists at all: a **Narrated Match** is keyed on ESPN's game id
and usually has no Fixture — most competitions ESPN narrates are not ones we
collect. `fixture_id` is an optional bridge the operator types by hand, and a
typo would link commentary to the wrong match silently, which nothing downstream
could detect. So this module refuses rather than guesses, mirroring `join.py`.

What is compared:

- **Kickoff**, which is the real key. The two providers agree *exactly*, in UTC —
  verified across the cache (ESPN `2026-07-12T15:00Z` == football.db
  `2026-07-12 15:00:00.000000`). Fixture ids are globally unique.
- **Team names**, as confirmation. Exact match after normalising case/whitespace.
  National teams agree readily ("France"/"Spain"); clubs may not, and a
  disagreement is reported verbatim so the operator can see both sides rather
  than being told a vague "mismatch".

`data/football.db` alone is searched: it is a strict superset holding all 122,100
fixture ids, including every id in `liga-mx.db` and `world-cup.db`, so ADR 0011's
per-competition split does not require searching each one.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

FOOTBALL_DB = Path(__file__).resolve().parent.parent / "data" / "football.db"


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

    espn_kickoff = _parse_espn_date(match.get("date"))
    db_kickoff = _parse_db_date(row["date"])
    if espn_kickoff is None or db_kickoff is None or espn_kickoff != db_kickoff:
        raise FixtureMismatch(
            f"kickoff disagrees — refusing to link.\n"
            f"  ESPN  {match['game_id']}: {match.get('date')!r} "
            f"({match['home']['team']} v {match['away']['team']})\n"
            f"  fixture {fixture_id}: {row['date']!r} "
            f"({row['home_team_name']} v {row['away_team_name']}, {row['league_name']})"
        )

    espn_teams = {_norm_team(match["home"]["team"]), _norm_team(match["away"]["team"])}
    db_teams = {_norm_team(row["home_team_name"]), _norm_team(row["away_team_name"])}
    name_mismatch = espn_teams != db_teams

    if name_mismatch and not force:
        shared = espn_teams & db_teams
        raise FixtureMismatch(
            f"kickoff matches but teams do not — refusing to link. The two providers "
            f"may simply name this team differently; if so, re-run with --force-link "
            f"once you are certain it is the same match.\n"
            f"  ESPN  {match['game_id']}: {match['home']['team']} v {match['away']['team']}"
            f"  ({match['home']['score']}–{match['away']['score']})\n"
            f"  fixture {fixture_id}: {row['home_team_name']} v {row['away_team_name']} "
            f"({row['league_name']})"
            f"  ({row['home_goals']}–{row['away_goals']})\n"
            f"  agreeing on: {', '.join(sorted(shared)) or 'NEITHER team — check the id'}"
        )

    out = dict(row)
    out["name_mismatch"] = name_mismatch
    return out
