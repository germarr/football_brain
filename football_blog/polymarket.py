"""Polymarket **Winner Markets**, the second **Exchange** (ADR 0043).

A Winner Market is the three mutually exclusive contracts an Exchange lists on one
Fixture's result — home, away and the draw. Polymarket calls its three `moneyline` and
links them (`negRisk: true`), and resolves them on *"the outcome within the first 90
minutes of regular play plus stoppage time"* — Kalshi's rule word for word. That shared
settlement is the whole reason the two are comparable, and it is the only property they
share exactly.

Four things here differ from `football_blog.kalshi` and each one is load-bearing:

**A league is enumerated by its series slug, never by its name.** Leagues Cup's *sport*
slug is `lec` but its *tag* slug is `lcs` — which collides with the esports LCS — and
`?sport=lec` returns a hundred events and **zero** moneylines without erroring. Neither
`tag_slug=lec` nor `public-search?q=Leagues Cup` finds the structured games; the latter
returns only hand-made 2025 events with no teams, no `startTime`, and sometimes no draw
leg. Only `series_slug=soccer-lec` enumerates them. A league whose series slug has not
been verified is not in the registry.

**A team id is per league, not per club.** CF Monterrey is `115320` in `mex` and
`3268366` in `lec`; Columbus Crew is `115057` in `mls` and `3268353` in `lec`. Kalshi's
UUID is one per club across every series — Polymarket's is not, so the registry key is
`(league, team id)` and a club playing its domestic league and the Leagues Cup holds two
rows.

**The anchor is the exact kickoff instant, and it does not decide anything alone.**
`startTime` matches our `fixture.date` to the minute, while the *slug* is dated too and
lies: `mls-ner-hou-2026-03-07` is an `08-08 20:30Z` kickoff, because a Polymarket event
survives a postponement by design and keeps its birth date in the slug. But six pairs of
Leagues Cup Fixtures kick off at the same instant in a single week, so the instant
**narrows** and the registry-resolved team pair **decides** — exactly as the local date
does for Kalshi (ADR 0041).

**Volume is dollars traded, not contracts.** It must never share an axis with Kalshi's,
which is why every Quote here carries its unit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from football import teamnames

REGISTRY_FILE = Path(__file__).resolve().parent / "registry" / "polymarket_teams.json"

GAMMA_URL = "https://gamma-api.polymarket.com"

#: Polymarket's own word for a Winner Market leg. The same event also carries `spreads`,
#: `totals`, `both_teams_to_score`, `soccer_halftime_result` and a dozen more, so this
#: filter is what makes "the Winner Market" name one thing (CONTEXT.md).
MONEYLINE = "moneyline"


# --------------------------------------------------------------------------- #
# The registry                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Registry:
    """The committed `(league, Polymarket team id) -> our team id` map.

    A **Registry** in CONTEXT.md's sense: input, never output; decided rather than
    fetched; no rebuild path from the raw cache; every change reviewed as a diff.

    `leagues` maps Polymarket's league key (`mls`, `mex`, `lec` — the value on both
    `event.sport.sport` and `team.league`) to our Competition id and to the **series
    slug** that enumerates it. Both are needed and they are not the same string:
    `lec`'s series slug is `soccer-lec`.
    """
    leagues: dict[str, dict]                  # league key -> {competition_id, series_slug}
    teams: dict[tuple[str, int], int]         # (league key, pm team id) -> our team id
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def competition_for(self, league: str) -> Optional[int]:
        entry = self.leagues.get(league)
        return int(entry["competition_id"]) if entry else None

    def series_slug(self, league: str) -> Optional[str]:
        entry = self.leagues.get(league)
        return entry.get("series_slug") if entry else None

    def team_id(self, league: str, pm_team_id: int) -> Optional[int]:
        return self.teams.get((league, int(pm_team_id)))

    @property
    def league_keys(self) -> list[str]:
        return sorted(self.leagues)

    @property
    def competition_ids(self) -> list[int]:
        return sorted({int(v["competition_id"]) for v in self.leagues.values()})

    def covers(self, competition_id: int) -> bool:
        """Does Polymarket list this Competition at all?

        **Not covered** is a distinct state from *not listed yet* and from *unmapped*
        (ADR 0043): the first is permanent and needs no human, the others do.
        """
        return int(competition_id) in set(self.competition_ids)


def load_registry(path: Path = REGISTRY_FILE) -> Registry:
    if not path.exists():
        raise FileNotFoundError(
            f"No Polymarket team registry at {path}. Nothing is attached for a Team no "
            f"Registry names (CONTEXT.md) — bootstrap it with:\n"
            f"    uv run python -m football_blog.polymarket --propose")
    raw = json.loads(path.read_text())
    teams: dict[tuple[str, int], int] = {}
    for key, entry in (raw.get("teams") or {}).items():
        if entry.get("team_id") is None:
            continue
        league, _, pm_id = key.partition(":")
        teams[(league, int(pm_id))] = int(entry["team_id"])
    return Registry(leagues=dict(raw.get("leagues") or {}), teams=teams, raw=raw)


def registry_key(league: str, pm_team_id: Any) -> str:
    """`("lec", 3268357) -> "lec:3268357"` — the key a registry row is filed under."""
    return f"{league}:{int(pm_team_id)}"


# --------------------------------------------------------------------------- #
# The client                                                                   #
# --------------------------------------------------------------------------- #
class PolymarketClient:
    """Read-only, unauthenticated. Gamma for discovery; the CLOB is `track.py`'s.

    No key, no wallet, no signing. `httpx` matters here rather than being incidental:
    the CLOB host sits behind Cloudflare and 403s (`error code: 1010`) a
    `Python-urllib` user agent, while httpx's default passes untouched.
    """

    def __init__(self, base_url: str = GAMMA_URL, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def events(self, series_slug: str, closed: bool = False,
               page: int = 100, max_pages: int = 20) -> list[dict[str, Any]]:
        """Every open event in a league's series, paginated by offset.

        Keyed on `series_slug` and nothing else — see the module docstring for what the
        plausible alternatives silently return instead.
        """
        out: list[dict[str, Any]] = []
        for i in range(max_pages):
            body = self._client.get(f"{self.base_url}/events", params={
                "series_slug": series_slug,
                "closed": "true" if closed else "false",
                "limit": page,
                "offset": i * page,
            })
            body.raise_for_status()
            chunk = body.json()
            if not chunk:
                return out
            out.extend(chunk)
        return out

    def event(self, slug: str) -> Optional[dict[str, Any]]:
        r = self._client.get(f"{self.base_url}/events", params={"slug": slug})
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    def teams(self, league: str, limit: int = 200) -> list[dict[str, Any]]:
        r = self._client.get(f"{self.base_url}/teams",
                             params={"league": league, "limit": limit})
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# Quotes and probabilities                                                     #
# --------------------------------------------------------------------------- #
def _money(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _yes_price(market: dict[str, Any]) -> Optional[float]:
    """The Yes leg's price out of `outcomePrices`, which arrives as a JSON *string*.

    `'["0.565", "0.435"]'` — a two-element list whose order follows `outcomes`, so the
    Yes leg is located by name rather than by position. A leg whose outcomes are not
    Yes/No is not a Winner Market leg and yields nothing.
    """
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        prices = json.loads(market.get("outcomePrices") or "[]")
    except (TypeError, ValueError):
        return None
    for name, price in zip(outcomes, prices):
        if str(name).strip().lower() == "yes":
            return _money(price)
    return None


def quote_of(market: dict[str, Any]) -> dict[str, Any]:
    """One outcome's **Quote** — what Polymarket published, kept verbatim.

    `volume` is **dollars traded**, where Kalshi's is contracts. The unit rides along on
    the Quote because the two must never share an axis (ADR 0043), and a bare number
    labelled "volume" on two panels is exactly the lie by omission CONTEXT.md forbids.
    """
    bid, ask = _money(market.get("bestBid")), _money(market.get("bestAsk"))
    return {
        "bid": bid,
        "ask": ask,
        "last": _money(market.get("lastTradePrice")),
        "volume": _money(market.get("volume")),
        "volume_unit": "usd",
        "open_interest": None,
        #: Polymarket's own mid, published as `outcomePrices`. Carried because the CLOB
        #: price history is this same number, so a Market Track and the card agree.
        "mid": _yes_price(market),
    }


def _mid(quote: dict[str, Any]) -> Optional[float]:
    """The mid, preferring Polymarket's own over one we recompute.

    They agree — `bestBid 0.39` / `bestAsk 0.40` published as `0.395` — but the history
    series *is* `outcomePrices`, so taking it here keeps a **Market Track**'s last point
    equal to the number on the card rather than merely close to it.
    """
    if quote.get("mid") is not None:
        return quote["mid"]
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return None


def market_probabilities(quotes: list[dict]) -> tuple[Optional[list[float]], Optional[float]]:
    """**Market Probability** per outcome — the mid, normalised to sum to 1 — and the sum.

    Identical in rule to `kalshi.market_probabilities` and deliberately not shared with
    it: the two Exchanges reach a mid differently and normalising across them is
    forbidden (ADR 0043), so a single helper would invite exactly the merge the design
    refuses.

    The second value keeps the name **overround** while no longer describing its sign:
    Polymarket's three legs sum to roughly 0.995–1.065, and a sum below 1 is ordinary
    there where it never is on Kalshi.

    Returns `(None, None)` unless **every** outcome has a mid. Normalising over two of
    three legs would invent a distribution nobody quoted.
    """
    mids = [_mid(q) for q in quotes]
    if not mids or any(m is None for m in mids):
        return None, None
    total = sum(mids)
    if total <= 0:
        return None, None
    return [m / total for m in mids], total


# --------------------------------------------------------------------------- #
# Resolution                                                                   #
# --------------------------------------------------------------------------- #
def start_time(event: dict[str, Any]) -> Optional[datetime]:
    """The Fixture's kickoff as Polymarket states it — **the exact instant, in UTC**.

    Never the slug, which is dated too and disagrees: an event outlives a postponement
    (*"this market will remain open until the game has been completed"*) and moves
    `startTime` while the slug keeps the date it was born with.
    """
    raw = event.get("startTime") or event.get("gameStartTime")
    if not raw:
        return None
    text = str(raw).strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(second=0, microsecond=0)


def moneyline_legs(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in (event.get("markets") or [])
            if m.get("sportsMarketType") == MONEYLINE]


def _leg_suffix(event_slug: str, market: dict[str, Any]) -> Optional[str]:
    """`lec-nyc-san-2026-08-06-draw` -> `draw`; `…-nyc` -> `nyc`.

    The leg is identified by its slug suffix against the event's own slug and the
    teams' own abbreviations — both strings from the same payload — rather than by
    reading `groupItemTitle`, which is a display name and would put us back to
    matching clubs by name.
    """
    slug = market.get("slug") or ""
    prefix = f"{event_slug}-"
    return slug[len(prefix):] if slug.startswith(prefix) else None


@dataclass
class WinnerMarket:
    """One Fixture's three contracts on one Exchange, resolved against the registry."""
    event_slug: str
    league: str
    kickoff_utc: datetime
    outcomes: list[dict]            # side/team_id/market_slug/token/quote/market_probability
    overround: Optional[float]
    state: str                      # quoted | listed_unquoted
    exchange: str = "polymarket"


