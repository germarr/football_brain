"""One command: a Fixture id and an ESPN game id in, a PocketBase draft out.

    uv run python -m football_blog.pipeline --fixture-id 1591866 --espn-id 760514

Everything here already existed as a separate tool; what was missing was the
order to run them in and the checks between. The pipeline is a composition, and
its only real design decision is where the ESPN stage sits:

    0. Preflight   resolve the Fixture's Competition, confirm it is onboarded in
                   PocketBase, confirm every credential the later stages need —
                   before anything writes.
    1. Cache       Refresh the Competition's current-Season frontier (ADR 0018)
                   with --only and --no-rebuild: force-refresh the fixture list
                   (a cache-first read would freeze it) and collect per-fixture
                   data for the newly-Final matches. Seconds; deliberately skips
                   the ~13-minute football.db rebuild.
    2. ESPN        Ingest the game id into data/commentary.db (ADR 0026), linked
                   to the Fixture. Refuses unless ESPN says the match is complete.
                   Already-stored is a free skip.
    3. Publish     One delta publish (ADR 0028) to the Postgres Published Store.
    4. Draft       Load the bundle from Postgres, prompt Claude, upsert the
                   `match_post` record as status='draft'.

**ESPN sits between the refresh and the publish on purpose.** `publish_pg`'s
delta full-copies the commentary tables on every apply — even when zero Fixtures
changed (`_apply_delta` step 4) — so a single publish carries both the new Final
*and* its commentary. Ingesting after the publish instead would need a second
publish to move the commentary across, for no gain.

Publishing the post is still a manual gate. This writes `status='draft'` and
`published_at=null`, and nothing else does otherwise: review it at
http://127.0.0.1:8090/_/, fix whatever the model got wrong, then flip it.

## Flags

  --fixture-id ID     required; the API-Football Fixture.
  --espn-id ID        ESPN game id, or the full commentary URL. Omit it to skip
                      stage 2 entirely — many Competitions ESPN does not narrate,
                      and the drafter works without commentary (it just loses the
                      colour). The omission is announced, never silent.
  --dry-run           stage 4 prints the prompt and writes nothing — no Claude
                      call, no PocketBase record. Stages 1-3 still run: they only
                      refresh the cache and Postgres, which the nightly cron does
                      anyway, and stage 4 cannot build a prompt without them.
  --skip-refresh      skip stage 1 (publish and draft from the cache as it is).
  --skip-publish      skip stage 3 (draft from Postgres as it is).
  --force-link        waive ESPN-vs-ours *team name* disagreement in stage 2. The
                      kickoff is still checked, and this flag can never waive it. A
                      match delayed past the 15-min tolerance links on its own, without
                      any flag, when both team names agree (ADR 0038).
  --instruction TEXT  a one-off editorial steer for this report ("lead with the
                      comeback"). It joins the *user* prompt after the facts, not
                      the system prompt: it is about this match, not this
                      Publication's voice. It shapes emphasis and angle only — the
                      prompt says so in words, because rule 1 is "never invent data"
                      and a steer is the obvious way to erode it. Recorded on the
                      Match Post, since a Narrative cannot be regenerated and an
                      unrecorded input is one nothing can recover (ADR 0034).
  --reclassify        re-ingest an ESPN match already stored. Costs model calls
                      and will not reproduce the old labels exactly.
  --redraft           overwrite an already-published match_post. See below.
  --verbose / -v      DEBUG-level logs.

## What it refuses to do

- Draft a Fixture whose Competition has no `publication` record in PocketBase — run
  `python -m football_blog.onboard --league-id <id>` first, which is interactive
  and asks for the language, timezone and slug this needs.
- Draft a Fixture that is not Final. A narrative of an unfinished match is worse
  than no narrative, because nothing downstream can tell.
- Redraft a Fixture whose Match Post is already **published**. Its Narrative has
  been edited by hand and the Editorial Store has no rebuild path, so overwriting
  it is unrecoverable. Drafts are overwritten freely — that is what a draft is.
  `--redraft` waives this; nothing else does.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional

import requests

import refresh
from commentary import ingest as commentary_ingest
from football.publish import pg as publish_pg
from football.config import DB_PATH as FOOTBALL_DB

from . import FINAL_STATUSES, draft
from .config import require_env
from .pocketbase import PocketBaseClient
from .postgres import get_conn

log = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """A stage refused. The message is written for the operator, not the log."""


def _banner(step: int, total: int, title: str) -> None:
    # flush: stdout is block-buffered when this is piped to a log, and the stages
    # below write progress to stderr. Without it a redirected run reads with its
    # banners in the wrong places, or an error above the step that raised it.
    print(f"\n━━ Step {step}/{total} · {title} ━━\n", flush=True)


# --------------------------------------------------------------------------- #
# Stage 0 — preflight                                                          #
# --------------------------------------------------------------------------- #


def resolve_league_id(fixture_id: int) -> tuple[int, str, str]:
    """Find the Fixture's Competition. Returns (league_id, league_name, source).

    Postgres first, `data/football.db` second. The order matters: Postgres holds
    only published Competitions but is current to the last publish, while
    football.db holds all ~42 but is only as fresh as the 04:00 rebuild. A
    Fixture we are about to draft is almost always in the first; the fallback is
    what lets you pipeline a Competition the store has not published yet.
    """
    row = get_conn().execute(
        "SELECT league_id, league_name FROM public.fixture WHERE id = %s", (fixture_id,)
    ).fetchone()
    if row:
        return int(row[0]), str(row[1]), "Published Store"

    import sqlite3

    if FOOTBALL_DB.exists():
        connection = sqlite3.connect(f"file:{FOOTBALL_DB}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT league_id, league_name FROM fixture WHERE id = ?", (fixture_id,)
            ).fetchone()
        finally:
            connection.close()
        if row:
            return int(row[0]), str(row[1]), "football.db"

    raise PipelineError(
        f"Fixture {fixture_id} is in neither the Published Store nor "
        f"{FOOTBALL_DB.name}. Check the id — look it up with:\n"
        f"    SELECT id, date, home_team_name, away_team_name, status, round\n"
        f"    FROM fixture WHERE league_id = <competition> ORDER BY date DESC LIMIT 20;"
    )


def preflight(fixture_id: int, espn_id: Optional[str], *, dry_run: bool,
              redraft: bool = False) -> tuple[int, str, dict, PocketBaseClient]:
    """Resolve the Competition and prove every later stage can run.

    Everything checked here is checked *before* the first write, so a missing
    credential or an un-onboarded publication costs no API quota and leaves no partial
    state behind.
    """
    league_id, league_name, source = resolve_league_id(fixture_id)
    print(f"  Fixture {fixture_id} → {league_name} (competition {league_id}), via {source}")

    # The model is needed by stage 4, and by stage 2 for narrative-coverage feeds
    # (events_only feeds cost nothing). Fail now rather than after the refresh.
    if espn_id is not None or not dry_run:
        require_env("ANTHROPIC_API_KEY")

    pb = PocketBaseClient()
    publication = pb.get_publication_by_competition_id(league_id)
    if not publication:
        pb.close()
        raise PipelineError(
            f"No PocketBase `publication` record for competition {league_id} ({league_name}).\n"
            f"    Onboarding is interactive — it asks for the slug, language and\n"
            f"    display timezone this pipeline needs:\n"
            f"        uv run python -m football_blog.onboard --league-id {league_id}"
        )
    print(f"  PocketBase publication: slug={publication['slug']}, lang={publication['default_language']}, "
          f"tz={publication['display_timezone']}, published={publication['published']}")

    # The Editorial Store is authored, not derived: it has no rebuild path, and a
    # *published* Match Post's Narrative has been edited by hand. `upsert_post`
    # keys on the Fixture id alone, so a re-run would replace that prose with fresh
    # model output and reset the record to draft — silently, and unrecoverably.
    # Drafts stay freely overwritable; that is what a draft is for. See ADR 0029.
    existing = pb.get_post_by_fixture_id(fixture_id)
    if existing and existing.get("status") == "published" and not (redraft or dry_run):
        pb.close()
        raise PipelineError(
            f"Fixture {fixture_id} already has a PUBLISHED match_post "
            f"({existing['id']}, {existing['slug']}, published "
            f"{existing.get('published_at') or 'date unrecorded'}).\n"
            f"    Redrafting would overwrite a Narrative you edited by hand and reset\n"
            f"    it to draft. Nothing can regenerate it — the Editorial Store has no\n"
            f"    rebuild path.\n"
            f"    Pass --redraft if that is genuinely what you want."
        )
    if existing and existing.get("status") == "published" and dry_run:
        print(f"  note: a published match_post exists ({existing['id']}). --dry-run "
              f"writes nothing, so this is allowed; a live run would need --redraft.")
    elif existing:
        print(f"  existing match_post {existing['id']} (status={existing['status']}) "
              f"will be overwritten.")

    # A Competition absent from Postgres can still be delta-published, but the
    # delta only ever parses the *current* Season — its history would silently
    # not be there. Say so now; it is invisible later.
    (published_count,) = get_conn().execute(
        "SELECT count(*) FROM public.fixture WHERE league_id = %s", (league_id,)
    ).fetchone()
    if published_count == 0:
        print(f"  ⚠ {league_name} has no Fixtures in the Published Store yet. The delta in\n"
              f"    step 3 publishes the current Season only; for its full history run\n"
              f"    `uv run python -m football.publish.pg <all league ids you want kept>`\n"
              f"    (a wholesale publish is destructive by omission — list them all).")

    return league_id, league_name, publication, pb


# --------------------------------------------------------------------------- #
# Stages 1-4                                                                   #
# --------------------------------------------------------------------------- #


def stage_refresh(league_id: int) -> None:
    """Force-refresh this Competition's current-Season frontier into the cache."""
    try:
        refresh.main(["--no-rebuild", "--only", str(league_id)])
    except SystemExit as e:
        # Refresh exits non-zero on a hit quota or a bad Competition, but the cache
        # is internally consistent even after a partial run (ADR 0018). Warn and
        # carry on rather than block the draft on an incomplete frontier — exactly
        # what football.publish.delta does for the same reason.
        if e.code:
            print(f"\n⚠ Refresh exited non-zero (code {e.code}) — the cache holds whatever "
                  f"it collected; continuing with that.")


