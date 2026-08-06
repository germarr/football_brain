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
- **Team names**, as confirmation, compared *canonically* (`_norm_team`): accents
  folded, punctuation and acronym dots removed, generic club tokens (`FC`/`CF`/`SC`)
  dropped, word order ignored. So "Charlotte FC" agrees with "Charlotte", and
  "Pumas UNAM" with "U.N.A.M. - Pumas" (ADR 0039). This is spelling reconciliation,
  not fuzzy matching: nothing is scored, and a genuinely different club never
  matches. What it cannot reach is an *alternate name* rather than a respelling —
  "Atlético de San Luis" against "Atletico San Luis" still disagrees, because the
  connective word cannot be dropped without merging real clubs. Disagreements are
  still reported verbatim so the operator sees both sides.

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

import re
import sqlite3
import unicodedata
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

#: The **delayed match** window (ADR 0038). One provider records the scheduled
#: kickoff and the other the actual one, so they disagree by hours rather than
#: minutes: Monterrey v Orlando City in the Leagues Cup was ESPN `00:55Z` against
#: API-Football `23:30`, 85 minutes apart, while every other match on that slate
#: agreed exactly — a delay, not a rounding convention.
#:
#: This is NOT a wider `KICKOFF_TOLERANCE`, and must never be merged into one. That
#: bound is safe because it cannot reach a neighbouring fixture; this one plainly can
#: (two Leagues Cup matches kicked off at 23:30 that night and two more at 00:30), so
#: it is admitted only under the strictly stronger anchor of **both** team names
#: agreeing — see `_compare`.
#:
#: Six hours because of what it actually guards. Against a *wrong fixture* it guards
#: nothing that the both-names anchor has not already settled: the same two teams
#: cannot play twice in an evening. What it catches is **our own data being wrong** —
#: a kickoff out by six hours is a delay, one out by a day is a bug we want to keep
#: hearing about.
DELAY_TOLERANCE = timedelta(hours=6)


class FixtureMismatch(Exception):
    """The supplied fixture_id is not this ESPN match. Never store the link."""


def _unlinked_remedy(link_required: bool) -> str:
    """The 'what do I do now' sentence every refusal ends with.

    ADR 0038 moved remedies to the raise sites because only they know which check
    failed. They do not, however, know whether the *caller* can act on the remedy —
    and "omit --fixture-id" is impossible from `football_blog.pipeline`, which
    requires it to draft (ADR 0039). So the caller declares that, and the refusal
    names the escape hatch that caller actually has: dropping the commentary rather
    than the link.
    """
    if link_required:
        return (
            "--fixture-id cannot be dropped here — the pipeline drafts from it. To "
            "draft this Fixture without the ESPN commentary, omit --espn-id instead; "
            "to ingest the commentary unlinked, run commentary.ingest on its own."
        )
    return "To ingest without a link, omit --fixture-id."


#: Generic club-type tokens dropped before comparing team names. Deliberately just
#: these three (ADR 0039). Every longer list tested merged clubs that are genuinely
#: distinct: dropping `afc` merges Liverpool with AFC Liverpool and Blackpool with AFC
#: Blackpool (fan-owned clubs, separate ids, separate matches), and dropping `usa`/`de`
#: merges Corinthians with Corinthians USA and Lyon with Club De Lyon. `fc`/`cf`/`sc`
#: carry no such freight — no pair of distinct clubs in the store is told apart by them
#: alone.
CLUB_TOKENS = frozenset({"fc", "cf", "sc"})