def _token_ids(market: dict[str, Any]) -> list[str]:
    try:
        return [str(t) for t in json.loads(market.get("clobTokenIds") or "[]")]
    except (TypeError, ValueError):
        return []


def index_by_team_pair(events: Iterable[dict], league: str,
                       registry: Registry) -> tuple[dict, list[dict]]:
    """Build `{(frozenset(team_ids), kickoff_utc): WinnerMarket}` plus the unmapped Teams.

    A Winner Market whose two clubs do not **both** resolve through the registry is
    refused, never half-attached — the resolver never infers the second club from the
    first, from the abbreviations, or from the instant alone. Those refusals come back
    as the second return value, which is what the `/previews` board shows and what the
    registry proposal is built from.

    The key carries the team pair as well as the instant on purpose: six pairs of
    Leagues Cup Fixtures share a kickoff instant in a single week, so keying on time
    alone would attach one of each pair to the wrong game and do it invisibly.
    """
    index: dict[tuple, WinnerMarket] = {}
    unmapped: list[dict] = []

    for event in events:
        legs = moneyline_legs(event)
        if len(legs) != 3:
            continue                        # not a three-way Winner Market
        kickoff = start_time(event)
        if kickoff is None:
            continue
        event_slug = event.get("slug") or ""
        by_abbrev = {(t.get("abbreviation") or "").lower(): t
                     for t in (event.get("teams") or [])}
        if len(by_abbrev) != 2:
            continue

        sides, missing, draws = [], [], 0
        for market in legs:
            suffix = (_leg_suffix(event_slug, market) or "").lower()
            if suffix == "draw":
                draws += 1
                sides.append(("draw", None, market))
                continue
            team = by_abbrev.get(suffix)
            if team is None:
                sides = []                  # a leg we cannot place structurally
                break
            our_id = registry.team_id(league, team.get("id"))
            if our_id is None:
                missing.append({"polymarket_team": team.get("id"),
                                "polymarket_name": team.get("name"),
                                "league": league,
                                "event_slug": event_slug,
                                "kickoff_utc": kickoff.isoformat()})
            sides.append((None, our_id, market))

        if not sides or draws != 1:
            continue
        if missing:
            unmapped.extend(missing)
            continue                        # refuse, never half-attach

        team_ids = [tid for _s, tid, _m in sides if tid is not None]
        if len(team_ids) != 2:
            continue

        quotes = [quote_of(m) for _s, _t, m in sides]
        probs, overround = market_probabilities(quotes)
        outcomes = []
        for i, (side, our_id, market) in enumerate(sides):
            suffix = (_leg_suffix(event_slug, market) or "").lower()
            team = by_abbrev.get(suffix) or {}
            tokens = _token_ids(market)
            outcomes.append({
                "side": side or "team",
                "team_id": our_id,
                "polymarket_team": team.get("id"),
                "polymarket_name": team.get("name") or market.get("groupItemTitle"),
                "market_slug": market.get("slug"),
                #: The Yes token. A **Market Track** is fetched per token (ADR 0043).
                "token_id": tokens[0] if tokens else None,
                "quote": quotes[i],
                "market_probability": round(probs[i], 4) if probs else None,
            })
        index[(frozenset(team_ids), kickoff)] = WinnerMarket(
            event_slug=event_slug,
            league=league,
            kickoff_utc=kickoff,
            outcomes=outcomes,
            overround=round(overround, 4) if overround else None,
            state="quoted" if probs else "listed_unquoted",
        )
    return index, unmapped