def stage_espn(espn_id: str, fixture_id: int, *, force_link: bool, reclassify: bool) -> None:
    """Ingest the ESPN match into data/commentary.db, linked to the Fixture."""
    # --link-required: this stage cannot fall back to an unlinked ingest, because the
    # drafter is keyed on the Fixture and --fixture-id is required. Without it a
    # refusal would end by advising the one thing impossible here (ADR 0039).
    argv = [espn_id, "--fixture-id", str(fixture_id), "--verify-fallback-pg",
            "--link-required"]
    if force_link:
        argv.append("--force-link")
    if reclassify:
        argv.append("--reclassify")
    try:
        code = commentary_ingest.main(argv)
    except SystemExit as e:  # an unparseable game id
        raise PipelineError(str(e.code) if e.code else "ESPN ingest refused.") from e
    except requests.RequestException as e:
        # A typo'd game id is a 404 from ESPN's site API. Left alone it surfaces as
        # a raw traceback, which reads like a bug in the pipeline rather than a bad
        # argument — and a mistyped ESPN id is at least as likely as a mistyped
        # Fixture id, which we already refuse by hand.
        raise PipelineError(
            f"ESPN would not serve game {espn_id}: {e}\n"
            f"    Check the id against the URL — the number in\n"
            f"    https://www.espn.com/soccer/commentary/_/gameId/<this>"
        ) from e
    except ValueError as e:
        # Raised either by fetch_summary (no `commentary` key at all) or, more often,
        # by the join in commentary/join.py refusing to guess. Do not assert which —
        # the earlier wording blamed the payload and sent the operator hunting the
        # game id when the actual complaint was about joining keyEvents to lines.
        raise PipelineError(
            f"ESPN commentary for game {espn_id} could not be processed:\n"
            f"    {e}\n"
            f"    The raw payload is cached, so re-running after a fix costs no "
            f"requests. Nothing was written to data/commentary.db."
        ) from e
    if code:
        # No remedy is offered here on purpose. `commentary.ingest` returns an exit
        # *code*, so this function cannot know which check refused — and it used to
        # print both guesses at every failure. Against a delayed-match refusal one was
        # misleading ("still live": football.db lags, because stage 1 runs --no-rebuild)
        # and the other impossible (--force-link cannot waive a kickoff). The refusal
        # above states its own remedy, which is the only place that knows it (ADR 0038).
        raise PipelineError(
            f"ESPN ingest refused game {espn_id}. Nothing has been published or "
            f"drafted.\n"
            f"    The refusal above says which check failed and what to do about it."
        )


