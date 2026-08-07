"""Build **Match Bundles** and **Fixture Rows** — the blog's read-feeds (ADR 0044).

    uv run python -m football_blog.bundle --bundles   # nightly: one per Match Post
    uv run python -m football_blog.bundle --rows      # frequent: the moving window

The blog used to read these facts straight out of the **Published Store** over
`postgres.js`, which cannot run on Cloudflare Workers. Copying them into the **Editorial
Store** is what lets the site drop that dependency and render on demand. Nothing here
changes what the pipeline *writes*; this is one more **derived** read-path on top.

## Two collections, because there are two questions

    match_bundle  — "render THIS Fixture"   keyed by Fixture id
    fixture_row   — "which Fixtures?"       keyed by a date window

A **Match Bundle** is the full rendering payload for a played Fixture: timeline, both
squads, team stats, profiles, Venue, every Player named in it, and each side's last five
results. It measures ~30 KB, and there is one **only for a Fixture that has a Match
Post** — about 40 today, against 9,514 Fixtures in the store. Bundling everything would
put ~285 MB into a SQLite file currently under a megabyte, to serve pages that do not
exist: the only page that renders a bundle is a post page.

A **Fixture Row** is ~300 bytes — two crests, two names, a kickoff, a status, a score —
and exists for every Fixture in a moving window. That is what the ribbon and the
upcoming-week table read.

The invariant that keeps the split honest: **never ask `match_bundle` "which Fixtures?"**
A filter by date or team over that collection silently assumes a history that is
deliberately absent.

## Why the last-five strip is stored rather than queried

Recent form is the one feed that reads *arbitrary past Fixtures*, and answering it by
query is precisely what would have forced full history into `match_bundle`. It is ten
rows, and its cutoff — the subject Fixture's kickoff — is known while the bundle is being
built. Precomputing it costs nothing and removes the reason to store the other 9,474.

## Two cadences, and neither refreshes the store

`--bundles` runs nightly from `scripts/nightly.sh`, after `preview --full`, whose
`refresh_published_store()` is what actually delta-publishes the Competitions. `--rows`
runs every quarter hour and is a straight copy out of Postgres.

Neither delta-publishes, and that is deliberate: the store's own scores only move when the
nightly **Refresh** runs, so a `--rows` pass that published first would spend quota every
fifteen minutes to re-read what it already had. It follows `--quotes`' precedent.

The honest consequence, worth stating because the cadence invites the opposite reading: a
fresh `computed_at` on a Fixture Row means the row was copied recently, **not** that the
scoreline is current. In-progress scores are not available at all — the Published Store
has no live timeline (ADR 0020), and the provisional Live Mirror that does is never
published. That is why `fixture_row` has no `live_minute` field: nothing could fill it,
and a permanently-null field reads as "not in play" rather than "never measured".
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from football.status import FINAL

from .loader import load_post_bundles
from .pocketbase import PocketBaseClient
from .postgres import get_conn

log = logging.getLogger(__name__)

#: How far either side of now a **Fixture Row** exists. Wider than either consumer needs
#: alone — the ribbon wants one local day, the upcoming table Monday to Sunday — because
#: one window covering both is one job and the rows are small enough that slack is free.
WINDOW_BEFORE_DAYS = 3
WINDOW_AFTER_DAYS = 14

#: Rows in the last-five strip, per team.
FORM_COUNT = 5


# --------------------------------------------------------------------------- #
# Serialization                                                                #
# --------------------------------------------------------------------------- #
def _iso_z(value: datetime) -> str:
    """A datetime as ISO-8601 in UTC with a trailing `Z`.

    Load-bearing, and silently so. `fixture.date` is TIMESTAMP WITHOUT TIME ZONE holding
    naive UTC (ADR 0005); `loader._fixture` attaches UTC on the way out. Emitting the
    offset-less string that `isoformat()` would give for a naive value makes every
    `new Date(...)` in the browser read it as *local* time, which moves kickoff by hours
    with no error anywhere and a plausible-looking result.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    """Recursively convert a dataclass-derived structure into JSON-serializable values.

    `Decimal` is here because psycopg returns it for Postgres `numeric` columns — httpx's
    JSON encoder raises on it, so its absence would be a loud failure rather than a
    silent one, but only for Competitions that report the affected stat.
    """
    if isinstance(value, datetime):
        return _iso_z(value)
    if isinstance(value, date_cls):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def serialize_bundle(full) -> dict[str, Any]:
    """A `FullFixture` as the JSON the site renders.

    `dataclasses.asdict` gives the right shape for free: `types.py` is declared to match
    `blog/la-cancha/src/lib/types.ts` field for field, so the mapping is the absence of a
    mapping. Three corrections on top:

    - **`commentary` is dropped.** ESPN key-moment lines are drafting input for the model,
      they are third-party text, and the TypeScript `FullFixture` has no field for them
      (`types.py` says so at the field itself). The collection's list rule is public, so
      this is not only a size question.
    - **`players` keys are stringified**, because JSON object keys always are. Doing it
      here rather than letting the encoder do it keeps the Python and the wire shape the
      same thing.
    - **`events` are sorted by `event_index`.** The loader already returns them ordered,
      but the order is the provider's and nothing downstream re-derives it, so sorting on
      write means a future loader change cannot quietly scramble a timeline.
    """
    data = dataclasses.asdict(full)
    data.pop("commentary", None)
    data["events"] = sorted(data.get("events") or [], key=lambda e: e["event_index"])
    data["players"] = {str(k): v for k, v in (data.get("players") or {}).items()}
    return _json_safe(data)


