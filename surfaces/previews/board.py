"""What the `/previews` board reads: the 7-day card list, and what each card is missing.

Two questions, answered from different places, and keeping them apart is the point:

  **"Which Fixtures should have a Match Preview?"** — the Published Store. It is the
  authority on what is scheduled, and asking PocketBase instead would only ever tell you
  what the builder already managed to write. A Fixture the builder skipped or has not
  reached yet has to show as a *gap*, not vanish.

  **"What does each one actually carry?"** — the Editorial Store. Table, Team Leaders,
  Winner Market: present or not, per card.

The third thing the board shows is the one that needs a human: **unmapped Kalshi Teams**.
That list is deliberately not a list of unmatched games. A Winner Market attaches by Team
(ADR 0041), so the gap is a Team we have never mapped — which shrinks to zero and stays
there — rather than ~40 games a week that would never stop arriving.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from football_blog import kalshi
from football_blog.preview import SCHEDULED, WINDOW_DAYS
from football_blog.pocketbase import PocketBaseClient
from football_blog.postgres import get_conn


@dataclass
class PreviewCard:
    """One upcoming Fixture, and how complete its Match Preview is."""
    fixture_id: int
    kickoff: datetime
    local_date: Optional[str]
    league_id: int
    league_name: str
    home_name: str
    away_name: str
    home_logo: Optional[str] = None
    away_logo: Optional[str] = None

    publication_slug: str = ""
    brand_color: str = ""

    #: None when the builder has not written this Fixture yet — a gap, not an absence.
    record_id: Optional[str] = None
    lifecycle: Optional[str] = None
    market_state: Optional[str] = None
    market_reason: Optional[str] = None
    football_computed_at: Optional[str] = None
    quote_read_at: Optional[str] = None

    home_table: bool = False
    away_table: bool = False
    home_leaders: int = 0
    away_leaders: int = 0
    home_probability: Optional[float] = None
    draw_probability: Optional[float] = None
    away_probability: Optional[float] = None
    market_volume: Optional[float] = None

    @property
    def present(self) -> bool:
        return self.record_id is not None

    @property
    def has_table(self) -> bool:
        return self.home_table and self.away_table

    @property
    def has_leaders(self) -> bool:
        return self.home_leaders > 0 and self.away_leaders > 0

    @property
    def has_market(self) -> bool:
        return self.market_state == "quoted"

    @property
    def complete(self) -> bool:
        return self.present and self.has_table and self.has_leaders and self.has_market

    @property
    def missing(self) -> list[str]:
        """What this card lacks, in the words the glossary uses.

        A knockout Fixture's absent table is reported the same way as any other gap and
        that is deliberate — the board says what is *not on the card*, and why it is not
        there is the ADR's job, not a badge's.
        """
        out = []
        if not self.present:
            return ["not built"]
        if not self.has_table:
            out.append("table")
        if not self.has_leaders:
            out.append("leaders")
        if not self.has_market:
            out.append({"absent": "market", "listed_unquoted": "market prices"}.get(
                self.market_state or "", "market"))
        return out


def _publications(pb: PocketBaseClient) -> dict[int, dict]:
    return {int(p["postgres_competition_id"]): p
            for p in pb.list_publications(only_published=True)}


def list_cards(pb: PocketBaseClient, *, days: int = WINDOW_DAYS,
               now: Optional[datetime] = None) -> list[PreviewCard]:
    now = now or datetime.now(timezone.utc)
    publications = _publications(pb)
    if not publications:
        return []

    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT f.id, f.league_id, f.date, f.home_team_id, f.away_team_id, "
            "       f.home_team_name, f.away_team_name, c.name, "
            "       hp.logo, ap.logo "
            "FROM fixture f "
            "JOIN competition c ON c.id = f.league_id "
            "LEFT JOIN teamprofile hp ON hp.id = f.home_team_id "
            "LEFT JOIN teamprofile ap ON ap.id = f.away_team_id "
            "WHERE f.league_id = ANY(%s) AND f.status = ANY(%s) "
            "  AND f.date >= %s AND f.date < %s "
            "ORDER BY f.date",
            (sorted(publications), list(SCHEDULED),
             now.replace(tzinfo=None), (now + timedelta(days=days)).replace(tzinfo=None)))
        rows = cur.fetchall()

    if not rows:
        return []

    records = pb.list_previews_by_fixture_ids([int(r[0]) for r in rows])

    cards: list[PreviewCard] = []
    for (fid, lid, date, _hid, _aid, hn, an, cname, hlogo, alogo) in rows:
        fid, lid = int(fid), int(lid)
        pub = publications[lid]
        card = PreviewCard(
            fixture_id=fid,
            kickoff=date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date,
            local_date=None,
            league_id=lid, league_name=cname,
            home_name=hn, away_name=an, home_logo=hlogo, away_logo=alogo,
            publication_slug=pub["slug"], brand_color=pub.get("brand_color") or "",
        )
        rec = records.get(fid)
        if rec:
            card.record_id = rec["id"]
            card.local_date = rec.get("local_date")
            card.lifecycle = rec.get("lifecycle")
            card.market_state = rec.get("market_state")
            card.football_computed_at = rec.get("football_computed_at") or None
            card.quote_read_at = rec.get("quote_read_at") or None
            home, away = rec.get("home") or {}, rec.get("away") or {}
            card.home_table = (home.get("table") or {}).get("state") == "present"
            card.away_table = (away.get("table") or {}).get("state") == "present"
            card.home_leaders = len(home.get("leaders") or [])
            card.away_leaders = len(away.get("leaders") or [])
            market = rec.get("market") or {}
            card.market_reason = market.get("reason")
            for o in market.get("outcomes") or []:
                if o.get("side") == "home":
                    card.home_probability = o.get("market_probability")
                    card.market_volume = (o.get("quote") or {}).get("volume")
                elif o.get("side") == "draw":
                    card.draw_probability = o.get("market_probability")
                elif o.get("side") == "away":
                    card.away_probability = o.get("market_probability")
        cards.append(card)
    return cards


def summary(cards: list[PreviewCard]) -> dict[str, int]:
    return {
        "fixtures": len(cards),
        "built": sum(1 for c in cards if c.present),
        "complete": sum(1 for c in cards if c.complete),
        "no_market": sum(1 for c in cards if c.present and not c.has_market),
        "not_built": sum(1 for c in cards if not c.present),
    }


def unmapped_teams() -> dict[str, Any]:
    """Kalshi Teams no registry entry names — the board's one actionable gap.

    Hits Kalshi (three GETs), so it is served from its own endpoint rather than blocking
    the page: the cards render from two local stores and this arrives after.

    Every entry here is a **refusal**, not a failure. A Winner Market whose two clubs do
    not both resolve is dropped whole rather than half-attached, because inferring the
    second club from the first is exactly the guess ADR 0041 refuses to make.
    """
    registry = kalshi.load_registry()
    client = kalshi.KalshiClient()
    seen: dict[str, dict] = {}
    affected = 0
    try:
        for series in registry.series_tickers:
            markets = client.markets(series)
            _index, unmapped = kalshi.index_by_team_pair(markets, series, registry)
            affected += len({u["event_ticker"] for u in unmapped})
            for u in unmapped:
                key = u["kalshi_team"]
                row = seen.setdefault(key, {"kalshi_team": key,
                                            "kalshi_name": u["kalshi_name"],
                                            "series": set(), "events": 0})
                row["series"].add(u["series_ticker"])
                row["events"] += 1
    finally:
        client.close()
    teams = [{**row, "series": sorted(row["series"])} for row in seen.values()]
    teams.sort(key=lambda r: (r["kalshi_name"] or ""))
    return {"teams": teams, "markets_refused": affected}