def home_team_id(event: dict[str, Any], league: str,
                 registry: Registry) -> Optional[int]:
    """Our id for the club Polymarket marks `ordering: home`, if the registry knows it.

    Kalshi states no such thing, so this is the one check the Polymarket half can make
    that the Kalshi half cannot. It is a **check, not a source**: sides are always
    assigned from the registry, and this only ever refuses (ADR 0043).
    """
    for team in event.get("teams") or []:
        if (team.get("ordering") or "").lower() == "home":
            return registry.team_id(league, team.get("id"))
    return None


def attach(fixture_home_id: int, fixture_away_id: int, kickoff_utc: datetime,
           index: dict) -> Optional[WinnerMarket]:
    """The Winner Market for this Fixture, or None. Both clubs and the instant must agree."""
    if kickoff_utc.tzinfo is None:
        kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)
    key = (frozenset({fixture_home_id, fixture_away_id}),
           kickoff_utc.astimezone(timezone.utc).replace(second=0, microsecond=0))
    return index.get(key)


def label_sides(market: WinnerMarket, home_id: int, away_id: int) -> WinnerMarket:
    """Rewrite each outcome's `side` to home/away/draw for the card."""
    for o in market.outcomes:
        if o["team_id"] == home_id:
            o["side"] = "home"
        elif o["team_id"] == away_id:
            o["side"] = "away"
        else:
            o["side"] = "draw"
    order = {"home": 0, "draw": 1, "away": 2}
    market.outcomes.sort(key=lambda o: order.get(o["side"], 9))
    return market


