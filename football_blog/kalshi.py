"""Kalshi **Winner Markets**, and the Team registry that attaches them (ADR 0041).

A Winner Market is the three mutually exclusive contracts Kalshi lists on one Fixture's
result — home, away and the draw — and it is the only Kalshi product we take. The same
Fixture also carries spread, total, both-teams-to-score, correct-score and first-half
series; "the market for this game" names a dozen things, so the qualifier is load-bearing.

Three things here are easy to get wrong and are therefore not left to inference:

**Prices live in the decimal fields, not the cent ones.** `yes_bid`, `yes_ask`,
`last_price`, `volume` and `open_interest` now return `None` on every market, active and
settled alike. The live values are `yes_bid_dollars`, `yes_ask_dollars`,
`last_price_dollars`, `volume_fp` and `open_interest_fp`. Code reading the cent fields
does not fail — it silently reports nothing, which is what `predicitons/fastapi_server/`
does today.

**A Team is identified by UUID, never by name.** Kalshi's names disagree with ours about
half the time in ways no canonical comparison reconciles (`Tigres UANL`/`Tigres`,
`Guadalajara Chivas`/`Guadalajara`, `Club Tijuana`/`Tijuana de Caliente`). The registry is
the only bridge, and `football.teamnames` appears here **only to rank proposals for human
review** — never to accept one.

**A Fixture and its market are matched on the local match date.** Kalshi's ticker is dated
in the match's own locale, so a 01:00 UTC kickoff is the *previous* day's market. The date
compared is the kickoff in the Publication's `display_timezone` — the same date that fixes
a Match Post's slug (ADR 0029).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import httpx

from football import teamnames

REGISTRY_FILE = Path(__file__).resolve().parent / "registry" / "kalshi_teams.json"

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

#: Month abbreviations as Kalshi writes them into a ticker (`26AUG15`). Spelled out
#: rather than parsed with `%b`, which is locale-dependent — a machine with a non-English
#: locale would silently stop resolving every market.
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


# --------------------------------------------------------------------------- #
# The registry                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Registry:
    """The committed `Kalshi team UUID -> our team id` map, plus series -> Competition.

    A **Registry** in CONTEXT.md's sense: input, never output; decided rather than
    fetched; no rebuild path from the raw cache; every change reviewed as a diff. Nothing
    is attached for a Team it does not name.
    """
    series: dict[str, int]          # series ticker -> our competition (league) id
    teams: dict[str, int]           # kalshi soccer_team uuid -> our team id
    draw_uuid: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def competition_for(self, series_ticker: str) -> Optional[int]:
        return self.series.get(series_ticker.upper())

    @property
    def series_tickers(self) -> list[str]:
        return sorted(self.series)


def load_registry(path: Path = REGISTRY_FILE) -> Registry:
    if not path.exists():
        raise FileNotFoundError(
            f"No Kalshi team registry at {path}. Nothing is attached for a Team no "
            f"Registry names (CONTEXT.md) — bootstrap it with:\n"
            f"    uv run python -m football_blog.kalshi --propose")
    raw = json.loads(path.read_text())
    return Registry(
        series={k.upper(): int(v) for k, v in raw.get("series", {}).items()},
        teams={k: int(v["team_id"]) for k, v in raw.get("teams", {}).items()
               if v.get("team_id") is not None},
        draw_uuid=raw["draw_uuid"],
        raw=raw,
    )


# --------------------------------------------------------------------------- #
# The client                                                                   #
# --------------------------------------------------------------------------- #
class KalshiClient:
    """Read-only, unauthenticated. The whole market half is one GET per series.

    `.env`'s `kalshi_api` is an API *Key ID* with no accompanying RSA private key, and
    nothing here needs one: `/markets?series_ticker=…` returns every open market for a
    series, priced, unpaginated (~90 for MLS). No quota to budget.
    """

    def __init__(self, base_url: str = BASE_URL, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def markets(self, series_ticker: str, status: str = "open") -> list[dict[str, Any]]:
        """Every market in a series at `status`. `open` is the only one worth fetching:
        `unopened` and `closed` both come back empty for these series, so a Fixture with
        no open market is genuinely **not listed yet** rather than hiding under another
        status. Kalshi opens a game market roughly five days out.
        """
        out: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params = {"series_ticker": series_ticker.upper(), "status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = self._client.get(f"{self.base_url}/markets", params=params)
            r.raise_for_status()
            body = r.json()
            out.extend(body.get("markets") or [])
            cursor = body.get("cursor") or None
            if not cursor:
                return out

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# Quotes and probabilities                                                     #
# --------------------------------------------------------------------------- #
def _money(value: Any) -> Optional[float]:
    """Kalshi's decimal-string money fields (`'0.6600'`) -> float, or None."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quote_of(market: dict[str, Any]) -> dict[str, Optional[float]]:
    """One outcome's **Quote** — what Kalshi published, kept verbatim.

    Volume is carried because it is the only honest depth signal: `liquidity_dollars`
    reads `0.0000` on markets with tens of thousands of contracts traded, and a
    34,197-contract Quote and a 162-contract Quote are not the same claim.
    """
    return {
        "bid": _money(market.get("yes_bid_dollars")),
        "ask": _money(market.get("yes_ask_dollars")),
        "last": _money(market.get("last_price_dollars")),
        "volume": _money(market.get("volume_fp")),
        "open_interest": _money(market.get("open_interest_fp")),
    }


