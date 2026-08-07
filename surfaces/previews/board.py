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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from football_blog import kalshi, polymarket, track
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
    football_computed_at: Optional[str] = None

    home_table: bool = False
    away_table: bool = False
    home_leaders: int = 0
    away_leaders: int = 0

    #: One per **Exchange**, never merged into a single set of percentages
    #: (ADR 0043). Each carries its own state, read time and volume unit, because
    #: either, both or neither Winner Market may exist for a Fixture.
    markets: dict[str, dict] = field(default_factory=dict)

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
        """Quoted on **at least one** Exchange.

        Not both: Polymarket does not cover every Competition Kalshi does, and the
        reverse will happen too. Requiring both would mark a card incomplete for a
        gap that is permanent and that nobody can close (ADR 0043).
        """
        return any(m.get("state") == "quoted" for m in self.markets.values())

    @property
    def exchanges(self) -> list[dict]:
        """Both Exchanges in a fixed order, present or not — Kalshi first, then Polymarket.

        Fixed so a card's two rows never swap places between renders: colour and
        position follow the Exchange, never its rank or whether it happens to be
        quoted this minute.
        """
        return [dict(self.markets.get(name) or {"state": "absent"}, exchange=name)
                for name in ("kalshi", "polymarket")]

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
            states = {m.get("state") for m in self.markets.values()}
            out.append("market prices" if "listed_unquoted" in states else "market")
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
            card.football_computed_at = rec.get("football_computed_at") or None
            home, away = rec.get("home") or {}, rec.get("away") or {}
            card.home_table = (home.get("table") or {}).get("state") == "present"
            card.away_table = (away.get("table") or {}).get("state") == "present"
            card.home_leaders = len(home.get("leaders") or [])
            card.away_leaders = len(away.get("leaders") or [])
            card.markets = _markets_of(rec)
        cards.append(card)
    return cards


#: Where each Exchange's half of the record lives. Named rather than derived so a
#: field rename fails at one line instead of silently reading `None` everywhere.
_MARKET_FIELDS = {
    "kalshi": ("market_kalshi", "quote_read_at_kalshi"),
    "polymarket": ("market_polymarket", "quote_read_at_polymarket"),
}


def _markets_of(rec: dict) -> dict[str, dict]:
    """Flatten one record's two Winner Markets into `{exchange: {…}}` for the card.

    Volume is read off the **home** outcome and kept with its unit. Kalshi counts
    contracts and Polymarket counts dollars, so the two numbers never share a scale
    and a card that renders them alike is lying by omission (CONTEXT.md).
    """
    out: dict[str, dict] = {}
    for exchange, (block_field, read_field) in _MARKET_FIELDS.items():
        block = rec.get(block_field) or {}
        if not block:
            continue
        row = {"state": block.get("state"), "reason": block.get("reason"),
               "read_at": rec.get(read_field) or None,
               "overround": block.get("overround"),
               "home": None, "draw": None, "away": None,
               "volume": None, "volume_unit": None}
        for o in block.get("outcomes") or []:
            side = o.get("side")
            if side in ("home", "draw", "away"):
                row[side] = o.get("market_probability")
            if side == "home":
                quote = o.get("quote") or {}
                row["volume"] = quote.get("volume")
                row["volume_unit"] = quote.get("volume_unit") or "contracts"
        out[exchange] = row
    return out


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


def unmapped_polymarket_teams() -> dict[str, Any]:
    """Polymarket Teams no registry entry names — the same gap, on the other Exchange.

    Kept as its own function rather than a parameterised one because the two Exchanges
    are refused for different reasons and the operator needs to see which: a Kalshi row
    is a club we have not mapped, a Polymarket row is a club we have not mapped *in that
    league*, since a Polymarket team id is per league (ADR 0043).
    """
    registry = polymarket.load_registry()
    client = polymarket.PolymarketClient()
    seen: dict[str, dict] = {}
    affected = 0
    try:
        for league in registry.league_keys:
            series_slug = registry.series_slug(league)
            if not series_slug:
                continue
            events = client.events(series_slug)
            _index, unmapped = polymarket.index_by_team_pair(events, league, registry)
            affected += len({u["event_slug"] for u in unmapped})
            for u in unmapped:
                key = polymarket.registry_key(u["league"], u["polymarket_team"])
                row = seen.setdefault(key, {"key": key,
                                            "polymarket_team": u["polymarket_team"],
                                            "polymarket_name": u["polymarket_name"],
                                            "league": u["league"], "events": 0})
                row["events"] += 1
    finally:
        client.close()
    teams = sorted(seen.values(), key=lambda r: (r["league"], r["polymarket_name"] or ""))
    return {"teams": teams, "markets_refused": affected}