# --------------------------------------------------------------------------- #
# Proposing registry entries                                                   #
# --------------------------------------------------------------------------- #
#: Bootstrap only. Once the registry exists it is the source of truth, and this is never
#: consulted again. Each series slug is *verified* rather than guessed: `sport=lec` and
#: `tag_slug=lec` both return events with no moneylines at all (ADR 0043).
BOOTSTRAP_LEAGUES = {
    "mls": {"competition_id": 253, "series_slug": "mls-2025"},
    "mex": {"competition_id": 262, "series_slug": "mex-2025"},
    "lec": {"competition_id": 772, "series_slug": "soccer-lec"},
    # La Liga's series slug is `la-liga-2025` — spelled out, unlike `mex`/`mls`, and
    # unrelated to the `lal` league key that keys its teams and heads its event slugs
    # (`lal-cel-osa-2026-08-16`). `laliga-2025`, `lal-2025`, `soccer-lal` and `la-liga`
    # all return zero events without erroring, which is the failure ADR 0043 records.
    "lal": {"competition_id": 140, "series_slug": "la-liga-2025"},
    # The Premier League repeats the lesson: the league key is `epl`, the slug
    # that enumerates it is `premier-league-2025`, and `epl-2025`, `epl-2026`,
    # `soccer-epl` and `english-premier-league-2025` all return zero events
    # without erroring.
    "epl": {"competition_id": 39, "series_slug": "premier-league-2025"},
}


