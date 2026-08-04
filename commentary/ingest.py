"""CLI: ingest an ESPN match into `data/commentary.db` (ADR 0026).

    python -m commentary.ingest https://www.espn.com/soccer/commentary/_/gameId/760514
    python -m commentary.ingest 760514 --fixture-id 1585131
    python -m commentary.ingest 760514 --reclassify
    python -m commentary.ingest --stats
    uv run python -m commentary.ingest 401877045 --fixture-id 1550894 --force-link

The pipeline, and the decision behind each step:

1. **URL in, game id out.** ESPN's game id is the Narrated Match's identity and
   the store's key, but the operator has a URL in the address bar, so accept either.

2. **Skip if already stored** (unless `--reclassify`). The store is the system of
   record with no backup and no classify cache, so the DB *is* the cache: a re-run
   costs nothing and cannot clobber labels by accident. This is what makes step 4's
   "refused because live — re-run later" practical.

3. **Route by Narration Coverage.** `narrative` needs the join and the model;
   `events_only` needs neither and costs nothing. Half the feed is each.

4. **Refuse unless ESPN says the match is complete**, testing ESPN's computed
   `completed` boolean rather than its display string. Freezing a match at 60'
   would store partial commentary that looks whole — worse than empty, because
   nothing downstream could tell. ESPN, not football.db, is authoritative about
   ESPN's own feed: football.db is eventually consistent and has been observed
   saying `2H` for a match ESPN already called Full Time.

5. **Verify `--fixture-id` if given**, read-only, refusing on any disagreement.
   Most narrated matches have no Fixture at all; the link is an optional bridge.
"""
from __future__ import annotations

import argparse
import re
import sys

from .espn import fetch_summary
from .events_only import build_events_only
from .fixture_link import FixtureMismatch, verify_fixture, verify_fixture_any
from .join import build_match, match_meta, match_status, narration_coverage
from .store import connect, delete_match, has_match, write_match

USAGE = """Ingest an ESPN match into data/commentary.db (ADR 0026).

  python -m commentary.ingest https://www.espn.com/soccer/commentary/_/gameId/760514
  python -m commentary.ingest 760514                    # a bare gameId also works
  python -m commentary.ingest 760514 --fixture-id 1585131
  python -m commentary.ingest 760514 --reclassify
  python -m commentary.ingest --stats

Notes:
  * Only matches ESPN reports COMPLETE are ingested; a live one is refused, so
    just re-run it after full time.
  * A match already stored is skipped for free — re-running a sweep is safe.
  * Most matches need no --fixture-id: ESPN narrates many competitions we do
    not collect, and those have no Fixture to link to.
  * Half of ESPN's matches arrive pre-typed and cost no API calls at all. The
    other half are classified with Haiku and need ANTHROPIC_API_KEY (env or .env).
"""

GAME_ID_PATTERNS = (
    re.compile(r"gameId[/=](\d+)", re.IGNORECASE),  # .../gameId/760514 or ?gameId=760514
    re.compile(r"^(\d+)$"),  # a bare id
)


def parse_game_id(value: str) -> str:
    """Pull the game id out of an ESPN URL, or accept a bare id.

    Both `/soccer/commentary/_/gameId/760514` and `/soccer/match/_/gameId/760514`
    work, with or without query strings.
    """
    text = (value or "").strip()
    for pattern in GAME_ID_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(1)
    raise SystemExit(
        f"could not find a gameId in {value!r}. Pass an ESPN URL such as "
        f"https://www.espn.com/soccer/commentary/_/gameId/760514, or the bare id."
    )


def _narrative_doc(payload: dict, game_id: str) -> dict:
    """Join + classify, then fold field_position back onto each line.

    `build_synthesis` returns the seven classified fields; the coordinates live on
    the joined keyEvent. They are married here rather than inside `build_synthesis`
    so that the JSON-emitting spike keeps its existing output contract.
    """
    import anthropic  # imported late: events_only ingest needs no API key at all

    from .classify import build_synthesis, load_api_key

    match = build_match(payload, game_id)
    client = anthropic.Anthropic(api_key=load_api_key())
    doc = build_synthesis(client, match)

    positions = {
        row["sequence"]: (row["key_event"] or {}).get("field_position")
        for row in match["commentary"]
    }
    for event in doc["events"]:
        event["field_position"] = positions.get(event["sequence"])

    doc["status"] = match.get("status")
    doc["status_name"] = match.get("status_name")
    doc["completed"] = match.get("completed")
    doc["join"] = match["join"]
    return doc