def _mid(quote: dict[str, Optional[float]]) -> Optional[float]:
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return None


def market_probabilities(quotes: list[dict]) -> tuple[Optional[list[float]], Optional[float]]:
    """**Market Probability** per outcome — the mid, normalised to sum to 1 — and the overround.

    Ours, not Kalshi's: the exchange publishes a bid, an ask and a last trade, and the
    three legs' raw mids sum to roughly 1.005–1.025. Three percentages on a card that add
    to 101.5% read as a broken site, not as a discovered vig, so they are normalised — and
    the raw Quote is kept beside them so the overround stays auditable.

    Returns `(None, None)` unless **every** outcome has a two-sided quote. Normalising
    over two of three legs would invent a distribution that nobody quoted.
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
def ticker_date(event_ticker: str, series_ticker: str) -> Optional[date]:
    """`KXMLSGAME-26AUG15ATLNYRB` -> `date(2026, 8, 15)` — the **local** match date.

    Not the kickoff in UTC. Kalshi's `occurrence_datetime` is not the kickoff either: it
    equals `expected_expiration_time`, some hours after the match, and is usable only as a
    sanity band (ADR 0041).
    """
    prefix = f"{series_ticker.upper()}-"
    if not event_ticker.upper().startswith(prefix):
        return None
    rest = event_ticker[len(prefix):]
    if len(rest) < 7:
        return None
    yy, mon, dd = rest[:2], rest[2:5].upper(), rest[5:7]
    if mon not in _MONTHS or not (yy.isdigit() and dd.isdigit()):
        return None
    try:
        return date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None


@dataclass
class WinnerMarket:
    """One Fixture's three contracts, resolved against the registry."""
    event_ticker: str
    series_ticker: str
    local_date: date
    outcomes: list[dict]            # side/team_id/market_ticker/quote/market_probability
    overround: Optional[float]
    state: str                      # quoted | listed_unquoted


