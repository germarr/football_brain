"""Parse the raw cache (Layer 1) into SQLite (Layer 2). No network access.

Reads only from data/raw/ via a zero-budget client, so a cache miss fails loudly
instead of silently spending API quota. The DB is disposable: this drops and
rebuilds every table on each run. Covers all teams for every season in
config.SEASONS.

Run with:  uv run python -m football.parse
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import func
from sqlmodel import Session, SQLModel, create_engine, select

from . import collect, config
from .client import CachedClient, QuotaExceeded
from .models import (
    Competition, Event, Fixture, Player, PlayerTeam, SquadEntry, Team, age_at,
)

_NUM = re.compile(r"-?\d+")


def _parse_round(round_str: str | None) -> tuple[str, int | None]:
    """Split a provider round into (tournament, matchday).

    'Apertura - 5' -> ('Apertura', 5); 'Regular Season - 12' -> ('Regular Season', 12);
    'Apertura - Final' -> ('Apertura', None); missing -> ('Unknown', None).
    """
    if not round_str:
        return "Unknown", None
    prefix, sep, suffix = round_str.rpartition(" - ")
    if not sep:
        return round_str.strip(), None
    suffix = suffix.strip()
    return prefix.strip(), int(suffix) if suffix.isdigit() else None


def _to_int(value) -> int | None:
    """Pull the first integer out of ints, floats, or strings like '187 cm'; None-safe."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = _NUM.search(str(value))
    return int(m.group()) if m else None


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status(substitute: bool, minutes: int | None) -> str:
    if not substitute:
        return "started"
    return "came_on" if (minutes or 0) > 0 else "unused_sub"


def _parse_fixture(fx: dict) -> Fixture:
    f, lg, tm, g = fx["fixture"], fx["league"], fx["teams"], fx["goals"]
    tournament, matchday = _parse_round(lg.get("round"))
    return Fixture(
        id=f["id"],
        date=dt.datetime.fromisoformat(f["date"]),
        season=lg["season"],
        league_id=lg["id"],
        league_name=config.COMPETITION_NAMES.get(lg["id"], lg["name"]),
        tournament=tournament,
        matchday=matchday,
        round=lg.get("round"),
        status=f["status"]["short"],
        venue=(f.get("venue") or {}).get("name"),
        home_team_id=tm["home"]["id"],
        home_team_name=tm["home"]["name"],
        away_team_id=tm["away"]["id"],
        away_team_name=tm["away"]["name"],
        home_goals=g["home"],
        away_goals=g["away"],
    )


def _parse_player(block: dict) -> Player:
    p = block["player"]
    birth = p.get("birth") or {}
    bdate = birth.get("date")
    return Player(
        id=p["id"],
        name=p["name"],
        firstname=p.get("firstname"),
        lastname=p.get("lastname"),
        nationality=p.get("nationality"),
        birth_date=dt.date.fromisoformat(bdate) if bdate else None,
        birth_country=birth.get("country"),
        birth_place=birth.get("place"),
        height_cm=_to_int(p.get("height")),
        weight_kg=_to_int(p.get("weight")),
        photo=p.get("photo"),
    )


def _parse_player_teams(player_id: int, response: list[dict]) -> list[PlayerTeam]:
    """Flatten a players/teams response into one Career Stint row per season.

    Each entry is {team: {id, name, ...}, seasons: [year, ...]}; a team with no
    seasons (rare provider gap) yields no rows. Dedupes (team, season) pairs.
    """
    seen: set[tuple[int, int]] = set()
    rows: list[PlayerTeam] = []
    for entry in response:
        team = entry.get("team") or {}
        tid = team.get("id")
        if not tid:
            continue
        for season in entry.get("seasons") or []:
            key = (tid, season)
            if key in seen:
                continue
            seen.add(key)
            rows.append(PlayerTeam(
                player_id=player_id,
                team_id=tid,
                season=season,
                team_name=team.get("name") or str(tid),
            ))
    return rows


