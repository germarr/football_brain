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
    Competition, Event, Fixture, Player, PlayerTeam, SquadEntry, Team, Venue, age_at,
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


_GROUP_LETTER = re.compile(r"^Group ([A-Z])(?: - (\d+))?$")
_SINGLE_TABLE = re.compile(r"^(?:Group Stage|League Stage)(?: - (\d+))?$")


def _parse_phase(round_str: str | None) -> tuple[str | None, str | None, str | None, int | None]:
    """Classify a cup fixture's round into (phase, group_label, stage, matchday).

    Grounded in the real provider vocabulary (ADR 0010):
      'Play-offs' / 'Preliminary Round' / 'Nth Qualifying Round' -> qualifying,
      'Group A - 3' -> group, label 'Group A', matchday 3,
      'Group Stage - 2' / 'League Stage - 5' -> group, no label (letterless),
      everything else ('Round of 16', 'Final', 'Knockout Round Play-offs', ...) ->
      knockout, stage = the round string.
    Qualifying is tested before knockout so bare 'Play-offs' (a qualifier) is never
    mistaken for the new CL's 'Knockout Round Play-offs' (a real knockout round).
    """
    if not round_str:
        return None, None, None, None
    r = round_str.strip()

    if r == "Preliminary Round" or r == "Play-offs" or "Qualifying Round" in r:
        return "qualifying", None, r, None

    m = _GROUP_LETTER.match(r)
    if m:
        matchday = int(m.group(2)) if m.group(2) else None
        return "group", f"Group {m.group(1)}", None, matchday

    m = _SINGLE_TABLE.match(r)
    if m:
        matchday = int(m.group(1)) if m.group(1) else None
        return "group", None, None, matchday

    return "knockout", None, r, None


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


def _venue_key(fx: dict) -> tuple[str, str | None] | None:
    """The (name, city) identity of a fixture's venue, or None if unnamed.

    The provider's venue.id is too sparse/inconsistent to key on (see Venue), so a
    ground is identified by its name plus city (which disambiguates same-named
    stadiums in different cities).
    """
    v = fx["fixture"].get("venue") or {}
    name = v.get("name")
    return (name, v.get("city")) if name else None