def group_events(markets: Iterable[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for m in markets:
        et = m.get("event_ticker")
        if et:
            grouped.setdefault(et, []).append(m)
    return grouped


def index_by_team_pair(markets: Iterable[dict], series_ticker: str,
                       registry: Registry) -> tuple[dict, list[dict]]:
    """Build `{(frozenset(team_ids), local_date): WinnerMarket}` plus the unmapped Teams.

    A Winner Market whose two clubs do not **both** resolve through the registry is
    refused, never half-attached: the resolver never infers the second team from the
    first, from the ticker abbreviations, or from the date alone (ADR 0041). Those
    refusals come back as the second return value, which is what the `/previews` board
    shows and what the registry proposal is built from.
    """
    index: dict[tuple, WinnerMarket] = {}
    unmapped: list[dict] = []

    for event_ticker, legs in group_events(markets).items():
        local = ticker_date(event_ticker, series_ticker)
        if local is None:
            continue
        sides, missing = [], []
        for m in legs:
            uuid = (m.get("custom_strike") or {}).get("soccer_team")
            if uuid == registry.draw_uuid:
                sides.append(("draw", None, m))
                continue
            team_id = registry.teams.get(uuid)
            if team_id is None:
                missing.append({"kalshi_team": uuid,
                                "kalshi_name": m.get("yes_sub_title"),
                                "event_ticker": event_ticker,
                                "series_ticker": series_ticker,
                                "local_date": local.isoformat()})
            sides.append((None, team_id, m))

        if missing:
            unmapped.extend(missing)
            continue                      # refuse, never half-attach

        team_ids = [tid for _s, tid, _m in sides if tid is not None]
        if len(team_ids) != 2:
            continue                      # not a two-club winner market

        quotes = [quote_of(m) for _s, _t, m in sides]
        probs, overround = market_probabilities(quotes)
        outcomes = []
        for i, (side, team_id, m) in enumerate(sides):
            outcomes.append({
                "side": side or "team",
                "team_id": team_id,
                "kalshi_team": (m.get("custom_strike") or {}).get("soccer_team"),
                "kalshi_name": m.get("yes_sub_title"),
                "market_ticker": m.get("ticker"),
                "quote": quotes[i],
                "market_probability": round(probs[i], 4) if probs else None,
            })
        index[(frozenset(team_ids), local)] = WinnerMarket(
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            local_date=local,
            outcomes=outcomes,
            overround=round(overround, 4) if overround else None,
            state="quoted" if probs else "listed_unquoted",
        )
    return index, unmapped


def local_match_date(kickoff_utc: datetime, display_timezone: str) -> date:
    """The Fixture's kickoff as a date in its Publication's timezone (ADR 0029/0041)."""
    if kickoff_utc.tzinfo is None:
        kickoff_utc = kickoff_utc.replace(tzinfo=ZoneInfo("UTC"))
    return kickoff_utc.astimezone(ZoneInfo(display_timezone)).date()


def attach(fixture_home_id: int, fixture_away_id: int, kickoff_utc: datetime,
           display_timezone: str, index: dict) -> Optional[WinnerMarket]:
    """The Winner Market for this Fixture, or None. Both clubs and the date must agree."""
    key = (frozenset({fixture_home_id, fixture_away_id}),
           local_match_date(kickoff_utc, display_timezone))
    return index.get(key)


def label_sides(market: WinnerMarket, home_team_id: int, away_team_id: int) -> WinnerMarket:
    """Rewrite each outcome's `side` to home/away/draw for the card."""
    for o in market.outcomes:
        if o["team_id"] == home_team_id:
            o["side"] = "home"
        elif o["team_id"] == away_team_id:
            o["side"] = "away"
        else:
            o["side"] = "draw"
    order = {"home": 0, "draw": 1, "away": 2}
    market.outcomes.sort(key=lambda o: order.get(o["side"], 9))
    return market


# --------------------------------------------------------------------------- #
# Proposing registry entries                                                   #
# --------------------------------------------------------------------------- #
#: Bootstrap only. Once the registry exists it is the source of truth for both, and this
#: is never consulted again. Kept here rather than in the file's absence so `--propose`
#: has somewhere to start on a machine with no registry at all.
BOOTSTRAP_SERIES = {"KXMLSGAME": 253, "KXLIGAMXGAME": 262, "KXLEAGUESCUPGAME": 772,
                    "KXLALIGAGAME": 140, "KXEPLGAME": 39}


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


def propose(series: dict[str, int] | None = None,
            client: KalshiClient | None = None) -> dict[str, Any]:
    """Propose a Kalshi team registry: confident matches, and the ones needing a human.

    `football.teamnames.canonical` ranks the proposals and does nothing more. It resolves
    a **respelling** and never an **alternate name** (ADR 0039), so `Atlante FC`/`Atlante`
    matches while `Tigres UANL`/`Tigres` does not — roughly half the clubs in scope fall
    to the human, which is exactly why the registry exists rather than name matching at
    read time (ADR 0041).
    """
    series = series or BOOTSTRAP_SERIES
    owns_client = client is None
    client = client or KalshiClient()
    try:
        ours = _our_teams(sorted(set(series.values())))
        by_canon: dict[str, list[tuple[int, str]]] = {}
        for tid, name in ours.items():
            by_canon.setdefault(teamnames.canonical(name), []).append((tid, name))

        seen: dict[str, dict] = {}
        draw_uuids: set[str] = set()
        for series_ticker, competition_id in series.items():
            for m in client.markets(series_ticker):
                uuid = (m.get("custom_strike") or {}).get("soccer_team")
                name = m.get("yes_sub_title")
                if not uuid:
                    continue
                # Bootstrap only: the draw is named once so the file can record its UUID,
                # after which it is recognised structurally and never by this string.
                if (name or "").strip().lower() == "tie":
                    draw_uuids.add(uuid)
                    continue
                seen.setdefault(uuid, {"kalshi_name": name, "series": set()})
                seen[uuid]["series"].add(series_ticker)

        matched, needs_human = {}, []
        for uuid, info in sorted(seen.items(), key=lambda kv: kv[1]["kalshi_name"] or ""):
            hits = by_canon.get(teamnames.canonical(info["kalshi_name"]), [])
            if len(hits) == 1:
                tid, our_name = hits[0]
                matched[uuid] = {"team_id": tid, "kalshi_name": info["kalshi_name"],
                                 "our_name": our_name}
            else:
                needs_human.append({
                    "kalshi_team": uuid,
                    "kalshi_name": info["kalshi_name"],
                    "series": sorted(info["series"]),
                    "reason": "ambiguous" if hits else "no canonical match",
                })
        if len(draw_uuids) != 1:
            raise SystemExit(
                f"Expected exactly one draw UUID across every Winner Market, found "
                f"{len(draw_uuids)}: {sorted(draw_uuids)}. The draw is a contract like any "
                f"other and must be identifiable structurally — refusing to guess.")
        return {"series": series, "draw_uuid": draw_uuids.pop(),
                "teams": matched, "needs_human": needs_human, "our_teams": ours}
    finally:
        if owns_client:
            client.close()


def write_registry(proposal: dict, path: Path = REGISTRY_FILE,
                   keep_existing: bool = True) -> tuple[int, int]:
    """Write the registry **uncommitted**, for review as a `git diff` (ADR 0041).

    Existing entries win: a mapping a human has already reviewed is never overwritten by
    a fresh proposal. Returns `(total_entries, newly_added)`.
    """
    existing = json.loads(path.read_text()) if (path.exists() and keep_existing) else {}
    teams = dict(existing.get("teams") or {})
    added = 0
    for uuid, entry in proposal["teams"].items():
        if uuid not in teams:
            teams[uuid] = entry
            added += 1
    payload = {
        "_comment": (
            "Kalshi team registry (ADR 0041). Kalshi soccer_team UUID -> our team id. "
            "A Winner Market is attached only when BOTH clubs resolve here; nothing is "
            "guessed from names. Reviewed as a git diff before commit — check that each "
            "kalshi_name and our_name name the same club."),
        "series": {k: v for k, v in sorted(proposal["series"].items())},
        "draw_uuid": proposal["draw_uuid"],
        "teams": {k: teams[k] for k in sorted(teams, key=lambda u: teams[u]["kalshi_name"] or "")},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return len(teams), added


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m football_blog.kalshi",
        description="Propose or inspect the Kalshi team registry (ADR 0041).")
    ap.add_argument("--propose", action="store_true",
                    help="fetch the series, match what can be matched, and write the "
                         "registry uncommitted for `git diff` review.")
    ap.add_argument("--dry-run", action="store_true", help="propose but write nothing.")
    args = ap.parse_args(argv)

    if not args.propose:
        reg = load_registry()
        print(f"{len(reg.teams)} Teams mapped across {len(reg.series)} series "
              f"({', '.join(reg.series_tickers)}); draw = {reg.draw_uuid}")
        return 0

    p = propose()
    print(f"Kalshi Teams seen: {len(p['teams']) + len(p['needs_human'])}  "
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
            print(f"  {row['kalshi_name']:<24} {row['kalshi_team']}  "
                  f"({row['reason']}; {', '.join(row['series'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
