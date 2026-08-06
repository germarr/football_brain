"""Build **Match Previews** — the forward half of the blog (ADR 0040).

    uv run python -m football_blog.preview --full      # nightly: everything
    uv run python -m football_blog.preview --quotes    # hourly: Winner Markets only

A Match Preview is the card shown for a Fixture that has **not been played yet**: each
Team's table position and points, its leading scorer and assister, and the prediction
market's view of the result. It is **derived** — every field falls out of a rebuild — and
so, unlike a **Match Post**, overwriting one costs nothing. There is one per Fixture,
keyed on the Fixture id and on nothing else.

## Two modes, because the halves have different economics

The football half sits behind a quota-bound **Refresh** and only moves when a Fixture goes
Final, so `--full` runs nightly. The market half is three unauthenticated GETs and moves
continuously, so `--quotes` runs hourly and touches only `market`, `market_state` and
`quote_read_at`. The record carries **two timestamps** because its halves are true as of
two different moments; one `updated` field would misdate whichever it did not describe.

## The freeze

At kickoff a Match Preview stops being rewritten, and `--full` and `--quotes` both run the
freeze pass before anything else. This is not tidiness: the football half stays derivable
forever (it is only history), but a **Quote** is a point-in-time read and nothing
reconstructs what the market thought an hour before kickoff — Kalshi's candlesticks come
back empty for these series. So the last Quote read before kickoff is the one that stays,
and `settle_preview` patches `lifecycle` alone so the freeze cannot become an occasion to
overwrite it.

## What refuses, and what merely thins

Only two things gate a Match Preview existing at all: the Fixture is scheduled, and its
Competition has a **published** Publication. Everything else is a signal — an absent table
(a knockout Fixture has none, and always will not), an empty leader block, an unlisted
Winner Market. Each is written onto the card *saying so*, in the spirit of ADR 0014's
Coverage-light Competitions: the Publication still gets a preview, with fewer facts on it.
"""
from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from football import standings as standings_mod
from football.status import FINAL

from . import kalshi
from .pocketbase import PocketBaseClient
from .postgres import get_conn

log = logging.getLogger(__name__)

#: How far ahead a Match Preview exists. Seven days (CONTEXT.md).
WINDOW_DAYS = 7

#: A Fixture that has not been played and still might be. Selected by **whitelist**, never
#: by excluding the terminal states: the store holds cancelled Fixtures under two spellings
#: (`CANC` 78 rows, `Canc` 5), so a blacklist silently previews five cancelled matches.
SCHEDULED = ("NS", "TBD")


# --------------------------------------------------------------------------- #
# Scopes                                                                       #
# --------------------------------------------------------------------------- #
def _has_table(cur, league_id: int, season: int, tournament: str) -> bool:
    """Does this Tournament have a round-robin table behind it?

    Two ways to qualify, and the second is a **workaround for a known-wrong model** —
    see ADR 0040's consequences before treating it as design. Leagues Cup (772) is
    registered as `type: "league"` though it is a cup, so its `phase` column is null and
    its cup rounds (`Group Stage`, `Quarter-finals`, `Final`) sit in the *tournament*
    column, which CONTEXT.md forbids. Its 2026 Group Stage also carries no matchday where
    2025's did, so the matchday test alone finds nothing and Leagues Cup — the competition
    running this week — would show no table at all.

    A knockout Tournament qualifies under neither, correctly and permanently: there is no
    round-robin behind a quarter-final, and fixing the registry would not create one.
    """
    if tournament == "Group Stage":
        return True
    cur.execute("SELECT count(*) FROM fixture WHERE league_id = %s AND season = %s "
                "AND tournament = %s AND matchday IS NOT NULL",
                (league_id, season, tournament))
    return cur.fetchone()[0] > 0


def _current_domestic_scope(cur, competition_id: int) -> Optional[tuple[int, str]]:
    """The (season, tournament) a club's domestic league is *currently* playing.

    ADR 0025's rule — the latest with at least one **Final** — rather than
    `max(season)`, which through an off-season names a campaign that has not kicked off
    and whose every leader is therefore zero.
    """
    cur.execute(
        "SELECT season, tournament, max(date) FROM fixture "
        "WHERE league_id = %s AND status = ANY(%s) "
        "GROUP BY season, tournament ORDER BY max(date) DESC LIMIT 1",
        (competition_id, list(FINAL)))
    row = cur.fetchone()
    return (int(row[0]), row[1]) if row else None


def _standings_for(cur, league_id: int, season: int, tournament: str) -> list[dict]:
    cur.execute(
        "SELECT home_team_id, home_team_name, away_team_id, away_team_name, "
        "       home_goals, away_goals, status "
        "FROM fixture WHERE league_id = %s AND season = %s AND tournament = %s",
        (league_id, season, tournament))
    return standings_mod.standings_rows(cur.fetchall())


