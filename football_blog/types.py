"""Row shapes mirroring the Postgres schema. Uses dataclasses (no Pydantic
dependency — we only need shape typing, not validation).

Field names and Optional-ness EXACTLY match blog/la-cancha/src/lib/types.ts.
When you change one, change the other.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

FixtureStatus = Literal["FT", "NS", "CANC", "Canc", "PEN", "AET", "PST"]
MatchEventType = Literal["Goal", "Card", "subst", "Var"]
SquadEntryStatus = Literal["started", "came_on", "unused_sub"]
Outcome = Literal["W", "D", "L"]


@dataclass
class FixtureRow:
    id: int
    date: datetime
    season: int
    league_id: int
    league_name: str
    tournament: str
    phase: Optional[str]
    group_label: Optional[str]
    stage: Optional[str]
    matchday: Optional[int]
    round: Optional[str]
    status: Optional[str]
    venue_id: Optional[int]
    home_team_id: int
    home_team_name: str
    away_team_id: int
    away_team_name: str
    home_goals: Optional[int]
    away_goals: Optional[int]
    penalty_home: Optional[int]
    penalty_away: Optional[int]


@dataclass
class MatchEventRow:
    fixture_id: int
    event_index: int
    team_id: int
    minute: Optional[int]
    extra: Optional[int]
    type: str
    detail: Optional[str]
    player_id: Optional[int]
    assist_id: Optional[int]
    comments: Optional[str]


@dataclass
class SquadEntryRow:
    fixture_id: int
    player_id: int
    team_id: int
    status: str
    minutes: Optional[int]
    position: Optional[str]
    rating: Optional[float]
    captain: bool
    goals: int
    assists: int
    shots_total: Optional[int]
    shots_on: Optional[int]
    passes_total: Optional[int]
    passes_key: Optional[int]
    tackles_total: Optional[int]
    interceptions: Optional[int]
    duels_total: Optional[int]
    duels_won: Optional[int]
    dribbles_attempts: Optional[int]
    dribbles_success: Optional[int]
    fouls_drawn: Optional[int]
    fouls_committed: Optional[int]
    yellow: int
    red: int
    penalty_scored: int
    penalty_missed: int


@dataclass
class TeamMatchStatRow:
    fixture_id: int
    team_id: int
    possession: Optional[int]
    shots_total: Optional[int]
    shots_on: Optional[int]
    shots_off: Optional[int]
    shots_blocked: Optional[int]
    shots_inside: Optional[int]
    shots_outside: Optional[int]
    corners: Optional[int]
    offsides: Optional[int]
    fouls: Optional[int]
    yellow: Optional[int]
    red: Optional[int]
    saves: Optional[int]
    passes_total: Optional[int]
    passes_accurate: Optional[int]
    passes_pct: Optional[int]
    expected_goals: Optional[float]
    goals_prevented: Optional[float]


@dataclass
class TeamProfileRow:
    id: int
    name: str
    code: Optional[str]
    country: Optional[str]
    founded: Optional[int]
    is_national: Optional[bool]
    logo: Optional[str]
    league_id: Optional[int]
    league_name: Optional[str]
    league_country: Optional[str]
    continent: Optional[str]


@dataclass
class VenueRow:
    id: int
    name: str
    city: Optional[str]
    provider_id: Optional[int]


@dataclass
class PlayerRow:
    id: int
    name: str
    firstname: Optional[str]
    lastname: Optional[str]
    nationality: Optional[str]


@dataclass
class CommentaryLine:
    sequence: int
    minute: Optional[str]
    clock_seconds: Optional[float]
    team: Optional[str]
    category: str
    source: str
    text: str


@dataclass
class FullFixture:
    fixture: FixtureRow
    events: list[MatchEventRow]
    squad: list[SquadEntryRow]
    team_stats: list[TeamMatchStatRow]
    home_profile: Optional[TeamProfileRow]
    away_profile: Optional[TeamProfileRow]
    venue: Optional[VenueRow]
    players: dict[int, PlayerRow]  # keyed by player_id
    commentary: list[CommentaryLine]  # ESPN key-moment lines; empty if the fixture is not narrated. Python-side only — the TS FullFixture (blog/la-cancha) is a rendering bundle and doesn't need this.