def stage_publish(league_id: int) -> dict[str, int]:
    """One delta publish — carries the new Final *and* the commentary."""
    try:
        return publish_pg.delta_publish([league_id])
    except SystemExit as e:
        raise PipelineError(f"Delta publish refused: {e.code}") from e


def assert_final(fixture_id: int) -> str:
    """Refuse to draft a match that is not over.

    Stage 2 already gates on ESPN's own `completed` flag, but that stage is
    skipped whenever --espn-id is omitted, and this is the check that covers it.
    """
    row = get_conn().execute(
        "SELECT status, home_team_name, away_team_name FROM public.fixture WHERE id = %s",
        (fixture_id,),
    ).fetchone()
    if not row:
        raise PipelineError(
            f"Fixture {fixture_id} still is not in the Published Store after the "
            f"publish. Its Competition may have no cached current Season — run\n"
            f"    uv run python -m football.publish.pg <league ids>\n"
            f"to baseline the store first."
        )
    status, home, away = row
    if status not in FINAL_STATUSES:
        raise PipelineError(
            f"Fixture {fixture_id} ({home} v {away}) has status {status!r}, not one of "
            f"{'/'.join(FINAL_STATUSES)}.\n"
            f"    A narrative of an unfinished match is worse than none, because nothing\n"
            f"    downstream can tell. Re-run this after full time."
        )
    return status


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def run(
    fixture_id: int,
    espn_id: Optional[str] = None,
    *,
    dry_run: bool = False,
    skip_refresh: bool = False,
    skip_publish: bool = False,
    force_link: bool = False,
    reclassify: bool = False,
    redraft: bool = False,
    instruction: Optional[str] = None,
) -> Optional[dict]:
    """Run the whole pipeline. Returns the PocketBase record, or None on dry-run."""
    t0 = time.monotonic()
    total = 4

    _banner(0, total, "Preflight")
    league_id, league_name, _publication, pb = preflight(
        fixture_id, espn_id, dry_run=dry_run, redraft=redraft)

    try:
        _banner(1, total, f"Refreshing {league_name}'s current-Season frontier into the cache")
        if skip_refresh:
            print("  skipped (--skip-refresh): publishing from the cache as it stands.")
        else:
            stage_refresh(league_id)

        _banner(2, total, "Ingesting the ESPN commentary")
        if espn_id is None:
            print("  skipped: no --espn-id given. The draft will have no ESPN commentary\n"
                  "  to draw colour from — the event timeline and team stats still carry it.")
        else:
            stage_espn(espn_id, fixture_id, force_link=force_link, reclassify=reclassify)

        _banner(3, total, "Publishing to the Postgres Published Store (delta)")
        if skip_publish:
            print("  skipped (--skip-publish): drafting from Postgres as it stands.")
        else:
            counts = stage_publish(league_id)
            print(f"  {sum(counts.values()):,} rows across {len(counts)} tables")

        status = assert_final(fixture_id)

        _banner(4, total, f"Drafting the narrative ({status})")
        if instruction:
            print(f"  editorial direction: {instruction}")
        post = draft.draft_fixture(fixture_id, pb, dry_run=dry_run,
                                   instruction=instruction)
        if post is None and not dry_run:
            raise PipelineError(
                f"The drafter returned nothing for fixture {fixture_id} — see the error "
                f"logged above."
            )
    finally:
        pb.close()

    elapsed = time.monotonic() - t0
    if dry_run:
        print(f"\n✓ dry run complete in {elapsed:.0f}s — no Claude call, no PocketBase write.")
        return None

    print(f"\n✓ fixture {fixture_id} → match_post {post['id']} "
          f"slug={post['slug']} status={post['status']} in {elapsed:.0f}s")
    print(f"  Review and publish it at http://127.0.0.1:8090/_/#/collections?collection=match_post")
    return post


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m football_blog.pipeline",
        description="Fixture id + ESPN game id -> a PocketBase match_post draft.",
    )
    ap.add_argument("--fixture-id", type=int, required=True,
                    help="the API-Football Fixture to draft.")
    ap.add_argument("--espn-id", type=str,
                    help="ESPN game id or commentary URL. Omit to skip the ESPN stage.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt; no Claude call and no PocketBase write.")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="skip the cache refresh (step 1).")
    ap.add_argument("--skip-publish", action="store_true",
                    help="skip the delta publish (step 3).")
    ap.add_argument("--force-link", action="store_true",
                    help="waive the ESPN-vs-ours team-name check; kickoff still verified.")
    ap.add_argument("--reclassify", action="store_true",
                    help="re-ingest an ESPN match already stored (costs model calls).")
    ap.add_argument("--instruction", type=str,
                    help="one-off editorial steer for this report, e.g. 'lead with the "
                         "comeback'. Shapes emphasis and angle only — it cannot license "
                         "a fact the material does not carry. Recorded on the Match Post.")
    ap.add_argument("--redraft", action="store_true",
                    help="overwrite an already-PUBLISHED match_post. Destroys the "
                         "hand-edited Narrative; nothing can regenerate it.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # httpx logs every PocketBase call at INFO, which buries the stage banners in
    # a run that is mostly HTTP. -v puts them back.
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        run(
            args.fixture_id,
            args.espn_id,
            dry_run=args.dry_run,
            skip_refresh=args.skip_refresh,
            skip_publish=args.skip_publish,
            force_link=args.force_link,
            reclassify=args.reclassify,
            redraft=args.redraft,
            instruction=args.instruction,
        )
    except PipelineError as e:
        sys.stdout.flush()  # keep the refusal below the stage that raised it
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