def _parse_squad_entry(fixture_id: int, team_id: int, pblock: dict) -> SquadEntry:
    s = pblock["statistics"][0]
    games, goals = s["games"], s["goals"]
    shots, passes = s["shots"], s["passes"]
    dribbles, fouls = s["dribbles"], s["fouls"]
    cards, pen = s["cards"], s["penalty"]
    minutes = _to_int(games.get("minutes"))
    return SquadEntry(
        fixture_id=fixture_id,
        player_id=pblock["player"]["id"],
        team_id=team_id,
        status=_status(bool(games.get("substitute")), minutes),
        minutes=minutes,
        position=games.get("position"),
        rating=_to_float(games.get("rating")),
        captain=bool(games.get("captain")),
        goals=_to_int(goals.get("total")) or 0,
        assists=_to_int(goals.get("assists")) or 0,
        shots_total=_to_int(shots.get("total")),
        shots_on=_to_int(shots.get("on")),
        passes_total=_to_int(passes.get("total")),
        passes_key=_to_int(passes.get("key")),
        tackles_total=_to_int(s["tackles"].get("total")),
        interceptions=_to_int(s["tackles"].get("interceptions")),
        duels_total=_to_int(s["duels"].get("total")),
        duels_won=_to_int(s["duels"].get("won")),
        dribbles_attempts=_to_int(dribbles.get("attempts")),
        dribbles_success=_to_int(dribbles.get("success")),
        fouls_drawn=_to_int(fouls.get("drawn")),
        fouls_committed=_to_int(fouls.get("committed")),
        yellow=_to_int(cards.get("yellow")) or 0,
        red=_to_int(cards.get("red")) or 0,
        penalty_scored=_to_int(pen.get("scored")) or 0,
        penalty_missed=_to_int(pen.get("missed")) or 0,
    )


def _parse_event(fixture_id: int, index: int, ev: dict) -> Event:
    """One fixtures/events entry -> an Event row (ADR 0007).

    player_id/assist_id are taken raw here; build() nulls any that aren't in the
    Player table (coach cards, off-scope actors) once the full player set is known.
    """
    time = ev.get("time") or {}
    team = ev.get("team") or {}
    player = ev.get("player") or {}
    assist = ev.get("assist") or {}
    return Event(
        fixture_id=fixture_id,
        event_index=index,
        team_id=team.get("id"),
        minute=_to_int(time.get("elapsed")),
        extra=_to_int(time.get("extra")),
        type=ev.get("type") or "Unknown",
        detail=ev.get("detail"),
        player_id=player.get("id"),
        assist_id=assist.get("id"),
        comments=ev.get("comments"),
    )


def build() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{config.DB_PATH}")
    SQLModel.metadata.drop_all(engine)   # disposable store (ADR 0002)
    SQLModel.metadata.create_all(engine)

    client = CachedClient(max_live_requests=0)  # cache-only: a miss raises, never fetches
    teams: dict[int, str] = {}
    competitions: dict[int, str] = {}
    player_season: dict[int, int] = {}
    player_names: dict[int, str] = {}   # fallback name for players lacking a bio

    # The full store is ~1M squad entries + ~200k career stints + ~400k events.
    # Holding all of that in one session's identity map exhausts memory (OOM at
    # full scale), so every phase commits and expunges in batches — the session
    # never accumulates more than one batch of rows at a time. Only the small
    # Python dicts above (a few ints/strs per player/team) live across phases.
    with Session(engine) as session:
        for league_id, _name, season in config.targets():
            for fx in collect.fetch_fixtures(client, league_id, season):
                session.add(_parse_fixture(fx))
                _lid = fx["league"]["id"]
                competitions[_lid] = config.COMPETITION_NAMES.get(_lid, fx["league"]["name"])
                for side in ("home", "away"):
                    t = fx["teams"][side]
                    teams[t["id"]] = t["name"]
                fid = fx["fixture"]["id"]
                fp = collect.fetch_fixture_players(client, fid)
                seen_pids: set[int] = set()  # provider lists a few players twice per fixture
                for team_id, pblock in collect.players_in_fixture(fp):
                    pid = pblock["player"]["id"]
                    if not pid:  # unknown player (id 0/None) — no bio, no valid FK
                        continue
                    if pid in seen_pids:  # duplicate block for the same player — keep the first
                        continue
                    seen_pids.add(pid)
                    session.add(_parse_squad_entry(fid, team_id, pblock))
                    player_season.setdefault(pid, season)
                    player_names[pid] = pblock["player"]["name"]
            session.commit()          # flush this season's fixtures + squad entries
            session.expunge_all()     # and drop them from memory

        for cid, cname in competitions.items():
            session.add(Competition(id=cid, name=cname))
        for tid, tname in teams.items():
            session.add(Team(id=tid, name=tname))
        session.commit()
        session.expunge_all()

        for i, (pid, season) in enumerate(sorted(player_season.items()), 1):
            try:
                block = collect.fetch_player(client, pid, season)
            except QuotaExceeded:
                block = None  # bio not cached yet (backfill still in progress)
            if block is not None:
                session.add(_parse_player(block))
            else:  # no provider bio (missing or not-yet-cached) — minimal row
                session.add(Player(id=pid, name=player_names.get(pid, str(pid))))
            if i % 5000 == 0:
                session.commit()
                session.expunge_all()
        session.commit()
        session.expunge_all()

        # Career Stints: full cross-competition team history per player.
        for i, pid in enumerate(sorted(player_season), 1):
            try:
                response = collect.fetch_player_teams(client, pid)
            except QuotaExceeded:
                continue  # career history not cached yet (backfill in progress)
            for row in _parse_player_teams(pid, response):
                session.add(row)
            if i % 2000 == 0:
                session.commit()
                session.expunge_all()
        session.commit()
        session.expunge_all()

        _build_events(session, client, set(player_season))
        _summary(session)


