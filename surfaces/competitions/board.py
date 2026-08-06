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
    flag: Optional[str]
    seasons: int                   # how many Seasons the Registry covers
    in_serve: bool                 # present in the serving store
    played: int                    # matches played, newest Season (serve.db league_meta)
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
    def has_play_count(self) -> bool:
        """Whether `played` means anything for this Competition.

        `league_meta` carries standings, and ADR 0025 gives a cup none — so `played` is
        structurally 0 for all 14 cups, including the World Cup with 360 Finals in the
        Published Store. Shown as "—" rather than "0", which would read as "no data".
        """
        return self.type == "league"

    @property
    def blocked_reason(self) -> Optional[str]:
        """Why the button would be pointless — only ever asked when it has work to do."""
        if self.next_action is None:
            return None
        if not self.in_serve:
            return "Not in the serving store — run Publish serve.db first."
        if self.has_play_count and not self.played:
            return "No matches played in the newest Season yet."
        return None


def _serve_rows() -> dict[int, dict]:
    """Competition + newest-Season play counts from the serving store."""
    if not SERVE_DB.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{SERVE_DB}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            comps = {r["id"]: dict(r) for r in con.execute(
                "select id, name, type, country, flag from competition")}
            for r in con.execute(
                "select league_id, max(season) as season, sum(played) as played "
                "from league_meta group by league_id"
            ):
                if r["league_id"] in comps:
                    comps[r["league_id"]]["played"] = r["played"] or 0
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
            flag=s.get("flag"),
            seasons=len(comp.get("seasons") or []),
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
