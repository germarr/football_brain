"""Propose ESPN game ids for a Fixture, from ESPN's own scoreboard (ADR 0034).

ADR 0029 examined this and declined to build it, recording that the scoreboard would
only ever *propose* while `commentary/fixture_link.py` still checked exact kickoff and
exact team names, so "proposal by code is not assertion by hand." That argument was
right and is unchanged. What changed is the evidence: `--force-link` had become
habitual, because ESPN's club names disagree with ours often enough for Liga MX and
MLS that the flag was passed preemptively. **A check that is always waived checks
nothing** — and it is the check standing between us and linking commentary to the
wrong match, which `fixture_link.py` says nothing downstream could detect.

So this module exists to make that waiver an informed one: it puts ESPN's names beside
ours for a human to judge, one match at a time, instead of a reflexive flag.

## What it filters on, and what it deliberately does not

**Kickoff only**, within `fixture_link.KICKOFF_TOLERANCE` — the same 15 minutes the
ingest check uses, imported rather than repeated so the two cannot drift.

It does **not** filter on team names, and that is the whole point: the names are
exactly what disagree. Filtering on them would hide the true match and leave an empty
list, which reads as "ESPN does not have this game."

Nothing here waives anything. `fixture_link.verify_fixture_any` runs unchanged when
`commentary.ingest` is invoked, against whichever id the operator picked.

## Failure is not emptiness

`propose()` raises `LookupUnavailable` when ESPN cannot be reached or answers
unusably. It never returns `[]` for that case. "No match found" and "could not ask"
are different answers, and rendering them alike would send an operator hunting for a
game id that was there all along.

## Day boundaries

ESPN's `dates=YYYYMMDD` does not slice the day the way a UTC kickoff does — a Liga MX
match at 03:05 UTC is the previous evening locally, and ESPN files it accordingly. So
the fetch covers the UTC date *and its neighbours* and lets the kickoff tolerance do
the discriminating. Over-fetching by a day is one cached request; missing the match is
a wrong answer.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from commentary.fixture_link import KICKOFF_TOLERANCE

log = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
TIMEOUT = 20

#: Cached beside `commentary/espn.py`'s summary cache, same rationale: a scoreboard day
#: is immutable once its matches are over, so a re-render costs zero requests. Kept in a
#: separate directory because these are keyed by date, not by game id.
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "espn-scoreboard"


class LookupUnavailable(RuntimeError):
    """ESPN could not be asked, or answered unusably. Distinct from 'no candidates'."""


@dataclass
class Proposal:
    """One ESPN match that could be this Fixture. Nothing here is asserted."""
    game_id: str
    kickoff: dt.datetime              # tz-aware UTC
    home_team: str                    # ESPN's name, verbatim — the point is to show it
    away_team: str
    league: Optional[str]
    completed: bool
    detail: Optional[str]             # ESPN's status text, e.g. "FT" / "Postponed"
    drift_seconds: int                # signed: ESPN kickoff minus ours

    @property
    def drift_minutes(self) -> float:
        return self.drift_seconds / 60.0

    @property
    def exact_kickoff(self) -> bool:
        return self.drift_seconds == 0

    @property
    def commentary_url(self) -> str:
        return f"https://www.espn.com/soccer/commentary/_/gameId/{self.game_id}"


def _cache_path(day: dt.date) -> Path:
    return CACHE_DIR / f"{day:%Y%m%d}.json"


def _fetch_day(day: dt.date, *, refresh: bool = False) -> dict:
    """One scoreboard day, cache-first. Raises LookupUnavailable on any failure."""
    path = _cache_path(day)
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("espn_lookup: corrupt cache for %s, refetching", day)

    try:
        response = requests.get(
            SCOREBOARD_URL, params={"dates": f"{day:%Y%m%d}"}, timeout=TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        raise LookupUnavailable(
            f"ESPN's scoreboard for {day:%Y-%m-%d} could not be read: {e}"
        ) from e

    if "events" not in payload:
        raise LookupUnavailable(
            f"ESPN's scoreboard for {day:%Y-%m-%d} carried no `events` key "
            f"(got: {sorted(payload)!r})."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload


def _parse_event(event: dict) -> Optional[dict]:
    """Pull the few fields we show out of one scoreboard event. None if unusable."""
    game_id = str(event.get("id") or "").strip()
    raw_date = event.get("date")
    if not game_id or not raw_date:
        return None
    try:
        kickoff = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=dt.timezone.utc)

    competitions = event.get("competitions") or [{}]
    competition = competitions[0] if competitions else {}
    home = away = None
    for c in competition.get("competitors") or []:
        name = (c.get("team") or {}).get("displayName")
        if c.get("homeAway") == "home":
            home = name
        elif c.get("homeAway") == "away":
            away = name

    status = (competition.get("status") or event.get("status") or {})
    status_type = status.get("type") or {}

    return {
        "game_id": game_id,
        "kickoff": kickoff.astimezone(dt.timezone.utc),
        "home": home or "?",
        "away": away or "?",
        "league": (event.get("league") or {}).get("name") or event.get("shortName"),
        "completed": bool(status_type.get("completed")),
        "detail": status_type.get("shortDetail") or status_type.get("description"),
    }


def propose(
    kickoff_utc: dt.datetime,
    *,
    tolerance: dt.timedelta = KICKOFF_TOLERANCE,
    refresh: bool = False,
) -> list[Proposal]:
    """ESPN matches whose kickoff is within `tolerance` of ours, nearest first.

    Raises `LookupUnavailable` if ESPN could not be asked. An empty list means ESPN was
    asked and had nothing at that time — a real answer, and a different one.
    """
    if kickoff_utc.tzinfo is None:
        kickoff_utc = kickoff_utc.replace(tzinfo=dt.timezone.utc)
    kickoff_utc = kickoff_utc.astimezone(dt.timezone.utc)

    # The UTC day and its neighbours — see the module docstring on day boundaries.
    day = kickoff_utc.date()
    days = [day - dt.timedelta(days=1), day, day + dt.timedelta(days=1)]

    seen: set[str] = set()
    proposals: list[Proposal] = []
    failures: list[str] = []

    for d in days:
        try:
            payload = _fetch_day(d, refresh=refresh)
        except LookupUnavailable as e:
            failures.append(str(e))
            continue
        for event in payload.get("events") or []:
            parsed = _parse_event(event)
            if parsed is None or parsed["game_id"] in seen:
                continue
            drift = parsed["kickoff"] - kickoff_utc
            if abs(drift) > tolerance:
                continue
            seen.add(parsed["game_id"])
            proposals.append(Proposal(
                game_id=parsed["game_id"],
                kickoff=parsed["kickoff"],
                home_team=parsed["home"],
                away_team=parsed["away"],
                league=parsed["league"],
                completed=parsed["completed"],
                detail=parsed["detail"],
                drift_seconds=int(drift.total_seconds()),
            ))

    # Every day failed and nothing was found: we never actually asked. Saying "no
    # candidates" here would be the lie this module's docstring is about.
    if failures and not proposals and len(failures) == len(days):
        raise LookupUnavailable(
            "ESPN's scoreboard could not be read for any of "
            + ", ".join(f"{d:%Y-%m-%d}" for d in days)
            + f". First error: {failures[0]}"
        )

    proposals.sort(key=lambda p: (abs(p.drift_seconds), p.game_id))
    return proposals
