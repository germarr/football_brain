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
Final, so `--full` runs nightly. The market half is a handful of unauthenticated GETs and
moves continuously, so `--quotes` runs hourly and touches only the per-**Exchange** market
fields — `market_kalshi` / `market_polymarket` and their `market_state_*` and
`quote_read_at_*`. The record carries **two timestamps** because its halves are true as of
two different moments; one `updated` field would misdate whichever it did not describe.

## Two Exchanges, never merged

A Match Preview carries one **Winner Market** per Exchange (ADR 0043). They are resolved
independently — Kalshi on the local match date, Polymarket on the exact kickoff instant —
and a failure on one must not cost the card the other, so the Polymarket half is wrapped
and degrades to a Kalshi-only card rather than taking the run down. Nothing here averages,
blends or picks between them: that is the reader's job, and the `/previews` board draws
both.

## One half skips, the other never does

`--full` compares the football half before writing it and drops it from the payload when a
table position and a Team Leader have not moved — which, on a night with no results for
that Competition, is all of them. `football_computed_at` rides with the half it dates, so
an unchanged card keeps the stamp of the run that last actually changed it.

The market half is **always** written, and the asymmetry is the point rather than an
oversight. A **Quote**'s `quote_read_at` is rendered on the card, so skipping would freeze
a displayed freshness claim and understate how current a forecast is — the misdating this
record carries two timestamps to avoid. It would also buy nothing: measured across one
hourly interval, all 33 quoted markets had moved, because a block carries `volume` and
`open_interest` that tick with every trade.

## The freeze

At kickoff a Match Preview stops being rewritten, and `--full` and `--quotes` both run the
freeze pass before anything else. This is not tidiness — but not for the reason once given
here. Both **Exchanges** serve their history after settlement, so what the market thought
an hour before kickoff *is* recoverable: `football_blog.track` recovers it, and Kalshi's
candlesticks return hourly bid/ask OHLC rather than coming back empty (ADR 0043 corrects
ADR 0040 on this).

What is not recoverable is the record's meaning. A **Quote** does not stop at kickoff; it
converges on the result and settles at 1 and 0. Re-running a settled Fixture would
overwrite a *forecast* with an *outcome*, both spelled as three percentages, and nothing
on the card would say which had been stored. So the last Quote read before kickoff is the
one that stays, and `settle_preview` patches `lifecycle` alone so the freeze cannot become
an occasion to overwrite it.

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

from . import kalshi, polymarket
from .pocketbase import PocketBaseClient, unchanged
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


def _polymarket_block(market: Optional[polymarket.WinnerMarket], reason: str) -> dict:
    """The same shape for the second **Exchange**, and deliberately its own function.

    A shared builder would have to be told which Exchange it is building for, and the
    two blocks do not carry the same facts: Kalshi's identifiers are a series and an
    event ticker, Polymarket's is an event slug, and only Polymarket's quotes carry a
    volume unit (its volume is dollars, Kalshi's is contracts — ADR 0043).
    """
    if market is None:
        return {"state": "absent", "reason": reason, "source": "polymarket"}
    return {
        "state": market.state,
        "source": "polymarket",
        "league": market.league,
        "event_slug": market.event_slug,
        # Polymarket resolves on "the first 90 minutes of regular play plus stoppage
        # time" — Kalshi's rule word for word, which is what makes the two comparable.
        "settles_on": "regulation",
        "overround": market.overround,
        "outcomes": market.outcomes,
    }


# --------------------------------------------------------------------------- #
# The run                                                                      #
# --------------------------------------------------------------------------- #
def _publications(pb: PocketBaseClient) -> dict[int, dict]:
    """Published Publications only, by Competition (ADR 0040).

    Thin alias for the client method, which is where the definition now lives — the Match
    Bundle builder needs the same set for the same reason, and two copies of "which
    Competitions are public" is exactly the kind of agreeing-until-it-doesn't duplication
    `football/status.py` was written to end.
    """
    return pb.published_publications()