def _build_events(session: Session, client: CachedClient, known_players: set[int]) -> None:
    """Second pass over the fixtures (from cache) to insert the event timeline.

    Runs after players exist so the FK guard can resolve, and re-reads events from
    the cache rather than hoarding ~400k rows from the first pass. Nulls any
    player/assist id without a Player row (coach cards, off-scope actors) and skips
    the rare event with no team; commits in batches to bound memory (ADR 0007).
    """
    batch: list[Event] = []
    for league_id, _name, season in config.targets():
        for fx in collect.fetch_fixtures(client, league_id, season):
            fid = fx["fixture"]["id"]
            try:
                raw_events = collect.fetch_fixture_events(client, fid)
            except QuotaExceeded:
                continue  # this fixture's events aren't backfilled yet
            for idx, ev in enumerate(raw_events):
                e = _parse_event(fid, idx, ev)
                if e.team_id is None:
                    continue
                if e.player_id not in known_players:
                    e.player_id = None
                if e.assist_id not in known_players:
                    e.assist_id = None
                batch.append(e)
            if len(batch) >= 10000:
                session.add_all(batch)
                session.commit()
                session.expunge_all()
                batch = []
    if batch:
        session.add_all(batch)
        session.commit()
        session.expunge_all()


def _summary(session: Session) -> None:
    # Aggregate in SQL — the tables are far too large to pull into memory (ADR 0002).
    def count(model, *where):
        stmt = select(func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return session.exec(stmt).one()

    n = {"competitions": count(Competition), "teams": count(Team),
         "fixtures": count(Fixture), "players": count(Player),
         "squad entries": count(SquadEntry), "career stints": count(PlayerTeam),
         "events": count(Event)}
    total_entries = n["squad entries"]
    appearances = count(SquadEntry, SquadEntry.minutes > 0)
    print("Built {}: {}".format(
        config.DB_PATH.name,
        ", ".join(f"{v} {k}" for k, v in n.items())))
    print(f"  ({appearances} appearances, {total_entries - appearances} unused subs)")

    # Fixture breakdown per (competition, season, tournament) — proves the split.
    print("\nFixtures by competition / season / tournament:")
    rows = session.exec(
        select(Fixture.league_name, Fixture.season, Fixture.tournament, func.count())
        .group_by(Fixture.league_name, Fixture.season, Fixture.tournament)
        .order_by(Fixture.league_name, Fixture.season, Fixture.tournament)
    ).all()
    for comp, season, tourn, c in rows:
        print(f"  {comp:10} {season}  {tourn:14} {c:4}")

    # Events breakdown (ADR 0007) — only meaningful once events are backfilled.
    if n["events"]:
        fixtures_with_events = session.exec(
            select(func.count(func.distinct(Event.fixture_id)))
        ).one()
        print(f"\nEvents across {fixtures_with_events} fixtures:")
        for t, c in session.exec(
            select(Event.type, func.count()).group_by(Event.type).order_by(func.count().desc())
        ).all():
            print(f"  {t:8} {c:6}")
        print("Goals by type:")
        for d, c in session.exec(
            select(Event.detail, func.count()).where(Event.type == "Goal")
            .group_by(Event.detail).order_by(func.count().desc())
        ).all():
            print(f"  {(d or '?'):14} {c:5}")
        stoppage_goals = count(Event, Event.type == "Goal", Event.extra.is_not(None))
        print(f"  ({stoppage_goals} goals in added time)")


if __name__ == "__main__":
    build()