def _our_teams(competition_ids: list[int]) -> dict[int, str]:
    """`team_id -> name` for every Team contesting a recent Fixture in these Competitions."""
    from .postgres import get_conn
    with get_conn().cursor() as cur:
        cur.execute(
            "SELECT DISTINCT t.id, t.name FROM team t "
            "JOIN fixture f ON f.home_team_id = t.id OR f.away_team_id = t.id "
            "WHERE f.league_id = ANY(%s) AND f.season >= %s",
            (competition_ids, 2025),
        )
        return {int(i): n for i, n in cur.fetchall()}


def propose(leagues: dict[str, dict] | None = None,
            client: PolymarketClient | None = None) -> dict[str, Any]:
    """Propose a Polymarket team registry: confident matches, and the ones needing a human.

    `football.teamnames.canonical` ranks the proposals and does nothing more. It resolves
    a **respelling** and never an **alternate name** (ADR 0039), so `Atlante FC`/`Atlante`
    matches while `Deportivo Toluca FC`/`Toluca` and `Pumas de la UNAM`/`U.N.A.M. - Pumas`
    do not — which is exactly why the registry exists rather than name matching at read
    time (ADR 0041/0043).
    """
    leagues = leagues or BOOTSTRAP_LEAGUES
    owns_client = client is None
    client = client or PolymarketClient()
    try:
        ours = _our_teams(sorted({int(v["competition_id"]) for v in leagues.values()}))
        by_canon: dict[str, list[tuple[int, str]]] = {}
        for tid, name in ours.items():
            by_canon.setdefault(teamnames.canonical(name), []).append((tid, name))

        matched: dict[str, dict] = {}
        needs_human: list[dict] = []
        for league in sorted(leagues):
            for team in client.teams(league):
                pm_id, pm_name = team.get("id"), team.get("name")
                if pm_id is None or not pm_name:
                    continue
                key = registry_key(league, pm_id)
                hits = by_canon.get(teamnames.canonical(pm_name), [])
                if len(hits) == 1:
                    tid, our_name = hits[0]
                    matched[key] = {"team_id": tid, "polymarket_name": pm_name,
                                    "our_name": our_name}
                else:
                    needs_human.append({
                        "key": key,
                        "polymarket_team": pm_id,
                        "polymarket_name": pm_name,
                        "league": league,
                        "reason": "ambiguous" if hits else "no canonical match",
                    })
        return {"leagues": leagues, "teams": matched,
                "needs_human": needs_human, "our_teams": ours}
    finally:
        if owns_client:
            client.close()