def _norm_team(name: str | None) -> str:
    """Canonical form of a team name, for comparing two providers' spellings.

    ESPN and API-Football name the same club differently in ways that are systematic
    rather than arbitrary, and exact-after-casefold could see none of them:

        'Charlotte FC'      vs 'Charlotte'            — a club-type suffix
        'Pumas UNAM'        vs 'U.N.A.M. - Pumas'     — acronym dots, and word order
        'Atletico San Luis' vs 'Atlético de San Luis' — an accent

    So: accents folded away, dots *inside* an acronym deleted rather than spaced (or
    `U.N.A.M.` becomes four one-letter tokens), remaining punctuation turned into
    space, `CLUB_TOKENS` dropped, and the rest compared as an order-insensitive set —
    returned as a sorted string so callers can still print what agreed.

    Digits are kept, which is what keeps a reserve side distinct from its first team
    ('Toronto FC II' does not become 'Toronto FC'). Suffixes are dropped wherever they
    appear, not just at the end, because ESPN writes 'FC Cincinnati' where
    API-Football writes 'Cincinnati FC'.
    """
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    # `u.n.a.m.` -> `unam`: only dots bounded by single letters, so `St. Louis` keeps
    # its word break and does not become `stlouis`.
    folded = re.sub(r"(?<=\b\w)\.(?=\w\b)", "", folded).replace(".", " ")
    tokens = re.sub(r"[^a-z0-9]+", " ", folded).split()
    kept = [t for t in tokens if t not in CLUB_TOKENS]
    # A club named only for its type ('FC') would otherwise normalise to nothing and
    # compare equal to every other such name.
    return " ".join(sorted(kept or tokens))


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
    link_required: bool = False,
) -> dict:
    """Confirm `fixture_id` in football.db IS `match`. Raise FixtureMismatch if not.

    Returns the Fixture row, plus `name_mismatch` recording whether the team
    names actually agreed. Read-only; never writes.

    Three ways a link is accepted: kickoffs equal; kickoffs within
    `KICKOFF_TOLERANCE` anchored by one agreeing team name; or a **delayed match** —
    kickoffs within `DELAY_TOLERANCE` with *both* names agreeing, where one provider
    recorded the scheduled kickoff and the other the actual one (ADR 0038).

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
            f"verified. {_unlinked_remedy(link_required)}"
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
            f"all. {_unlinked_remedy(link_required)}"
        )

    return _compare(fixture_id, dict(row), match, force=force, source=path.name,
                    link_required=link_required)


def _compare(fixture_id: int, row: dict, match: dict, *, force: bool, source: str,
             link_required: bool = False) -> dict:
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
    espn_teams = {_norm_team(match["home"]["team"]), _norm_team(match["away"]["team"])}
    db_teams = {_norm_team(row["home_team_name"]), _norm_team(row["away_team_name"])}
    name_mismatch = espn_teams != db_teams
    shared = espn_teams & db_teams

    # The **delayed match** path (ADR 0038). Beyond KICKOFF_TOLERANCE the clock is no
    # longer evidence of anything, so the link rests entirely on both names agreeing —
    # which is why this cannot be reached with one name, or with `force`. `force` waives
    # a naming disagreement, and a naming disagreement is exactly what disqualifies a
    # match from this path.
    delayed = (
        drift is not None
        and drift > KICKOFF_TOLERANCE
        and drift <= DELAY_TOLERANCE
        and not name_mismatch
    )
    if delayed:
        mins = int(drift.total_seconds() // 60)
        print(
            f"  LINKED despite a {mins} min kickoff drift — delayed match (ADR 0038).\n"
            f"    ESPN  {match['game_id']}: {match.get('date')!r}"
            + (f" [{match['league']}]" if match.get("league") else "")
            + f"\n    fixture {fixture_id} ({source}): {str(row['date'])!r} "
            f"[{row['league_name']}]\n"
            f"    Both team names agree, which is the whole of the evidence here."
        )

    if not delayed and (drift is None or drift > KICKOFF_TOLERANCE):
        tol = int(KICKOFF_TOLERANCE.total_seconds() // 60)
        if drift is None:
            why = "one of the two kickoffs could not be parsed"
        elif drift > DELAY_TOLERANCE:
            why = (
                f"they differ by {int(drift.total_seconds() // 60)} min, beyond both the "
                f"{tol} min tolerance and the "
                f"{int(DELAY_TOLERANCE.total_seconds() // 3600)}h delayed-match window"
            )
        else:
            # Inside the delay window, so the names are what refused it.
            why = (
                f"they differ by {int(drift.total_seconds() // 60)} min, which a delayed "
                f"match may — but a delayed match links only when BOTH team names agree, "
                f"and here "
                + (f"only {', '.join(sorted(shared))} does" if shared
                   else "neither does")
            )
        raise FixtureMismatch(
            f"kickoff disagrees — refusing to link.\n"
            f"  ESPN  {match['game_id']}: {match.get('date')!r} "
            f"({match['home']['team']} v {match['away']['team']})\n"
            f"  fixture {fixture_id} ({source}): {str(row['date'])!r} "
            f"({row['home_team_name']} v {row['away_team_name']}, {row['league_name']})\n"
            f"  {why}.\n"
            f"  --force-link cannot help: it waives team names only, and is checked "
            f"after this. {_unlinked_remedy(link_required)}"
        )

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
            f"to confirm this is the same match — check the fixture id. "
            f"{_unlinked_remedy(link_required)}\n"
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


def verify_fixture_pg(fixture_id: int, match: dict, *, force: bool = False,
                      link_required: bool = False) -> dict:
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
            f"publish it first (`uv run python -m football.publish.delta <league_id>`). "
            f"{_unlinked_remedy(link_required)}"
        )

    cols = ("id", "date", "league_name", "home_team_name", "away_team_name",
            "status", "home_goals", "away_goals")
    return _compare(fixture_id, dict(zip(cols, row)), match,
                    force=force, source="football_prod", link_required=link_required)


def verify_fixture_any(fixture_id: int, match: dict, *, force: bool = False,
                       link_required: bool = False) -> dict:
    """Try `football.db`, then the Published Store. Refuse only if both refuse.

    The fallback exists for the two cases `football.db` cannot answer for a
    same-day match — a Fixture added to the schedule since the last nightly
    rebuild, or one whose kickoff moved. It is strictly additive: a second source
    can confirm a link the first could not, and neither can waive the other's
    disagreement, because a refusal is raised unless *some* source agreed.
    """
    try:
        return verify_fixture(fixture_id, match, force=force,
                              link_required=link_required)
    except FixtureMismatch as first:
        try:
            result = verify_fixture_pg(fixture_id, match, force=force,
                                       link_required=link_required)
        except FixtureMismatch as second:
            raise FixtureMismatch(
                f"neither source could confirm fixture {fixture_id}.\n\n"
                f"  [football.db]     {first}\n\n"
                f"  [Published Store] {second}"
            ) from second
        return result
