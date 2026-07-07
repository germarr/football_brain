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

from sqlmodel import Session, SQLModel, create_engine, select

from . import collect, config
from .client import CachedClient, QuotaExceeded
from .models import Competition, Fixture, Player, PlayerTeam, SquadEntry, Team, age_at

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

        for cid, cname in competitions.items():
            session.add(Competition(id=cid, name=cname))
        for tid, tname in teams.items():
            session.add(Team(id=tid, name=tname))

        for pid, season in sorted(player_season.items()):
            try:
                block = collect.fetch_player(client, pid, season)
            except QuotaExceeded:
                block = None  # bio not cached yet (backfill still in progress)
            if block is not None:
                session.add(_parse_player(block))
            else:  # no provider bio (missing or not-yet-cached) — minimal row
                session.add(Player(id=pid, name=player_names.get(pid, str(pid))))

        # Career Stints: full cross-competition team history per player.
        for pid in sorted(player_season):
            try:
                response = collect.fetch_player_teams(client, pid)
            except QuotaExceeded:
                continue  # career history not cached yet (backfill in progress)
            for row in _parse_player_teams(pid, response):
                session.add(row)

        session.commit()
        _summary(session)


def _summary(session: Session) -> None:
    n = {name: len(session.exec(select(model)).all())
         for name, model in [("competitions", Competition), ("teams", Team),
                             ("fixtures", Fixture), ("players", Player),
                             ("squad entries", SquadEntry),
                             ("career stints", PlayerTeam)]}
    entries = session.exec(select(SquadEntry)).all()
    appearances = sum((e.minutes or 0) > 0 for e in entries)
    print("Built {}: {}".format(
        config.DB_PATH.name,
        ", ".join(f"{v} {k}" for k, v in n.items())))
    print(f"  ({appearances} appearances, {len(entries) - appearances} unused subs)")

    # Fixture breakdown per (competition, season, tournament) — proves the split.
    fixtures = session.exec(select(Fixture)).all()
    breakdown: dict[tuple, int] = {}
    for f in fixtures:
        key = (f.league_name, f.season, f.tournament)
        breakdown[key] = breakdown.get(key, 0) + 1
    print("\nFixtures by competition / season / tournament:")
    for (comp, season, tourn), c in sorted(breakdown.items()):
        print(f"  {comp:10} {season}  {tourn:14} {c:4}")


if __name__ == "__main__":
    build()
