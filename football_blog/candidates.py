"""The Desk's board: which Fixtures could be written about, and what each one has.

    uv run python -m football_blog.candidates            # refresh, then list
    uv run python -m football_blog.candidates --no-refresh

A **Drafting Candidate** (CONTEXT.md) is a Fixture that is **Final** and whose
Competition has a **Publication**, and whose **Match Post** is absent or still a
`draft`. Those are exactly the two conditions `pipeline.assert_final` and
`pipeline.preflight` refuse on, and this module adds no third one.

## The board is wider than the Candidates

`list_board` returns a row for every **Final** Fixture in a Publication's Competition,
including those whose Match Post is already `published` — which by the glossary are
*not* Candidates any more. They stay on the board, marked, because "have I written
this one?" is a question the Desk should answer, and answering it by omission answers
it badly (ADR 0034). `BoardRow.is_candidate` is the glossary term; the board is the
display.

## Completeness is a signal, never a filter

Each row carries how many Events, Squad Entries, Team Match Stats and Commentary Lines
the Fixture has. None of them gates anything. `prompts/builder.py` does not fail on a
missing one — it writes `(team match stats not available)` and produces a thinner
**Narrative** — and for a Coverage-light Competition (ADR 0014) every match would be
filtered away by a completeness rule, when a timeline-only report is exactly what that
ADR set out to make possible. So the numbers are shown and a human judges.

## Why this refreshes before it lists

The **Published Store** learns of a new Final at 04:00 (`scripts/nightly.sh`) or when
the pipeline's own steps 1 and 3 run. So the match that ended two hours ago is not
here yet — and you cannot trigger the pipeline for a Fixture from a list it is not on.
This module breaks that circle by refreshing the frontier and delta-publishing across
every Publication's Competition before it reads.

It is **additive**. The per-Fixture pipeline still runs its own steps 1 and 3
unchanged, and the redundancy is deliberate: ADR 0029 fixed the ESPN stage *between*
the refresh and the publish so that one publish carries both the new Final and its
commentary. Letting the run skip those stages because this module just did them would
move the ESPN ingest after the publish and need a second one. The duplicated refresh
is seconds and mostly cache hits; see ADR 0034.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import FINAL_STATUSES
from .pocketbase import PocketBaseClient
from .postgres import get_conn

log = logging.getLogger(__name__)

#: How far back the board looks by default. Long enough to catch a weekend you did not
#: get to, short enough that the page is a work list rather than an archive.
DEFAULT_DAYS = 14


@dataclass
class BoardRow:
    """One Final Fixture in a Publication's Competition, and what it has."""
    fixture_id: int
    kickoff: datetime                 # tz-aware UTC
    status: str
    league_id: int
    league_name: str
    home_team_name: str
    away_team_name: str
    home_goals: Optional[int]
    away_goals: Optional[int]

    publication_slug: str
    publication_language: str
    publication_timezone: str
    publication_published: bool

    post_id: Optional[str] = None
    post_slug: Optional[str] = None
    post_status: Optional[str] = None      # None | "draft" | "published"

    n_events: int = 0
    n_squad: int = 0
    n_team_stats: int = 0
    n_commentary: int = 0

    @property
    def is_candidate(self) -> bool:
        """The CONTEXT.md definition, and the only place it is computed.

        Final and has a Publication are already true of every row on the board — the
        query cannot produce one for which they are not — so what remains is the Match
        Post condition. A published Match Post ends Candidacy; a draft does not.
        """
        return self.post_status != "published"

    @property
    def scoreline(self) -> str:
        h = self.home_goals if self.home_goals is not None else "?"
        a = self.away_goals if self.away_goals is not None else "?"
        return f"{self.home_team_name} {h}–{a} {self.away_team_name}"

    @property
    def has_commentary(self) -> bool:
        return self.n_commentary > 0


def _publications_by_competition(pb: PocketBaseClient) -> dict[int, dict]:
    """Every Publication, keyed by the Competition it covers.

    `only_published=False` on purpose: `preflight` requires a Publication *record*, not
    a Publication whose `published` gate is on. That gate governs the public site alone
    (CONTEXT.md), so a Competition can be drafted against long before anything about it
    is public — which is exactly the state a new Publication is in while you build it up.
    """
    return {
        int(p["postgres_competition_id"]): p
        for p in pb.list_publications(only_published=False)
    }


@dataclass
class BoardCompetition:
    """One Competition the board can be narrowed to, and how to label it."""
    league_id: int
    name: str                 # the Competition's own name — what the board's column shows
    publication_slug: str
    publication_display_name: str   # how the blog renders it, which may be another language
    brand_color: str