def _parse_fixture(fx: dict, venue_ids: dict[tuple[str, str | None], int]) -> Fixture:
    f, lg, tm, g = fx["fixture"], fx["league"], fx["teams"], fx["goals"]
    lid = lg["id"]
    league_name = config.COMPETITION_NAMES.get(lid, lg["name"])
    round_str = lg.get("round")
    # A cup is one Tournament per Season (its own name); its round encodes the Phase
    # (ADR 0010). A league keeps the old round-prefix tournament and no phase.
    if config.COMPETITION_TYPES.get(lid) == "cup":
        tournament = league_name
        phase, group_label, stage, matchday = _parse_phase(round_str)
    else:
        tournament, matchday = _parse_round(round_str)
        phase = group_label = stage = None
    return Fixture(
        id=f["id"],
        date=dt.datetime.fromisoformat(f["date"]),
        season=lg["season"],
        league_id=lid,
        league_name=league_name,
        tournament=tournament,
        phase=phase,
        group_label=group_label,
        stage=stage,
        matchday=matchday,
        round=round_str,
        status=f["status"]["short"],
        venue_id=venue_ids.get(_venue_key(fx)),  # None when the provider named no venue
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


def _build_venues(session: Session, client: CachedClient) -> dict[tuple[str, str | None], int]:
    """Sweep every target's fixtures for venues, insert the Venue rows, and return
    the (name, city) -> surrogate id map the main loop uses to set Fixture.venue_id.

    A cheap pre-pass (fixtures are local cache reads; no player fetches): the main
    build loop commits and expunges Fixtures per season to bound memory, so the
    surrogate ids must exist *before* those rows are parsed. provider_id is taken
    from the first fixture that exposes a non-null venue.id for the ground.
    """
    provider_ids: dict[tuple[str, str | None], int | None] = {}
    for league_id, _name, season in config.targets():
        for fx in collect.fetch_fixtures(client, league_id, season):
            key = _venue_key(fx)
            if key is None:
                continue
            provider_ids[key] = provider_ids.get(key) or (fx["fixture"].get("venue") or {}).get("id")

    # Enumerate sorted (name, city) pairs for a deterministic surrogate id (see Venue).
    venue_ids = {key: i for i, key in enumerate(sorted(provider_ids, key=lambda k: (k[0], k[1] or "")), 1)}
    for (name, city), sid in venue_ids.items():
        session.add(Venue(id=sid, name=name, city=city, provider_id=provider_ids[(name, city)]))
    session.commit()
    session.expunge_all()
    return venue_ids


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
        venue_ids = _build_venues(session, client)
        for league_id, _name, season in config.targets():
            for fx in collect.fetch_fixtures(client, league_id, season):
                session.add(_parse_fixture(fx, venue_ids))
                _lid = fx["league"]["id"]
                competitions[_lid] = config.COMPETITION_NAMES.get(_lid, fx["league"]["name"])
                for side in ("home", "away"):
                    t = fx["teams"][side]
                    teams[t["id"]] = t["name"]
                fid = fx["fixture"]["id"]
                try:
                    fp = collect.fetch_fixture_players(client, fid)
                except QuotaExceeded:
                    # No squad data cached for this fixture — a cup season without
                    # per-player stats coverage (ADR 0010), or a bio/stat backfill
                    # still in progress. The Fixture row stands; it just carries no
                    # SquadEntry, like the guarded bio/career/event fetches below.
                    continue
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
            session.add(Competition(id=cid, name=cname,
                                    type=config.COMPETITION_TYPES.get(cid, "league")))
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
         "venues": count(Venue), "fixtures": count(Fixture), "players": count(Player),
         "squad entries": count(SquadEntry), "career stints": count(PlayerTeam),
         "events": count(Event)}
    total_entries = n["squad entries"]
    appearances = count(SquadEntry, SquadEntry.minutes > 0)
    print("Built {}: {}".format(
        config.DB_PATH.name,
        ", ".join(f"{v} {k}" for k, v in n.items())))
    print(f"  ({appearances} appearances, {total_entries - appearances} unused subs)")
    located = count(Fixture, Fixture.venue_id.is_not(None))
    with_pid = count(Venue, Venue.provider_id.is_not(None))
    print(f"  ({located}/{n['fixtures']} fixtures located; {with_pid}/{n['venues']} venues carry a provider id)")

    # Fixture breakdown per (competition, season, tournament) — proves the split.
    print("\nFixtures by competition / season / tournament:")
    rows = session.exec(
        select(Fixture.league_name, Fixture.season, Fixture.tournament, func.count())
        .group_by(Fixture.league_name, Fixture.season, Fixture.tournament)
        .order_by(Fixture.league_name, Fixture.season, Fixture.tournament)
    ).all()
    for comp, season, tourn, c in rows:
        print(f"  {comp:10} {season}  {tourn:14} {c:4}")

    # Cup phase breakdown (ADR 0010) — proves group/knockout/qualifying tagging.
    cup_rows = session.exec(
        select(Fixture.league_name, Fixture.season, Fixture.phase,
               func.coalesce(Fixture.group_label, Fixture.stage), func.count())
        .where(Fixture.phase.is_not(None))
        .group_by(Fixture.league_name, Fixture.season, Fixture.phase,
                  func.coalesce(Fixture.group_label, Fixture.stage))
        .order_by(Fixture.league_name, Fixture.season, Fixture.phase,
                  func.coalesce(Fixture.group_label, Fixture.stage))
    ).all()
    if cup_rows:
        print("\nCup fixtures by competition / season / phase / group-or-stage:")
        for comp, season, phase, label, c in cup_rows:
            print(f"  {comp:18} {season}  {phase:10} {(label or '—'):22} {c:4}")

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
