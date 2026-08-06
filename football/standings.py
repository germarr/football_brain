"""ADR 0025's standings and leader rules — the one definition (ADR 0040).

These rules are subtle in ways that do not announce themselves: only **Final** fixtures
score, points are 3/1/0, the result comes from the *on-pitch* goals with a penalty
shootout excluded (ADR 0012), and the ordering is Pts → GD → GF, which is deliberately
**unofficial** — real competitions use their own tiebreakers and ADR 0025 declined to
build a rules engine for them.

Until this module there was one implementation, in `serving.publish`, feeding the
Viewer. **Match Previews** (ADR 0040) need the same table computed from Postgres rather
than SQLite, and a second implementation would have let the Viewer show a team 4th on 31
points while the blog's card showed them 5th on 31 — nothing raising, both pages
rendering, discovered by a reader. That is the silent-divergence class ADR 0033 exists to
hunt, and it is the whole reason this file exists.

Like `football.status`, this is **kernel** in ADR 0031's sense — "what everything
imports". So it takes plain tuples and returns plain dicts, touches no store, and imports
nothing but `football.status`. `serving.publish` feeds it `sqlite3` rows and
`football_blog.preview` feeds it `psycopg` rows; neither can drift from the other because
there is nothing left to drift.
"""
from __future__ import annotations

from .status import FINAL

#: How many leaders `leaders_overall` returns per metric — ADR 0025's competition-wide
#: top-5, as rendered by the Viewer's league section.
LEADER_TOP_N = 5


def standings_rows(fixtures: list) -> list[dict]:
    """League table over `(home_id, home_name, away_id, away_name, home_goals,
    away_goals, status)` tuples, best first.

    Every fixture seeds its two teams **regardless of status**, so a not-yet-started
    season renders as an all-zeroes table listing everyone rather than as nothing at all
    (ADR 0025's second amendment). Only **Final** fixtures then score: 3/1/0, sorted
    Pts → GD → GF, name as the final deterministic tiebreak.

    The goals passed in must be the on-pitch ones. A `PEN` fixture is a **draw** here —
    its shootout lives in `penalty_home`/`penalty_away` and is not a scoreline (ADR 0012).
    """
    teams: dict[int, dict] = {}

    def ensure(tid: int, name: str) -> dict:
        return teams.setdefault(tid, {
            "team_id": tid, "name": name,
            "P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "Pts": 0,
        })

    for h, hn, a, an, hg, ag, status in fixtures:
        th, ta = ensure(h, hn), ensure(a, an)   # seed teams even for scheduled games
        if status not in FINAL or hg is None or ag is None:
            continue
        th["P"] += 1; ta["P"] += 1
        th["GF"] += hg; th["GA"] += ag; ta["GF"] += ag; ta["GA"] += hg
        if hg > ag:
            th["W"] += 1; th["Pts"] += 3; ta["L"] += 1
        elif ag > hg:
            ta["W"] += 1; ta["Pts"] += 3; th["L"] += 1
        else:
            th["D"] += 1; ta["D"] += 1; th["Pts"] += 1; ta["Pts"] += 1

    rows = list(teams.values())
    for t in rows:
        t["GD"] = t["GF"] - t["GA"]
    rows.sort(key=lambda t: (-t["Pts"], -t["GD"], -t["GF"], t["name"]))
    return rows


def leaders_overall(players: dict, metric: str, top_n: int = LEADER_TOP_N) -> list[tuple]:
    """The competition's top scorers or assisters — ADR 0025's rule, **grouped by player**.

    `players` maps `player_id -> {"goals": int, "assists": int, "teams": {team_id: apps}}`.
    A player who moves between two clubs *within the same competition* mid-season sums to
    one season total (that is what "top scorer" means), and the club shown is whichever he
    appeared for most. Returns `(player_id, value, primary_team_id)`, ties broken by
    player id so the order is stable across runs.

    Contrast `leaders_by_team`, which deliberately does **not** group this way.
    """
    ranked = sorted(
        ((pid, p[metric], max(p["teams"].items(), key=lambda kv: kv[1])[0])
         for pid, p in players.items() if p[metric] > 0),
        key=lambda x: (-x[1], x[0]),
    )
    return ranked[:top_n]


def leaders_by_team(entries: list, metric: str) -> dict[int, dict]:
    """Each Team's leading scorer or assister — the **Team Leaders** of a Match Preview.

    `entries` are `(team_id, player_id, player_name, goals, assists)` aggregates, already
    summed per (team, player) over the tournament in question.

    Grouped by **(team, player)**, not by player as `leaders_overall` is, and the
    difference is not an oversight. A competition's top scorer is a person's season total
    wherever they scored it; *this team's* top scorer is what they scored **for this
    team**. A January move between two clubs in the same league would otherwise credit
    the new club with goals scored against it.

    Returns `team_id -> {"player_id", "name", "value", "tied_with"}` for teams with a
    non-zero leader, and omits the rest — a team whose players have all scored zero has
    no leader, which is a fact about a three-game tournament and not a missing value.
    `tied_with` counts the *other* players level on that value, so a card can say "one of
    four on 1 goal" instead of implying a sole leader that does not exist.
    """
    idx = 3 if metric == "goals" else 4
    best: dict[int, dict] = {}
    tallies: dict[int, list[int]] = {}
    for team_id, player_id, player_name, goals, assists in entries:
        value = (goals, assists)[idx - 3] or 0
        if value <= 0:
            continue
        tallies.setdefault(team_id, []).append(value)
        cur = best.get(team_id)
        # Ties break on player id, matching `leaders_overall`, so both are stable.
        if cur is None or (value, -player_id) > (cur["value"], -cur["player_id"]):
            best[team_id] = {"player_id": player_id, "name": player_name, "value": value}
    for team_id, row in best.items():
        row["tied_with"] = tallies[team_id].count(row["value"]) - 1
    return best