def list_competitions(pb: PocketBaseClient) -> list[BoardCompetition]:
    """Every Competition a Publication covers — the filter's full range.

    Built from the **Publications**, not from the rows currently on the board: a
    Competition with no Final in the window has to stay selectable, or the filter would
    hide the very case you want to check ("nothing in the World Cup this week?") behind
    a missing chip. Names come from the Competition, not the Publication's
    `display_name`, so the filter reads in the same words as the board's column.
    """
    publications = _publications_by_competition(pb)
    if not publications:
        return []

    with get_conn().cursor() as cur:
        cur.execute("SELECT id, name FROM competition WHERE id = ANY(%s)",
                    (list(publications),))
        names = {int(cid): name for cid, name in cur.fetchall()}

    out = [
        BoardCompetition(
            league_id=cid,
            # A Publication can exist before its Competition has been published to the
            # store; fall back to its display name rather than showing a bare id.
            name=names.get(cid) or pub.get("display_name") or str(cid),
            publication_slug=pub["slug"],
            publication_display_name=pub.get("display_name") or pub["slug"],
            brand_color=pub.get("brand_color") or "",
        )
        for cid, pub in publications.items()
    ]
    return sorted(out, key=lambda c: c.name)


def _signal_counts(fixture_ids: list[int]) -> dict[int, dict[str, int]]:
    """Per-Fixture row counts for the four things that thin a Narrative.

    Four batched queries, in the style of `loader.load_post_bundles` — never one query
    per Fixture, because the board is rendered on every page load.
    """
    counts: dict[int, dict[str, int]] = {
        fid: {"events": 0, "squad": 0, "team_stats": 0, "commentary": 0}
        for fid in fixture_ids
    }
    if not fixture_ids:
        return counts

    queries = [
        ("events", "SELECT fixture_id, count(*) FROM event "
                   "WHERE fixture_id = ANY(%s) GROUP BY fixture_id"),
        ("squad", "SELECT fixture_id, count(*) FROM squadentry "
                  "WHERE fixture_id = ANY(%s) GROUP BY fixture_id"),
        ("team_stats", "SELECT fixture_id, count(*) FROM teammatchstat "
                       "WHERE fixture_id = ANY(%s) GROUP BY fixture_id"),
        # Commentary is reached through narrated_match's optional Fixture bridge — a
        # Narrated Match is ESPN's unit and is not Competition-scoped (CONTEXT.md), so
        # this is a left-ish join by construction: absent simply means not ingested.
        ("commentary", "SELECT nm.fixture_id, count(*) FROM commentary_line cl "
                       "JOIN narrated_match nm ON nm.game_id = cl.game_id "
                       "WHERE nm.fixture_id = ANY(%s) GROUP BY nm.fixture_id"),
    ]
    with get_conn().cursor() as cur:
        for key, sql in queries:
            cur.execute(sql, (fixture_ids,))
            for fid, n in cur.fetchall():
                if fid in counts:
                    counts[fid][key] = int(n)
    return counts


def list_board(
    pb: PocketBaseClient,
    *,
    since: Optional[datetime] = None,
    league_id: Optional[int] = None,
    fixture_ids: Optional[list[int]] = None,
) -> list[BoardRow]:
    """Every Final Fixture in a Publication's Competition, newest kickoff first.

    `fixture_ids` narrows to specific Fixtures and drops the date window — the Desk uses
    it to resolve one Fixture reached by URL without materialising a year of board to
    find it. The two gates still apply: an id that is not Final, or whose Competition has
    no Publication, comes back absent rather than as a row.
    """
    publications = _publications_by_competition(pb)
    if league_id is not None:
        publications = {k: v for k, v in publications.items() if k == league_id}
    if not publications:
        return []

    if fixture_ids is not None:
        if not fixture_ids:
            return []
        where = "id = ANY(%s)"
        params: tuple = (list(publications), list(FINAL_STATUSES), list(fixture_ids))
    else:
        since = since or (datetime.now(timezone.utc) - timedelta(days=DEFAULT_DAYS))
        where = "date >= %s"
        params = (list(publications), list(FINAL_STATUSES),
                  since.strftime("%Y-%m-%d %H:%M:%S"))

    with get_conn().cursor() as cur:
        cur.execute(
            f"""
            SELECT id, date, status, league_id, league_name,
                   home_team_name, away_team_name, home_goals, away_goals
            FROM fixture
            WHERE league_id = ANY(%s)
              AND status = ANY(%s)
              AND {where}
            ORDER BY date DESC
            """,
            params,
        )
        fixtures = cur.fetchall()

    if not fixtures:
        return []

    fixture_ids = [int(r[0]) for r in fixtures]
    counts = _signal_counts(fixture_ids)
    posts = pb.list_posts_by_fixture_ids(fixture_ids)

    rows: list[BoardRow] = []
    for (fid, date, status, lid, lname, home, away, hg, ag) in fixtures:
        pub = publications[int(lid)]
        post = posts.get(int(fid))
        c = counts[int(fid)]
        rows.append(BoardRow(
            fixture_id=int(fid),
            # fixture.date is TIMESTAMP WITHOUT TIME ZONE, naive UTC per ADR 0005 —
            # the same attachment loader.py makes, for the same reason.
            kickoff=date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date,
            status=status,
            league_id=int(lid),
            league_name=lname,
            home_team_name=home,
            away_team_name=away,
            home_goals=hg,
            away_goals=ag,
            publication_slug=pub["slug"],
            publication_language=pub["default_language"],
            publication_timezone=pub["display_timezone"],
            publication_published=bool(pub.get("published")),
            post_id=post["id"] if post else None,
            post_slug=post.get("slug") if post else None,
            post_status=post.get("status") if post else None,
            n_events=c["events"],
            n_squad=c["squad"],
            n_team_stats=c["team_stats"],
            n_commentary=c["commentary"],
        ))
    return rows