# --------------------------------------------------------------------------- #
# Market Tracks — the two graphs                                               #
# --------------------------------------------------------------------------- #
def _fixture_row(fixture_id: int) -> Optional[tuple]:
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT f.id, f.league_id, f.date, f.home_team_id, f.away_team_id, "
            "       f.home_team_name, f.away_team_name "
            "FROM fixture f WHERE f.id = %s", (fixture_id,))
        return cur.fetchone()


def market_tracks(pb: PocketBaseClient, fixture_id: int) -> dict[str, Any]:
    """Both Exchanges' **Market Tracks** for one Fixture, on one shared axis (ADR 0043).

    Six GETs and nothing stored. This is deliberately a per-Fixture endpoint rather than
    something the card list carries: forty cards would be 240 requests, and a Track is
    what you ask for when you want to look at one match, not something every row pays for.

    Each Exchange is resolved independently and reports its own absence in its own words.
    **not_covered** is a Competition that Exchange does not list at all — permanent, and
    nothing a human can close; **not_listed** is a market it has not opened yet;
    **unmapped** is a club missing from that Exchange's registry, the one state anybody
    can act on.
    """
    row = _fixture_row(fixture_id)
    if row is None:
        return {"error": f"no fixture {fixture_id}"}
    fid, league_id, kickoff, home_id, away_id, home_name, away_name = row
    fid, league_id = int(fid), int(league_id)
    home_id, away_id = int(home_id), int(away_id)
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)

    publications = _publications(pb)
    display_tz = (publications.get(league_id) or {}).get("display_timezone") or "UTC"

    # Prefer the identifiers the record already holds. Resolution sweeps only *open*
    # markets, so a few hours after kickoff neither Exchange's sweep can see this
    # Fixture any more — a settled Match Preview would lose its graph exactly when the
    # graph becomes the only place its history survives.
    record = pb.list_previews_by_fixture_ids([fid]).get(fid) or {}
    k_stored = track.stored_market(record.get("market_kalshi"))
    p_stored = track.stored_market(record.get("market_polymarket"))

    k_track = (track.kalshi_track(k_stored) if k_stored
               else _kalshi_track(league_id, home_id, away_id, kickoff, display_tz))
    p_track = (track.polymarket_track(p_stored) if p_stored
               else _polymarket_track(league_id, home_id, away_id, kickoff))

    pair = track.TrackPair(fixture_id=fid, kickoff=int(kickoff.timestamp()),
                           kalshi=k_track, polymarket=p_track)
    return {**pair.as_dict(), "home_name": home_name, "away_name": away_name,
            "kickoff_iso": kickoff.isoformat()}


def _kalshi_track(league_id: int, home_id: int, away_id: int,
                  kickoff, display_tz: str) -> track.MarketTrack:
    registry = kalshi.load_registry()
    series = [s for s, cid in registry.series.items() if cid == league_id]
    if not series:
        return track.MarketTrack(exchange="kalshi", state="not_covered")
    client = kalshi.KalshiClient()
    try:
        for series_ticker in series:
            index, _unmapped = kalshi.index_by_team_pair(
                client.markets(series_ticker), series_ticker, registry)
            market = kalshi.attach(home_id, away_id, kickoff, display_tz, index)
            if market is not None:
                return track.kalshi_track(kalshi.label_sides(market, home_id, away_id))
    finally:
        client.close()
    return track.MarketTrack(exchange="kalshi", state="not_listed")


def _polymarket_track(league_id: int, home_id: int, away_id: int,
                      kickoff) -> track.MarketTrack:
    registry = polymarket.load_registry()
    if not registry.covers(league_id):
        return track.MarketTrack(exchange="polymarket", state="not_covered")
    leagues = [k for k in registry.league_keys
               if registry.competition_for(k) == league_id]
    client = polymarket.PolymarketClient()
    unmapped_here = False
    try:
        for league in leagues:
            series_slug = registry.series_slug(league)
            if not series_slug:
                continue
            index, unmapped = polymarket.index_by_team_pair(
                client.events(series_slug), league, registry)
            market = polymarket.attach(home_id, away_id, kickoff, index)
            if market is not None:
                return track.polymarket_track(
                    polymarket.label_sides(market, home_id, away_id))
            unmapped_here = unmapped_here or bool(unmapped)
    finally:
        client.close()
    # A refusal somewhere in this league is the likelier explanation than "not opened
    # yet", and it is the only one a human can do anything about — so say so.
    return track.MarketTrack(exchange="polymarket",
                             state="unmapped" if unmapped_here else "not_listed")
