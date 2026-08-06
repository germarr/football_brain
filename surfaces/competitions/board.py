"""What stage each Competition has reached on its way to being **Draftable** (ADR 0037).

Reads only, and from three stores, because the answer genuinely lives in three places:

  - the **Registry** (`competitions.json`) — what we collect at all, and the board's
    population;
  - `serving/serve.db` — how much of it we actually hold. This is deliberately *not*
    Postgres: all eight checks in `football_blog/onboard.py` query the Published Store,
    which by definition holds none of the 42 Competitions a card is offering to onboard.
    ADR 0023 makes serve.db the store a UI may read;
  - the **Published Store** and the **Editorial Store** — the last two stages.

Nothing here writes, spawns, or spends quota. The one button on this page does all of
that, as a subprocess of one command (ADR 0021).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from football import config

SERVE_DB = config.ROOT / "serving" / "serve.db"

#: The ordered stages a Competition passes through. The card shows the furthest reached
#: and the button advances to the next — which is what makes one button label honest.
#: `live` is listed so the bar is complete, and is the one stage the button never
#: reaches: flipping a Publication's `published` gate is a human act (CONTEXT.md).
STAGES = ["collected", "published_store", "publication", "draftable", "live"]

STAGE_LABEL = {
    "collected": "Collected",
    "published_store": "In the Published Store",
    "publication": "Has a Publication",
    "draftable": "Draftable",
    "live": "Live",
}


@dataclass
class CompetitionCard:
    league_id: int
    name: str
    type: str                      # league | cup
    country: Optional[str]
    continent: Optional[str]
    flag: Optional[str]            # null for the six international Competitions
    logo: Optional[str]            # populated for all 45 — the reliable identifier
    seasons: int                   # how many Seasons the Registry covers
    season_label: Optional[str]    # newest Season, e.g. "2025/26"
    team_count: int
    stats_light: bool              # Coverage-light: timeline-only reports (ADR 0014)
    in_serve: bool                 # present in the serving store
    played: int                    # matches played, newest Season
    in_store: bool                 # rows in the Published Store
    finals_in_store: int
    publication: Optional[dict]    # the Publication record, if any

    @property
    def stage(self) -> str:
        """The furthest stage reached — deliberately *not* a boolean.

        `draftable` requires Finals actually present, not merely a Publication beside a
        Competition row. That combination is the failure a single 'ready?' bit renders as
        success: the card would read ready while the Desk shows an empty board (ADR 0037).
        """
        if not self.publication:
            return "published_store" if self.in_store else "collected"
        if not (self.in_store and self.finals_in_store):
            return "publication"
        return "live" if self.publication.get("published") else "draftable"

    @property
    def stage_index(self) -> int:
        return STAGES.index(self.stage)

    @property
    def is_draftable(self) -> bool:
        return self.stage in ("draftable", "live")

    @property
    def next_action(self) -> Optional[str]:
        """What the button would do, or None when there is nothing for it to do.

        Returns None at `live` and at `draftable`: the remaining step from Draftable is
        flipping the public gate, and this page does not offer it (ADR 0034/0037).
        """
        if self.stage in ("draftable", "live"):
            return None
        if self.stage == "publication":
            return "Publish to the store"
        return "Onboard to the blog"

    @property
    def is_international(self) -> bool:
        """No national flag because there is no nation — the six `country == "World"`
        Competitions. Detected by the absent flag rather than by string-matching the
        country, since the absence is exactly the fact being reported."""
        return not self.flag

    @property
    def coverage_note(self) -> Optional[str]:
        """Why a Narrative from this Competition would read thin (ADR 0014/0034).

        Editorially decisive and invisible everywhere else: 13 of the 31 leagues here are
        Coverage-light, meaning no per-player or team stats, so the drafter emits
        "(no per-player stats)" and writes a timeline-only report. Worth knowing *before*
        onboarding, and never a blocker — a timeline-only report is still worth writing.
        """
        return "Coverage-light — timeline-only reports" if self.stats_light else None

    @property
    def has_play_count(self) -> bool:
        """Whether `played` means anything for this Competition.

        `league_meta` carries standings, and ADR 0025 gives a cup none — so `played` is
        structurally 0 for all 14 cups, including the World Cup with 360 Finals in the
        Published Store. Shown as "—" rather than "0", which would read as "no data".
        """
        return self.type == "league"

    @property
    def season_note(self) -> Optional[str]:
        """Context, deliberately not a blocker.

        An earlier version refused to onboard a Competition whose newest Season had no
        matches played, which in early August blocked La Liga, the Premier League, the
        Bundesliga and Ligue 1 — eleven leagues with twelve Seasons of history each,
        between seasons. Whether there is anything recent to write about is a 90-day
        question that `football_blog.onboard` already asks against the Published Store.
        The card says what it knows; the command decides.
        """
        if self.has_play_count and not self.played and self.seasons > 1:
            return "Between Seasons — newest has not kicked off"
        return None

    @property
    def blocked_reason(self) -> Optional[str]:
        """The one thing that genuinely makes the button pointless: we hold nothing."""
        if self.next_action is None:
            return None
        if not self.in_serve:
            return "Not in the serving store — run Publish serve.db first."
        return None


def _serve_rows() -> dict[int, dict]:
    """Competition identity + newest-Season shape, from the serving store.

    `played` is the **newest Season only**, summed across its Tournaments so a split
    season (Apertura + Clausura) counts once. An earlier version summed every Season,
    which made a card read "3,042 played" for a league 205 matches into its current one —
    a lifetime total wearing a current-season label.
    """
    if not SERVE_DB.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{SERVE_DB}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            comps = {r["id"]: dict(r) for r in con.execute(
                "select id, name, type, country, country_code, continent, flag, logo "
                "from competition")}
            for r in con.execute(
                """
                select lm.league_id,
                       max(lm.season_label)  as season_label,
                       max(lm.team_count)    as team_count,
                       max(lm.stats_light)   as stats_light,
                       sum(lm.played)        as played
                from league_meta lm
                where lm.season = (select max(season) from league_meta x
                                   where x.league_id = lm.league_id)
                group by lm.league_id
                """
            ):
                c = comps.get(r["league_id"])
                if c is not None:
                    c.update({
                        "season_label": r["season_label"],
                        "team_count": r["team_count"] or 0,
                        # max(): if either Tournament of a split Season is Coverage-light,
                        # the Season is — a thin half still yields thin reports.
                        "stats_light": bool(r["stats_light"]),
                        "played": r["played"] or 0,
                    })
            return comps
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _store_rows() -> tuple[set[int], dict[int, int]]:
    """(competition ids in the Published Store, Finals per competition).

    Failure is degradation, not an error: an unreachable Postgres leaves every card at
    its serve.db stage rather than blanking the board.
    """
    from football_blog import FINAL_STATUSES
    try:
        from football_blog.postgres import get_conn
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("select id from competition")
            ids = {r[0] for r in cur.fetchall()}
            cur.execute(
                "select league_id, count(*) from fixture where status = ANY(%s) "
                "group by league_id", (list(FINAL_STATUSES),))
            finals = {r[0]: r[1] for r in cur.fetchall()}
        return ids, finals
    except Exception:                                # noqa: BLE001 — degrade, don't blank
        return set(), {}


def _publications() -> dict[int, dict]:
    try:
        from football_blog.pocketbase import PocketBaseClient
        pb = PocketBaseClient()
        try:
            return {int(p["postgres_competition_id"]): p
                    for p in pb.list_publications(only_published=False)}
        finally:
            pb.close()
    except Exception:                                # noqa: BLE001
        return {}


def list_cards() -> list[CompetitionCard]:
    """Every Registry Competition as a card, ordered draftable-first then by name.

    The Registry is the population rather than serve.db, so a Competition we collect but
    have not published to the serving store still gets a card — with `blocked_reason`
    saying so, instead of silently missing from a page whose whole job is telling you
    what exists.
    """
    serve = _serve_rows()
    store_ids, finals = _store_rows()
    pubs = _publications()

    cards = []
    for comp in config.COMPETITIONS:
        lid = comp["league_id"]
        s = serve.get(lid, {})
        cards.append(CompetitionCard(
            league_id=lid,
            name=s.get("name") or comp.get("name") or str(lid),
            type=(s.get("type") or comp.get("type") or "league"),
            country=s.get("country"),
            continent=s.get("continent"),
            flag=s.get("flag"),
            logo=s.get("logo"),
            seasons=len(comp.get("seasons") or []),
            season_label=s.get("season_label"),
            team_count=int(s.get("team_count") or 0),
            stats_light=bool(s.get("stats_light")),
            in_serve=lid in serve,
            played=int(s.get("played") or 0),
            in_store=lid in store_ids,
            finals_in_store=int(finals.get(lid, 0)),
            publication=pubs.get(lid),
        ))
    cards.sort(key=lambda c: (-c.stage_index, c.name))
    return cards


def summary(cards: list[CompetitionCard]) -> dict:
    counts = {s: 0 for s in STAGES}
    for c in cards:
        counts[c.stage] += 1
    return {"total": len(cards), "counts": counts,
            "draftable": sum(1 for c in cards if c.is_draftable)}