def write_registry(proposal: dict, path: Path = REGISTRY_FILE,
                   keep_existing: bool = True) -> tuple[int, int]:
    """Write the registry **uncommitted**, for review as a `git diff` (ADR 0041/0043).

    Existing entries win: a mapping a human has already reviewed is never overwritten by
    a fresh proposal. Returns `(total_entries, newly_added)`.
    """
    existing = json.loads(path.read_text()) if (path.exists() and keep_existing) else {}
    teams = dict(existing.get("teams") or {})
    added = 0
    for key, entry in proposal["teams"].items():
        if key not in teams:
            teams[key] = entry
            added += 1
    payload = {
        "_comment": (
            "Polymarket team registry (ADR 0043). '<league>:<polymarket team id>' -> our "
            "team id. The key carries the league because a Polymarket team id is per "
            "league, not per club — CF Monterrey is 115320 in mex and 3268366 in lec. A "
            "Winner Market is attached only when BOTH clubs resolve here; nothing is "
            "guessed from names. Reviewed as a git diff before commit — check that each "
            "polymarket_name and our_name name the same club."),
        "leagues": {k: proposal["leagues"][k] for k in sorted(proposal["leagues"])},
        "teams": {k: teams[k] for k in sorted(
            teams, key=lambda k: (k.split(":")[0], teams[k]["polymarket_name"] or ""))},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return len(teams), added


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m football_blog.polymarket",
        description="Propose or inspect the Polymarket team registry (ADR 0043).")
    ap.add_argument("--propose", action="store_true",
                    help="fetch each league's teams, match what can be matched, and write "
                         "the registry uncommitted for `git diff` review.")
    ap.add_argument("--dry-run", action="store_true", help="propose but write nothing.")
    args = ap.parse_args(argv)

    if not args.propose:
        reg = load_registry()
        print(f"{len(reg.teams)} Teams mapped across {len(reg.leagues)} leagues "
              f"({', '.join(reg.league_keys)})")
        for key in reg.league_keys:
            print(f"  {key:6} -> competition {reg.competition_for(key)}  "
                  f"series_slug {reg.series_slug(key)}")
        return 0

    p = propose()
    total_seen = len(p["teams"]) + len(p["needs_human"])
    print(f"Polymarket Teams seen: {total_seen}  "
          f"| matched by canonical name: {len(p['teams'])}  "
          f"| need a human: {len(p['needs_human'])}")
    if not args.dry_run:
        total, added = write_registry(p)
        print(f"Wrote {REGISTRY_FILE} — {total} entries ({added} new). Review with:\n"
              f"    git diff -- {REGISTRY_FILE}")
    if p["needs_human"]:
        print("\nUnmapped — a Winner Market naming any of these is refused, never "
              "half-attached:")
        for row in p["needs_human"]:
            print(f"  {row['polymarket_name']:<28} {row['key']:<18} ({row['reason']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