def _leaders_for(cur, league_id: int, season: int, tournament: str) -> dict:
    """Per-Team leading scorer and assister for one scope, plus each Team's games played.

    One aggregation per scope, not per Team — the board renders dozens of cards and a
    query per side would be hundreds of round trips.
    """
    cur.execute(
        "SELECT se.team_id, se.player_id, p.name, "
        "       sum(se.goals), sum(se.assists), count(DISTINCT se.fixture_id) "
        "FROM squadentry se "
        "JOIN fixture f ON f.id = se.fixture_id "
        "JOIN player p ON p.id = se.player_id "
        "WHERE f.league_id = %s AND f.season = %s AND f.tournament = %s "
        "  AND f.status = ANY(%s) "
        "GROUP BY se.team_id, se.player_id, p.name",
        (league_id, season, tournament, list(FINAL)))
    rows = cur.fetchall()
    entries = [(int(t), int(pid), name, int(g or 0), int(a or 0)) for t, pid, name, g, a, _n in rows]
    played: dict[int, int] = {}
    cur.execute(
        "SELECT team_id, count(*) FROM ("
        "  SELECT home_team_id AS team_id FROM fixture "
        "   WHERE league_id=%s AND season=%s AND tournament=%s AND status = ANY(%s) "
        "  UNION ALL "
        "  SELECT away_team_id FROM fixture "
        "   WHERE league_id=%s AND season=%s AND tournament=%s AND status = ANY(%s)"
        ") x GROUP BY team_id",
        (league_id, season, tournament, list(FINAL)) * 2)
    for tid, n in cur.fetchall():
        played[int(tid)] = int(n)
    return {
        "goals": standings_mod.leaders_by_team(entries, "goals"),
        "assists": standings_mod.leaders_by_team(entries, "assists"),
        "played": played,
    }


def _scope_label(competition_id: int, season: int, tournament: str,
                 names: dict[int, str]) -> dict:
    return {"competition_id": competition_id,
            "competition": names.get(competition_id, str(competition_id)),
            "season": season, "tournament": tournament,
            "label": f"{names.get(competition_id, competition_id)} {tournament} {season}"}


# --------------------------------------------------------------------------- #
# Assembly                                                                     #
# --------------------------------------------------------------------------- #
def _team_block(team_id: int, profile: dict, table_rows: list[dict],
                leader_scopes: list[tuple[dict, dict]]) -> dict:
    """One side of the card: identity, table row, and **Team Leaders** with their scopes."""
    block: dict[str, Any] = {
        "team_id": team_id,
        "name": profile.get("name"),
        "profile": {k: profile.get(k) for k in
                    ("code", "country", "founded", "logo", "is_national")},
    }
    row = next((r for r in table_rows if r["team_id"] == team_id), None)
    if row is None:
        block["table"] = {"state": "absent", "reason": "knockout"}
    else:
        pos = table_rows.index(row) + 1
        block["table"] = {
            "state": "present", "position": pos, "points": row["Pts"],
            "played": row["P"], "w": row["W"], "d": row["D"], "l": row["L"],
            "gf": row["GF"], "ga": row["GA"], "gd": row["GD"],
            # ADR 0025's footnote travels with the row rather than living in a template:
            # the sort is Pts/GD/GF and real competitions use their own tiebreakers.
            "unofficial_ordering": True,
        }

    leaders = []
    for scope, computed in leader_scopes:
        scorer = computed["goals"].get(team_id)
        assister = computed["assists"].get(team_id)
        if scorer is None and assister is None:
            # A three-game tournament in which nobody has scored is a fact, not a gap —
            # but an empty block says nothing, so it is dropped and the *other* scope
            # carries the card. Team Leaders exist precisely so one of them is populated.
            continue
        leaders.append({
            "scope": {**scope, "played": computed["played"].get(team_id, 0)},
            "top_scorer": scorer,
            "top_assister": assister,
        })
    block["leaders"] = leaders
    return block


def _market_block(market: Optional[kalshi.WinnerMarket], reason: str) -> dict:
    if market is None:
        return {"state": "absent", "reason": reason, "source": "kalshi"}
    return {
        "state": market.state,
        "source": "kalshi",
        "series_ticker": market.series_ticker,
        "event_ticker": market.event_ticker,
        # Kalshi resolves on 90'+stoppage; our scoreline is after extra time (ADR 0012).
        # For a knockout tie that goes past 90 the two disagree, legitimately, about
        # different questions — so the basis is stated on the record.
        "settles_on": "regulation",
        "overround": market.overround,
        "outcomes": market.outcomes,
    }


