"""Batched fixture loader. Mirrors blog/la-cancha/src/lib/loader.ts —
eight SQL round-trips regardless of input size (fixture, event, squadentry,
teammatchstat, teamprofile, venue, player, commentary_line+narrated_match).
The commentary join is Python-side only; the TS loader doesn't fetch it.

Note on timestamps: `fixture.date` is TIMESTAMP WITHOUT TIME ZONE in
Postgres, treated as naive UTC per ADR-0005. psycopg returns naive datetime
objects by default; we attach UTC to every fixture row so downstream callers
(fixture_slug, formatters, drafter) can rely on tz-aware datetimes.
The TS side does the equivalent via a postgres.js `types.timestamp` override.
"""
from datetime import timezone
from typing import Iterable

from .postgres import get_conn
from .types import (
    FixtureRow, MatchEventRow, SquadEntryRow, TeamMatchStatRow,
    TeamProfileRow, VenueRow, PlayerRow, CommentaryLine, FullFixture,
)


def _fixture(row) -> FixtureRow:
    f = FixtureRow(*row)
    # Attach UTC to the naive datetime so downstream code has tz-aware values.
    if f.date is not None and f.date.tzinfo is None:
        f.date = f.date.replace(tzinfo=timezone.utc)
    return f


def _event(row) -> MatchEventRow:
    return MatchEventRow(*row)


def _squad(row) -> SquadEntryRow:
    return SquadEntryRow(*row)


def _tstat(row) -> TeamMatchStatRow:
    return TeamMatchStatRow(*row)


def _profile(row) -> TeamProfileRow:
    return TeamProfileRow(*row)


def _venue(row) -> VenueRow:
    return VenueRow(*row)


def _player(row) -> PlayerRow:
    return PlayerRow(*row)


# ESPN commentary categories fed to the drafter as reference color. Excludes
# high-noise filler (foul, free_kick_won, corner, offside, delay, period_marker,
# added_time, lineups, other) — those don't add material the model can't infer
# from the event timeline + team stats. See football_brain/commentary/taxonomy.py
# for the full vocabulary.
COMMENTARY_KEY_CATEGORIES = (
    "goal", "own_goal",
    "penalty_awarded", "penalty_scored", "penalty_missed",
    "yellow_card", "red_card",
    "substitution",
    "var_decision",
    "attempt_saved", "attempt_missed", "attempt_blocked", "attempt_woodwork",
)

# Column lists locked in — order MUST match the dataclass constructors.
COLS_FIXTURE = "id, date, season, league_id, league_name, tournament, phase, group_label, stage, matchday, round, status, venue_id, home_team_id, home_team_name, away_team_id, away_team_name, home_goals, away_goals, penalty_home, penalty_away"
COLS_EVENT = "fixture_id, event_index, team_id, minute, extra, type, detail, player_id, assist_id, comments"
COLS_SQUAD = "fixture_id, player_id, team_id, status, minutes, position, rating, captain, goals, assists, shots_total, shots_on, passes_total, passes_key, tackles_total, interceptions, duels_total, duels_won, dribbles_attempts, dribbles_success, fouls_drawn, fouls_committed, yellow, red, penalty_scored, penalty_missed"
COLS_TSTAT = "fixture_id, team_id, possession, shots_total, shots_on, shots_off, shots_blocked, shots_inside, shots_outside, corners, offsides, fouls, yellow, red, saves, passes_total, passes_accurate, passes_pct, expected_goals, goals_prevented"
COLS_PROFILE = "id, name, code, country, founded, is_national, logo, league_id, league_name, league_country, continent"
COLS_VENUE = "id, name, city, provider_id"
COLS_PLAYER = "id, name, firstname, lastname, nationality"