# --------------------------------------------------------------------------- #
# Recent form — the last-five strip                                            #
# --------------------------------------------------------------------------- #
def recent_form(cur, team_id: int, before: datetime,
                count: int = FORM_COUNT) -> list[dict[str, Any]]:
    """This Team's last `count` **Final** Fixtures strictly before `before`, newest first.

    Mirrors `blog/la-cancha/src/lib/recentForm.ts`. `gf`/`ga` are written from *this*
    team's perspective and `outcome` derived from them, so nothing downstream has to know
    which side the team was on — that inversion is the only place a last-five strip can
    go wrong while still looking plausible.

    `strictly before` matters: a non-strict comparison puts the subject Fixture at the top
    of its own form guide, which reads as an ordinary row.

    Uses `football.status.FINAL` rather than a literal tuple. A cancelled or postponed
    Fixture carries no result, and an `AWD` carries a scoreline without a match having
    been played — neither belongs in a form guide.
    """
    cur.execute(
        "SELECT id, date, status, home_team_id, home_team_name, away_team_id, "
        "       away_team_name, home_goals, away_goals "
        "FROM fixture "
        "WHERE (home_team_id = %s OR away_team_id = %s) "
        "  AND status = ANY(%s) AND date < %s "
        "ORDER BY date DESC LIMIT %s",
        (team_id, team_id, list(FINAL), before.replace(tzinfo=None), count))

    rows = []
    for (fid, when, status, home_id, home_name,
         away_id, away_name, home_goals, away_goals) in cur.fetchall():
        is_home = home_id == team_id
        gf = (home_goals if is_home else away_goals) or 0
        ga = (away_goals if is_home else home_goals) or 0
        rows.append({
            "fixture_id": fid,
            "date": _iso_z(when),
            "status": status,
            "opponent_id": away_id if is_home else home_id,
            "opponent_name": away_name if is_home else home_name,
            "is_home": is_home,
            "gf": gf,
            "ga": ga,
            "outcome": "W" if gf > ga else ("L" if gf < ga else "D"),
        })
    return rows


