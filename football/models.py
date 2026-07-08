"""SQLModel schema — Layer 2 of docs/adr/0002.

Three tables mirroring CONTEXT.md language:
  - Player      : one row per footballer (biography).
  - Fixture     : one row per match.
  - SquadEntry  : one row per player *per fixture* (the ~22-man matchday squad),
                  tagged started / came_on / unused_sub, carrying per-game stats.
                  An "Appearance" is the subset where minutes > 0 (not a table).

Age is deliberately NOT stored — it is derived at fixture date (see age_at()).
Height/weight are nullable ints (some youth players have no recorded measurement).
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel


class Competition(SQLModel, table=True):
    id: int = Field(primary_key=True)   # provider league id (140, 262)
    name: str
    type: str = "league"                # "league" | "cup" (provider's league.type)


class Team(SQLModel, table=True):
    id: int = Field(primary_key=True)
    name: str


class Venue(SQLModel, table=True):
    """A stadium, identified by (name, city).

    The provider's own venue.id is null in ~32% of fixtures and inconsistent for
    the same ground (one fixture carries it, another for the same stadium does
    not), so it cannot be the key. `id` is instead a surrogate assigned by
    enumerating the unique (name, city) pairs in sorted order — deterministic
    across a drop-and-rebuild while the raw cache is stable, the same rationale as
    the Event table's key.

    provider_id keeps the provider's venue.id when any fixture exposes it (else
    null): a best-effort handle for a future /venues backfill of capacity /
    surface / coordinates, none of which the fixtures endpoint carries.
    """
    id: int = Field(primary_key=True)        # surrogate, deterministic (see above)
    name: str
    city: str | None = None
    provider_id: int | None = None           # provider venue.id when exposed; else null


class Player(SQLModel, table=True):
    id: int = Field(primary_key=True)
    name: str
    firstname: str | None = None
    lastname: str | None = None
    nationality: str | None = None          # canonical "country" (CONTEXT.md)
    birth_date: dt.date | None = None        # source of truth for age
    birth_country: str | None = None         # distinct from nationality
    birth_place: str | None = None
    height_cm: int | None = None             # unit-stripped from e.g. "187 cm"
    weight_kg: int | None = None
    photo: str | None = None


class Fixture(SQLModel, table=True):
    id: int = Field(primary_key=True)
    date: dt.datetime
    season: int
    league_id: int = Field(foreign_key="competition.id")
    league_name: str
    tournament: str                          # "Regular Season" | "Apertura" | cup name
    # Phase columns are cup-only (ADR 0010); all null for a league-type Competition.
    phase: str | None = None                 # "qualifying" | "group" | "knockout"
    group_label: str | None = None           # "Group A".."Group H"; null if not exposed
    stage: str | None = None                 # knockout/qualifying round name, e.g. "Final"
    matchday: int | None = None              # numeric round; null for knockout/qualifying
    round: str | None = None                 # full provider round, e.g. "Apertura - 5"
    status: str | None = None                # e.g. "FT"
    venue_id: int | None = Field(default=None, foreign_key="venue.id")  # null if provider named no venue
    home_team_id: int = Field(foreign_key="team.id")
    home_team_name: str
    away_team_id: int = Field(foreign_key="team.id")
    away_team_name: str
    home_goals: int | None = None
    away_goals: int | None = None


class PlayerTeam(SQLModel, table=True):
    """A player's Career Stint: one (Player, Team, Season) the provider records.

    Sourced from the players/teams endpoint, so it spans a player's WHOLE career
    across every competition and national team — not just the Competitions we
    collect fixtures for (CONTEXT.md: "Career Stint"). One row per season the
    provider places the player at a team, e.g. Cristiano Ronaldo -> Manchester
    United / Real Madrid / Juventus / Al-Nassr / Portugal ...

    team_id is a global provider team id and is intentionally NOT a foreign key to
    `team`: those clubs and national teams live outside our collected scope, so
    team_name is denormalized here (as Fixture denormalizes its team names).
    """
    player_id: int = Field(foreign_key="player.id", primary_key=True)
    team_id: int = Field(primary_key=True)
    season: int = Field(primary_key=True)
    team_name: str


class SquadEntry(SQLModel, table=True):
    fixture_id: int = Field(foreign_key="fixture.id", primary_key=True)
    player_id: int = Field(foreign_key="player.id", primary_key=True)
    team_id: int = Field(foreign_key="team.id")

    status: str                              # started | came_on | unused_sub
    minutes: int | None = None
    position: str | None = None
    rating: float | None = None
    captain: bool = False

    goals: int = 0
    assists: int = 0
    shots_total: int | None = None
    shots_on: int | None = None
    passes_total: int | None = None
    passes_key: int | None = None
    tackles_total: int | None = None
    interceptions: int | None = None
    duels_total: int | None = None
    duels_won: int | None = None
    dribbles_attempts: int | None = None
    dribbles_success: int | None = None
    fouls_drawn: int | None = None
    fouls_committed: int | None = None
    yellow: int = 0
    red: int = 0
    penalty_scored: int = 0
    penalty_missed: int = 0


class Event(SQLModel, table=True):
    """One time-stamped occurrence in a Fixture (ADR 0007): a goal, card,
    substitution, or VAR decision, sourced from the fixtures/events endpoint.

    The provider gives no event id, so the key is (fixture_id, event_index) — the
    event's position in that fixture's response array: deterministic across a
    drop-and-rebuild while the raw cache is stable, and chronological.

    `minute`/`extra` split a "90+3'" time into elapsed 90, extra 3 (Added Time;
    null when there is none). `type` is Goal | Card | subst | Var; `detail`
    refines it (Normal Goal | Penalty | Own Goal | Yellow Card | Red Card | ...).

    player_id/assist_id are nullable: an assist is only present on a Goal, and the
    provider occasionally names a coach (a card) or no one (some VAR). Any id not
    in the Player table is nulled at parse time so the build never fails on an
    out-of-scope actor. team_id is always one of the fixture's two Teams.
    """
    fixture_id: int = Field(foreign_key="fixture.id", primary_key=True)
    event_index: int = Field(primary_key=True)   # position in the fixture's event list
    team_id: int = Field(foreign_key="team.id")

    minute: int | None = None                     # time.elapsed
    extra: int | None = None                      # time.extra (Added Time), null if none
    type: str                                     # Goal | Card | subst | Var
    detail: str | None = None                     # Normal Goal | Penalty | Own Goal | Yellow Card | ...
    player_id: int | None = Field(default=None, foreign_key="player.id")
    assist_id: int | None = Field(default=None, foreign_key="player.id")
    comments: str | None = None


def age_at(birth_date: dt.date | None, on: dt.date) -> int | None:
    """Age in whole years at a given date (used with a fixture's date)."""
    if birth_date is None:
        return None
    years = on.year - birth_date.year
    if (on.month, on.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years