def load_post_bundles(fixture_ids: Iterable[int]) -> dict[int, FullFixture]:
    ids = list(fixture_ids)
    if not ids:
        return {}
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute(f"SELECT {COLS_FIXTURE} FROM fixture WHERE id = ANY(%s)", (ids,))
        fixtures = [_fixture(r) for r in cur.fetchall()]
        if not fixtures:
            return {}

        cur.execute(
            f"SELECT {COLS_EVENT} FROM event WHERE fixture_id = ANY(%s) "
            "ORDER BY fixture_id, event_index",
            (ids,),
        )
        events = [_event(r) for r in cur.fetchall()]

        cur.execute(f"SELECT {COLS_SQUAD} FROM squadentry WHERE fixture_id = ANY(%s)", (ids,))
        squad = [_squad(r) for r in cur.fetchall()]

        cur.execute(f"SELECT {COLS_TSTAT} FROM teammatchstat WHERE fixture_id = ANY(%s)", (ids,))
        tstats = [_tstat(r) for r in cur.fetchall()]

        team_ids = list({f.home_team_id for f in fixtures} | {f.away_team_id for f in fixtures})
        cur.execute(f"SELECT {COLS_PROFILE} FROM teamprofile WHERE id = ANY(%s)", (team_ids,))
        profiles = {p.id: p for p in (_profile(r) for r in cur.fetchall())}

        venue_ids = [f.venue_id for f in fixtures if f.venue_id is not None]
        venues: dict[int, VenueRow] = {}
        if venue_ids:
            cur.execute(f"SELECT {COLS_VENUE} FROM venue WHERE id = ANY(%s)", (venue_ids,))
            venues = {v.id: v for v in (_venue(r) for r in cur.fetchall())}

        player_ids = list({s.player_id for s in squad} | {e.player_id for e in events if e.player_id is not None})
        players: dict[int, PlayerRow] = {}
        if player_ids:
            cur.execute(f"SELECT {COLS_PLAYER} FROM player WHERE id = ANY(%s)", (player_ids,))
            players = {p.id: p for p in (_player(r) for r in cur.fetchall())}

        # ESPN commentary lives in a separate store (ADR 0026/0027) joined via
        # narrated_match.fixture_id. Only key-moment categories reach the LLM.
        cur.execute(
            """
            SELECT nm.fixture_id, cl.sequence, cl.minute, cl.clock_seconds,
                   cl.team, cl.category, cl.source, cl.text
            FROM commentary_line cl
            JOIN narrated_match nm ON nm.game_id = cl.game_id
            WHERE nm.fixture_id = ANY(%s)
              AND cl.category = ANY(%s)
            ORDER BY nm.fixture_id, cl.sequence
            """,
            (ids, list(COMMENTARY_KEY_CATEGORIES)),
        )
        commentary_by_fx: dict[int, list[CommentaryLine]] = {}
        for row in cur.fetchall():
            fx_id, seq, minute, clock, team, category, source, text = row
            commentary_by_fx.setdefault(fx_id, []).append(
                CommentaryLine(
                    sequence=seq, minute=minute, clock_seconds=clock,
                    team=team, category=category, source=source, text=text,
                )
            )

    # In-memory join
    events_by_fx: dict[int, list[MatchEventRow]] = {}
    for e in events:
        events_by_fx.setdefault(e.fixture_id, []).append(e)
    squad_by_fx: dict[int, list[SquadEntryRow]] = {}
    for s in squad:
        squad_by_fx.setdefault(s.fixture_id, []).append(s)
    tstats_by_fx: dict[int, list[TeamMatchStatRow]] = {}
    for t in tstats:
        tstats_by_fx.setdefault(t.fixture_id, []).append(t)

    result: dict[int, FullFixture] = {}
    for f in fixtures:
        result[f.id] = FullFixture(
            fixture=f,
            events=events_by_fx.get(f.id, []),
            squad=squad_by_fx.get(f.id, []),
            team_stats=tstats_by_fx.get(f.id, []),
            home_profile=profiles.get(f.home_team_id),
            away_profile=profiles.get(f.away_team_id),
            venue=venues.get(f.venue_id) if f.venue_id else None,
            players={pid: players[pid] for pid in {s.player_id for s in squad_by_fx.get(f.id, [])} if pid in players},
            commentary=commentary_by_fx.get(f.id, []),
        )
    return result