def refresh_published_store(pb: PocketBaseClient) -> list[int]:
    """Delta-publish every published Publication's Competition before reading it.

    Without this the builder reads a store nothing has updated. `scripts/nightly.sh` runs
    a full **Refresh** — which updates the raw cache and `football.db` — and then calls
    `football.publish.pg --heal-venues`, which returns *before* publishing anything. No
    wholesale publish runs on any schedule. So a Fixture whose kickoff moved, or a
    knockout round drawn after a group stage ended, would sit in the cache and never reach
    the Published Store (ADR 0028's amendment is what makes the delta carry it; this is
    what makes the delta run).

    Same shape and same stance as `candidates.refresh_board`: additive, seconds, mostly
    cache hits, and a non-zero exit thins the board rather than emptying it. Imports are
    local so `build` stays importable from a web request without dragging in the publish
    path.
    """
    from football.publish import pg as publish_pg

    competition_ids = sorted(_publications(pb))
    if not competition_ids:
        return []
    try:
        publish_pg.delta_publish(competition_ids)
    except SystemExit as e:
        # A delta needs a prior wholesale publish to baseline against. If that has never
        # happened the right answer is a stale board, not a failed nightly.
        if e.code:
            print(f"⚠ Delta publish exited non-zero (code {e.code}) — previewing whatever "
                  f"the Published Store already holds.")
    return competition_ids


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


def _polymarket_index(registry: polymarket.Registry) -> tuple[dict, list[dict]]:
    """`{(team pair, kickoff instant): WinnerMarket}` across every covered league.

    Keyed on the **instant**, where Kalshi's index is keyed on the local match date —
    two different rules for the same question, which is why the two indexes are built
    separately and never merged (ADR 0043).
    """
    client = polymarket.PolymarketClient()
    index, unmapped = {}, []
    try:
        for league in registry.league_keys:
            series_slug = registry.series_slug(league)
            if not series_slug:
                continue
            events = client.events(series_slug)
            idx, un = polymarket.index_by_team_pair(events, league, registry)
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