def refresh_board(pb: PocketBaseClient) -> list[int]:
    """Pull every Publication's Competition frontier into the Published Store.

    Returns the Competition ids covered. Imports are local: `list_board` must stay
    usable from a web request without dragging in the refresh machinery or `psycopg`'s
    publish path.
    """
    import refresh
    from football.publish import pg as publish_pg

    competition_ids = sorted(_publications_by_competition(pb))
    if not competition_ids:
        print("No Publications — nothing to refresh.")
        return []

    print(f"Refreshing the frontier for {len(competition_ids)} Competition(s): "
          f"{', '.join(map(str, competition_ids))}")
    try:
        refresh.main(["--no-rebuild", "--only", *map(str, competition_ids)])
    except SystemExit as e:
        # Same stance as pipeline.stage_refresh: the cache stays internally consistent
        # after a partial run (ADR 0018), so a hit quota thins the board rather than
        # emptying it. Warn and go on — a stale board is more useful than none.
        if e.code:
            print(f"⚠ Refresh exited non-zero (code {e.code}) — the board will show "
                  f"whatever was collected.")

    print("Delta-publishing to the Published Store…")
    counts = publish_pg.delta_publish(competition_ids)
    print(f"  {sum(counts.values()):,} rows across {len(counts)} tables")
    return competition_ids


def _print_board(rows: list[BoardRow]) -> None:
    if not rows:
        print("\nNothing on the board. Either no Publication covers a Competition with a "
              "recent Final, or the window is too narrow (see --days).")
        return

    candidates = [r for r in rows if r.is_candidate]
    print(f"\n{len(rows)} Final(s) on the board · {len(candidates)} Drafting Candidate(s)\n")
    print(f"  {'FIXTURE':>9}  {'KICKOFF':<16}  {'MATCH':<44}  "
          f"{'EV':>3} {'SQ':>3} {'TS':>3} {'CM':>4}  POST")
    for r in rows:
        post = {None: "—", "draft": "draft", "published": "PUBLISHED"}.get(
            r.post_status, r.post_status or "—")
        match = r.scoreline
        if len(match) > 44:
            match = match[:41] + "…"
        print(f"  {r.fixture_id:>9}  {r.kickoff:%Y-%m-%d %H:%M}  {match:<44}  "
              f"{r.n_events:>3} {r.n_squad:>3} {r.n_team_stats:>3} {r.n_commentary:>4}  {post}")
    print("\n  EV/SQ/TS/CM = Events / Squad Entries / Team Match Stats / Commentary Lines.")
    print("  These are signals, not gates — a thin row still drafts, just more briefly.")
    print("\n  Draft one with:")
    print("    uv run python -m football_blog.pipeline --fixture-id <FIXTURE> --espn-id <ESPN>")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m football_blog.candidates",
        description="List the Fixtures that could be written about (ADR 0034).",
    )
    ap.add_argument("--no-refresh", action="store_true",
                    help="list from the Published Store as it stands; skip the frontier "
                         "refresh and delta publish (fast, spends no quota).")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"how far back to look (default {DEFAULT_DAYS}).")
    ap.add_argument("--league-id", type=int,
                    help="restrict the board to one Competition.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    pb = PocketBaseClient()
    try:
        if not args.no_refresh:
            refresh_board(pb)
        rows = list_board(
            pb,
            since=datetime.now(timezone.utc) - timedelta(days=args.days),
            league_id=args.league_id,
        )
        _print_board(rows)
    finally:
        pb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