# --------------------------------------------------------------------------- #
# Match Bundles                                                                #
# --------------------------------------------------------------------------- #
def bundle_payload(cur, full, publication_id: str,
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """One Match Bundle record, ready to upsert."""
    now = now or datetime.now(timezone.utc)
    fx = full.fixture
    return {
        **serialize_bundle(full),
        "postgres_fixture_id": fx.id,
        "publication": publication_id,
        "kickoff_utc": _iso_z(fx.date),
        "status": fx.status,
        "home_team_id": fx.home_team_id,
        "away_team_id": fx.away_team_id,
        "home_form": recent_form(cur, fx.home_team_id, fx.date),
        "away_form": recent_form(cur, fx.away_team_id, fx.date),
        "football_computed_at": _iso_z(now),
    }


def build_bundles(pb: PocketBaseClient, fixture_ids: Optional[Iterable[int]] = None,
                  dry_run: bool = False,
                  now: Optional[datetime] = None) -> dict[str, int]:
    """Write a Match Bundle for every Fixture that has a Match Post.

    The work list comes from `match_post`, not from Postgres, and that direction is the
    point: a bundle exists to render a post, so posts are what define the set. Passing
    `fixture_ids` narrows it — that is how `draft.py` writes a single bundle for a Fixture
    it has just drafted, rather than leaving it bundle-less until 04:00.
    """
    now = now or datetime.now(timezone.utc)
    counts: dict[str, int] = {"posts": 0, "written": 0, "skipped_unpublished": 0,
                              "missing_in_store": 0}

    publications = pb.published_publications()
    if not publications:
        print("No published Publication — nothing to bundle.")
        return counts

    if fixture_ids is None:
        wanted = sorted({int(p["postgres_fixture_id"]) for p in pb.list_posts()})
    else:
        wanted = sorted({int(f) for f in fixture_ids})
    counts["posts"] = len(wanted)
    if not wanted:
        print("No Match Post — nothing to bundle.")
        return counts

    bundles = load_post_bundles(wanted)
    existing = pb.list_bundles_by_fixture_ids(wanted)

    conn = get_conn()
    with conn.cursor() as cur:
        for fid in wanted:
            full = bundles.get(fid)
            if full is None:
                # A Match Post whose Fixture is not in the Published Store. Loud in the
                # log and counted, because it means the post renders nothing — but not
                # fatal, since the remaining bundles are still worth writing.
                log.warning("bundle: fixture %s has a Match Post but is not in the "
                            "Published Store", fid)
                counts["missing_in_store"] += 1
                continue
            publication = publications.get(full.fixture.league_id)
            if publication is None:
                counts["skipped_unpublished"] += 1
                continue
            payload = bundle_payload(cur, full, publication["id"], now)
            if dry_run:
                counts["written"] += 1
                continue
            pb.upsert_bundle(payload, existing.get(fid))
            counts["written"] += 1
    return counts


# --------------------------------------------------------------------------- #
# Fixture Rows                                                                 #
# --------------------------------------------------------------------------- #
def window_fixtures(cur, competition_ids: list[int], now: datetime) -> list[tuple]:
    """Every Fixture in the window, whatever its status.

    Unlike `preview.upcoming_fixtures` this does **not** filter on status: the ribbon
    shows finished and cancelled matches too, and a whitelist here would drop exactly the
    cards it exists to draw. (Where a status filter *is* wanted, whitelist — the store
    spells cancelled both `CANC` and `Canc`.)

    The crest comes from a LEFT JOIN, so a Team with no Profile row yields a null logo and
    a placeholder crest rather than dropping the Fixture out of the ribbon.
    """
    cur.execute(
        "SELECT f.id, f.date, f.league_id, f.league_name, "
        "       f.home_team_id, f.home_team_name, hp.logo, "
        "       f.away_team_id, f.away_team_name, ap.logo, "
        "       f.status, f.home_goals, f.away_goals, f.penalty_home, f.penalty_away "
        "FROM fixture f "
        "LEFT JOIN teamprofile hp ON hp.id = f.home_team_id "
        "LEFT JOIN teamprofile ap ON ap.id = f.away_team_id "
        "WHERE f.league_id = ANY(%s) AND f.date >= %s AND f.date < %s "
        "ORDER BY f.date",
        (competition_ids,
         (now - timedelta(days=WINDOW_BEFORE_DAYS)).replace(tzinfo=None),
         (now + timedelta(days=WINDOW_AFTER_DAYS)).replace(tzinfo=None)))
    return cur.fetchall()


def build_rows(pb: PocketBaseClient, dry_run: bool = False,
               now: Optional[datetime] = None) -> dict[str, int]:
    """Refresh the Fixture Row window, and delete what has fallen out of it.

    The delete is not tidiness. Without it the collection grows without bound and slowly
    becomes the 9,514-row table the whole split exists to avoid — while looking correct
    the entire time, because every record in it is individually valid.
    """
    now = now or datetime.now(timezone.utc)
    counts: dict[str, int] = {"in_window": 0, "written": 0, "deleted": 0}

    publications = pb.published_publications()
    if not publications:
        print("No published Publication — nothing to row.")
        return counts

    conn = get_conn()
    with conn.cursor() as cur:
        fixtures = window_fixtures(cur, sorted(publications), now)
    counts["in_window"] = len(fixtures)

    existing = {int(r["postgres_fixture_id"]): r for r in pb.list_rows()}
    stamp = _iso_z(now)

    for (fid, when, league_id, league_name, home_id, home_name, home_logo,
         away_id, away_name, away_logo, status,
         home_goals, away_goals, pen_home, pen_away) in fixtures:
        publication = publications.get(league_id)
        if publication is None:
            continue
        payload = {
            "postgres_fixture_id": fid,
            "publication": publication["id"],
            "kickoff_utc": _iso_z(when),
            "league_id": league_id,
            "league_name": league_name,
            "home_team_id": home_id,
            "home_team_name": home_name,
            "home_logo": home_logo,
            "away_team_id": away_id,
            "away_team_name": away_name,
            "away_logo": away_logo,
            "status": status,
            "home_goals": home_goals,
            "away_goals": away_goals,
            "penalty_home": pen_home,
            "penalty_away": pen_away,
            "computed_at": stamp,
        }
        if not dry_run:
            pb.upsert_row(payload, existing.get(fid))
        counts["written"] += 1

    keep = {int(f[0]) for f in fixtures}
    for fid, record in existing.items():
        if fid in keep:
            continue
        if not dry_run:
            pb.delete_row(record["id"])
        counts["deleted"] += 1

    return counts


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the blog's read-feeds in the Editorial Store (ADR 0044).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bundles", action="store_true",
                      help="one Match Bundle per Match Post (nightly)")
    mode.add_argument("--rows", action="store_true",
                      help="refresh the Fixture Row window (frequent)")
    ap.add_argument("--fixture-id", type=int, action="append", dest="fixture_ids",
                    help="with --bundles: only these Fixtures (repeatable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute everything, write nothing")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # One line per record is not progress, it is noise — and it buries the warnings this
    # module does emit, which are the reason the log is read at all.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    pb = PocketBaseClient()
    try:
        if args.bundles:
            counts = build_bundles(pb, args.fixture_ids, dry_run=args.dry_run)
        else:
            counts = build_rows(pb, dry_run=args.dry_run)
    finally:
        pb.close()

    print(("DRY RUN " if args.dry_run else "") +
          " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