def build(full: bool = True, dry_run: bool = False, publish: bool = True,
          now: Optional[datetime] = None) -> dict[str, int]:
    """One run. `full` recomputes both halves; otherwise only the market half moves."""
    now = now or datetime.now(timezone.utc)
    counts = defaultdict(int)

    pb = PocketBaseClient()
    try:
        # Only on a full run: `--quotes` touches nothing the store feeds, so making the
        # hourly job delta-publish would spend a re-parse every hour for no new fact.
        if full and publish and not dry_run:
            refresh_published_store(pb)

        counts["frozen"] = 0 if dry_run else freeze_kicked_off(pb, now)

        publications = _publications(pb)
        if not publications:
            print("No published Publication — nothing to preview.")
            return dict(counts)

        registry = kalshi.load_registry()
        index, unmapped = _market_index(registry)
        counts["unmapped_teams"] = len({u["kalshi_team"] for u in unmapped})

        # The second Exchange is resolved independently and is allowed to fail on its
        # own: a Polymarket outage must not cost the card its Kalshi half, and the two
        # Winner Markets are independent facts (CONTEXT.md).
        pm_registry, pm_index, pm_unmapped = None, {}, []
        try:
            pm_registry = polymarket.load_registry()
            pm_index, pm_unmapped = _polymarket_index(pm_registry)
            counts["unmapped_teams_polymarket"] = len(
                {(u["league"], u["polymarket_team"]) for u in pm_unmapped})
        except FileNotFoundError:
            log.warning("No Polymarket registry — building Kalshi-only previews.")
        except Exception as exc:                       # noqa: BLE001 — see above
            log.warning("Polymarket unavailable (%s) — building Kalshi-only previews.", exc)

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

                pm_market = (polymarket.attach(home_id, away_id, kickoff_utc, pm_index)
                             if pm_index else None)
                if pm_market is not None:
                    pm_market = polymarket.label_sides(pm_market, home_id, away_id)
                    pm_reason = ""
                elif pm_registry is None:
                    pm_reason = "not_covered"
                elif not pm_registry.covers(lid):
                    # Permanent, and nothing a human can close — a different thing from
                    # a market this Exchange has not opened yet (ADR 0043).
                    pm_reason = "not_covered"
                elif any(u["league"] in _pm_leagues_of(pm_registry, lid)
                         for u in pm_unmapped):
                    pm_reason = "unmapped_team"
                else:
                    pm_reason = "not_listed"
                pm_block = _polymarket_block(pm_market, pm_reason)
                counts[f"polymarket_{pm_block['state']}"] += 1

                if not full:
                    if record is None:
                        counts["skipped_no_record"] += 1
                        continue
                    # Only stamp a read time when something was actually quoted. A
                    # Fixture whose Winner Market is not listed has never been quoted,
                    # and saying so beats dating a read that did not happen — the same
                    # rule the `--full` payload follows. Each Exchange is stamped
                    # separately: one may be quoted while the other is not listed.
                    payload = {"market_kalshi": market_block,
                               "market_state_kalshi": market_block["state"],
                               "quote_read_at_kalshi": now.isoformat() if market else None,
                               "market_polymarket": pm_block,
                               "market_state_polymarket": pm_block["state"],
                               "quote_read_at_polymarket":
                                   now.isoformat() if pm_market else None}
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

                # The football half: a table position and a Team Leader either moved or
                # they did not, and on a night with no results for this Competition they
                # did not. Kept as its own dict so it can be compared and dropped.
                football = {
                    "publication": pub["id"],
                    "postgres_competition_id": lid,
                    "kickoff_utc": kickoff_utc.isoformat(),
                    "local_date": kalshi.local_match_date(kickoff_utc, tz).isoformat(),
                    "lifecycle": "upcoming",
                    "home": home_block,
                    "away": away_block,
                }
                # The market half is *always* written, and that asymmetry is deliberate.
                # A **Quote** is a point-in-time read whose `quote_read_at` is rendered on
                # the card, so skipping the write would freeze a displayed freshness claim
                # and quietly understate how current a forecast is — exactly the misdating
                # ADR 0040 gave the record two timestamps to avoid. It would also buy
                # nothing: measured over one hourly interval, all 33 quoted markets had
                # moved, because a block carries `volume` and `open_interest` that tick
                # with every trade.
                markets = {
                    "quote_read_at_kalshi": now.isoformat() if market else None,
                    "market_state_kalshi": market_block["state"],
                    "quote_read_at_polymarket": now.isoformat() if pm_market else None,
                    "market_state_polymarket": pm_block["state"],
                    "market_kalshi": market_block,
                    "market_polymarket": pm_block,
                }

                payload = {"postgres_fixture_id": fid, **markets}
                if unchanged(football, record):
                    counts["football_unchanged"] += 1
                else:
                    # `football_computed_at` rides with the half it dates, so an unchanged
                    # card keeps the stamp of the run that last actually changed it.
                    payload |= football | {"football_computed_at": now.isoformat()}
                    counts["football_written"] += 1

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


def _pm_leagues_of(registry: polymarket.Registry, competition_id: int) -> set[str]:
    """Every Polymarket league key mapping to this Competition — usually one.

    A set rather than a value because the mapping is not guaranteed one-to-one: the
    same Competition could be listed under two league keys, and picking the first
    would silently ignore refusals in the other.
    """
    return {k for k in registry.league_keys
            if registry.competition_for(k) == competition_id}


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
    ap.add_argument("--no-publish", action="store_true",
                    help="skip the delta publish that precedes a --full run; preview "
                         "whatever the Published Store already holds.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    counts = build(full=not args.quotes, dry_run=args.dry_run,
                   publish=not args.no_publish)
    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if counts.get("unmapped_teams"):
        print(f"\n  ⚠ {counts['unmapped_teams']} Kalshi Team(s) unmapped — those Winner "
              f"Markets are refused, never half-attached. Propose entries with:\n"
              f"      uv run python -m football_blog.kalshi --propose")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