# --------------------------------------------------------------------------- #
# The run                                                                      #
# --------------------------------------------------------------------------- #
def _publications(pb: PocketBaseClient) -> dict[int, dict]:
    """Published Publications only, by Competition.

    Deliberately unlike `candidates.py`, which passes `only_published=False`. The Desk's
    board is an internal work list where the gate is irrelevant; this feed is read by the
    public site, where the gate is the entire point (ADR 0040).
    """
    return {int(p["postgres_competition_id"]): p
            for p in pb.list_publications(only_published=True)}


def freeze_kicked_off(pb: PocketBaseClient, now: Optional[datetime] = None) -> int:
    """Flip every `upcoming` Match Preview whose kickoff has passed to `settled`."""
    now = now or datetime.now(timezone.utc)
    frozen = 0
    for rec in pb.list_previews(lifecycle="upcoming"):
        kickoff = datetime.fromisoformat(rec["kickoff_utc"].replace("Z", "+00:00"))
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        if kickoff <= now:
            pb.settle_preview(rec["id"])
            frozen += 1
    return frozen


def _market_index(registry: kalshi.Registry) -> tuple[dict, list[dict]]:
    client = kalshi.KalshiClient()
    index, unmapped = {}, []
    try:
        for series in registry.series_tickers:
            markets = client.markets(series)
            idx, un = kalshi.index_by_team_pair(markets, series, registry)
            index.update(idx)
            unmapped.extend(un)
    finally:
        client.close()
    return index, unmapped


def upcoming_fixtures(cur, competition_ids: list[int], now: datetime,
                      days: int = WINDOW_DAYS) -> list[tuple]:
    cur.execute(
        "SELECT id, league_id, season, tournament, date, "
        "       home_team_id, away_team_id, status "
        "FROM fixture "
        "WHERE league_id = ANY(%s) AND status = ANY(%s) "
        "  AND date >= %s AND date < %s "
        "ORDER BY date",
        (competition_ids, list(SCHEDULED),
         now.replace(tzinfo=None), (now + timedelta(days=days)).replace(tzinfo=None)))
    return cur.fetchall()