def _stats(connection) -> int:
    rows = connection.execute(
        "SELECT narration_coverage, COUNT(*) n, SUM(fixture_id IS NOT NULL) linked "
        "FROM narrated_match GROUP BY narration_coverage"
    ).fetchall()
    if not rows:
        print("commentary.db is empty.")
        return 0
    print(f"{'coverage':<14}{'matches':>9}{'linked':>8}")
    for row in rows:
        print(f"{row['narration_coverage']:<14}{row['n']:>9}{row['linked'] or 0:>8}")
    lines = connection.execute(
        "SELECT source, COUNT(*) n FROM commentary_line GROUP BY source"
    ).fetchall()
    print()
    print(f"{'source':<14}{'lines':>9}")
    for row in lines:
        print(f"{row['source']:<14}{row['n']:>9}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="commentary.ingest",
        description=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "url", nargs="?", help="ESPN commentary URL, or a bare gameId"
    )
    parser.add_argument(
        "--fixture-id",
        type=int,
        help="link to an API-Football Fixture. Verified against football.db "
        "(read-only) on kickoff + teams; refuses on any disagreement. Omit for "
        "competitions we do not collect — which is most of them.",
    )
    parser.add_argument(
        "--force-link",
        action="store_true",
        help="accept a --fixture-id whose team names disagree with ESPN's (e.g. "
        "ESPN's 'United States' vs football.db's 'USA'). The kickoffs must still "
        "agree within 15 min, and if they are not identical then at least one "
        "team name must still match exactly — this flag cannot waive both. You "
        "are asserting these are the same match; nothing downstream can detect "
        "it if you are wrong.",
    )
    parser.add_argument(
        "--verify-fallback-pg",
        action="store_true",
        help="if football.db cannot confirm --fixture-id, try the Published Store "
        "(Postgres) too, and refuse only if both disagree. For a match played since "
        "the last nightly rebuild, football.db may not know it yet. This is what "
        "football_blog.pipeline passes; it does not waive any check.",
    )
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="re-ingest a match already stored: deletes its lines and re-runs the "
        "classifier. Costs money and will not reproduce the old labels exactly.",
    )
    parser.add_argument("--refresh", action="store_true", help="bypass the raw cache")
    parser.add_argument(
        "--stats", action="store_true", help="summarise the store and exit"
    )
    args = parser.parse_args(argv)

    connection = connect()

    if args.stats:
        return _stats(connection)
    if not args.url:
        parser.error("a URL or gameId is required (or --stats)")

    game_id = parse_game_id(args.url)

    if has_match(connection, game_id):
        if not args.reclassify:
            print(
                f"{game_id} is already stored — skipping (0 requests, labels "
                f"untouched). Use --reclassify to overwrite it.",
                file=sys.stderr,
            )
            return 0
        removed = delete_match(connection, game_id)
        print(f"--reclassify: dropped {removed} existing lines for {game_id}", file=sys.stderr)

    payload = fetch_summary(game_id, refresh=args.refresh)
    coverage = narration_coverage(payload)

    # Gate BEFORE classifying — refusing after paying the model would be silly.
    status = match_status(payload)
    if not status["completed"]:
        print(
            f"REFUSED: ESPN reports {game_id} as {status['status']!r} "
            f"({status['status_name']}), not complete. Commentary is ingested only "
            f"once the match is over — a partial ingest looks whole and nothing "
            f"downstream could detect it. Re-run this after full time.",
            file=sys.stderr,
        )
        return 1

    # Verify the link BEFORE classifying too: verification is free, the model is
    # not. Refusing a typo'd fixture id after paying to label 88 lines would be
    # a self-inflicted wound.
    fixture_id = None
    if args.fixture_id is not None:
        meta = match_meta(payload, game_id)
        check = verify_fixture_any if args.verify_fallback_pg else verify_fixture
        try:
            fixture = check(args.fixture_id, meta, force=args.force_link)
        except FixtureMismatch as error:
            print(f"REFUSED: {error}", file=sys.stderr)
            return 1
        fixture_id = args.fixture_id
        if fixture["name_mismatch"]:
            # Forced. Say so plainly rather than reporting a clean "verified" —
            # the kickoff was checked, the names were not.
            print(
                f"fixture {fixture_id} FORCE-LINKED on kickoff alone "
                f"({fixture['date']}); team names were NOT verified:\n"
                f"    ESPN     {meta['home']['team']} v {meta['away']['team']}\n"
                f"    fixture  {fixture['home_team_name']} v {fixture['away_team_name']} "
                f"({fixture['league_name']})",
                file=sys.stderr,
            )
        else:
            print(
                f"fixture {fixture_id} verified against {fixture['verified_against']}: "
                f"{fixture['home_team_name']} v {fixture['away_team_name']} "
                f"({fixture['league_name']}) @ {fixture['date']}",
                file=sys.stderr,
            )

    if coverage == "events_only":
        doc = build_events_only(payload, game_id)
        print(
            f"{game_id}: events_only feed — {len(doc['events'])} lines, every "
            f"Category asserted by ESPN (no model, 0 requests)",
            file=sys.stderr,
        )
    else:
        doc = _narrative_doc(payload, game_id)

    written = write_match(
        connection,
        doc,
        narration_coverage=coverage,
        fixture_id=fixture_id,
        source_url=args.url if args.url != game_id else None,
    )

    print(
        f"stored {game_id}: {doc['home']['team']} {doc['home']['score']}–"
        f"{doc['away']['score']} {doc['away']['team']} ({doc['league']}) — "
        f"{written} lines, coverage={coverage}, "
        f"fixture_id={fixture_id if fixture_id else 'none'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