def build(full: bool = True, dry_run: bool = False,
          now: Optional[datetime] = None) -> dict[str, int]:
    """One run. `full` recomputes both halves; otherwise only the market half moves."""
    now = now or datetime.now(timezone.utc)
    counts = defaultdict(int)

    pb = PocketBaseClient()
    try:
        counts["frozen"] = 0 if dry_run else freeze_kicked_off(pb, now)

        publications = _publications(pb)
        if not publications:
            print("No published Publication — nothing to preview.")
            return dict(counts)

        registry = kalshi.load_registry()
        index, unmapped = _market_index(registry)
        counts["unmapped_teams"] = len({u["kalshi_team"] for u in unmapped})

        conn = get_conn()
        with conn.cursor() as cur:
            fixtures = upcoming_fixtures(cur, sorted(publications), now)
            counts["fixtures"] = len(fixtures)
            if not fixtures:
                print("No scheduled Fixture in the window.")
                return dict(counts)

            existing = pb.list_previews_by_fixture_ids([int(f[0]) for f in fixtures])

            cur.execute("SELECT id, name FROM competition WHERE id = ANY(%s)",
                        (sorted(publications),))
            comp_names = {int(i): n for i, n in cur.fetchall()}

            # --- resolve every scope once, then reuse across cards -----------------
            fixture_scopes = {(int(lid), int(s), t) for _i, lid, s, t, *_ in fixtures}
            table_cache, leader_cache, has_table = {}, {}, {}
            for scope in fixture_scopes:
                has_table[scope] = _has_table(cur, *scope)
                table_cache[scope] = _standings_for(cur, *scope) if has_table[scope] else []
                leader_cache[scope] = _leaders_for(cur, *scope)

            team_ids = sorted({int(f[5]) for f in fixtures} | {int(f[6]) for f in fixtures})
            cur.execute(
                "SELECT id, name, code, country, founded, logo, is_national, league_id "
                "FROM teamprofile WHERE id = ANY(%s)", (team_ids,))
            profiles = {int(r[0]): dict(zip(
                ("id", "name", "code", "country", "founded", "logo", "is_national",
                 "league_id"), r)) for r in cur.fetchall()}

            domestic_scopes: dict[int, tuple] = {}
            for tid in team_ids:
                dom = profiles.get(tid, {}).get("league_id")
                if dom is None:
                    continue
                dom = int(dom)
                if dom not in domestic_scopes:
                    resolved = _current_domestic_scope(cur, dom)
                    if resolved:
                        scope = (dom, resolved[0], resolved[1])
                        domestic_scopes[dom] = scope
                        if scope not in leader_cache:
                            leader_cache[scope] = _leaders_for(cur, *scope)
                        if dom not in comp_names:
                            cur.execute("SELECT name FROM competition WHERE id = %s", (dom,))
                            got = cur.fetchone()
                            comp_names[dom] = got[0] if got else str(dom)

            # --- one card per Fixture ---------------------------------------------
            for (fid, lid, season, tournament, kickoff, home_id, away_id, status) in fixtures:
                fid, lid, season = int(fid), int(lid), int(season)
                home_id, away_id = int(home_id), int(away_id)
                pub = publications[lid]
                tz = pub["display_timezone"]
                kickoff_utc = kickoff.replace(tzinfo=timezone.utc) if kickoff.tzinfo is None \
                    else kickoff
                scope = (lid, season, tournament)
                record = existing.get(fid)

                if record and record.get("lifecycle") == "settled":
                    counts["skipped_settled"] += 1
                    continue

                market = kalshi.attach(home_id, away_id, kickoff_utc, tz, index)
                if market is not None:
                    market = kalshi.label_sides(market, home_id, away_id)
                    reason = ""
                else:
                    reason = ("unmapped_team"
                              if any(u["kalshi_team"] not in registry.teams
                                     and u["series_ticker"] == _series_of(registry, lid)
                                     for u in unmapped)
                              else "not_listed")
                market_block = _market_block(market, reason)
                counts[f"market_{market_block['state']}"] += 1

                if not full:
                    if record is None:
                        counts["skipped_no_record"] += 1
                        continue
                    # Only stamp a read time when something was actually quoted. A
                    # Fixture whose Winner Market is not listed has never been quoted,
                    # and saying so beats dating a read that did not happen — the same
                    # rule the `--full` payload follows.
                    payload = {"market": market_block,
                               "market_state": market_block["state"],
                               "quote_read_at": now.isoformat() if market else None}
                    if not dry_run:
                        pb.upsert_preview({**payload, "postgres_fixture_id": fid}, record)
                    counts["quotes_updated"] += 1
                    continue

                def scopes_for(team_id: int) -> list[tuple[dict, dict]]:
                    """Domestic always; the Fixture's own Tournament when it differs.

                    Both carry their scope label — CONTEXT.md's **Team Leaders**: "1 goal"
                    is true of a three-game campaign and false of a season, and only the
                    label distinguishes them. No threshold decides between them.
                    """
                    out = []
                    dom = domestic_scopes.get(int(profiles.get(team_id, {}).get("league_id") or 0))
                    if dom:
                        out.append((_scope_label(dom[0], dom[1], dom[2], comp_names)
                                    | {"is_fixture_tournament": dom == scope},
                                    leader_cache[dom]))
                    if scope != dom:
                        out.append((_scope_label(lid, season, tournament, comp_names)
                                    | {"is_fixture_tournament": True},
                                    leader_cache[scope]))
                    return out

                home_block = _team_block(home_id, profiles.get(home_id, {}),
                                         table_cache[scope], scopes_for(home_id))
                away_block = _team_block(away_id, profiles.get(away_id, {}),
                                         table_cache[scope], scopes_for(away_id))

                payload = {
                    "postgres_fixture_id": fid,
                    "publication": pub["id"],
                    "postgres_competition_id": lid,
                    "kickoff_utc": kickoff_utc.isoformat(),
                    "local_date": kalshi.local_match_date(kickoff_utc, tz).isoformat(),
                    "lifecycle": "upcoming",
                    "football_computed_at": now.isoformat(),
                    "quote_read_at": now.isoformat() if market else None,
                    "market_state": market_block["state"],
                    "home": home_block,
                    "away": away_block,
                    "market": market_block,
                }
                if not dry_run:
                    pb.upsert_preview(payload, record)
                counts["written"] += 1
    finally:
        pb.close()
    return dict(counts)


def _series_of(registry: kalshi.Registry, competition_id: int) -> Optional[str]:
    for series, comp in registry.series.items():
        if comp == competition_id:
            return series
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m football_blog.preview",
        description="Build Match Previews for the next 7 days (ADR 0040).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true",
                      help="recompute both halves (nightly).")
    mode.add_argument("--quotes", action="store_true",
                      help="re-read Winner Markets only (hourly); leaves the football "
                           "half and its timestamp untouched.")
    ap.add_argument("--dry-run", action="store_true",
                    help="assemble and report, write nothing.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    counts = build(full=not args.quotes, dry_run=args.dry_run)
    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if counts.get("unmapped_teams"):
        print(f"\n  ⚠ {counts['unmapped_teams']} Kalshi Team(s) unmapped — those Winner "
              f"Markets are refused, never half-attached. Propose entries with:\n"
              f"      uv run python -m football_blog.kalshi --propose")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
