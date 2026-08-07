"""Drafter: given a fixture id, load its data, call Claude, upsert to PocketBase.

## What it does (per fixture)

1. Load the fixture bundle from Postgres (8 batched queries via loader.py):
   fixture + events + squad + team stats + team profiles + venue + players +
   ESPN commentary (key-moment lines from commentary_line/narrated_match).
2. Look up the League row in PocketBase (by postgres_competition_id) to get
   `default_language`, `display_timezone`, and any `llm_prompt_overrides`.
3. Assemble the prompt (prompts.assemble_prompt): system_{lang}.md + the
   Publication's override + the derived facts block from prompts/builder.py —
   structured, not prose, so the model pattern-matches on facts rather than
   paraphrasing our text. That function is the *only* assembler; the Desk's
   preview calls it too, so what is shown is what is sent (ADR 0034).
4. Call Claude (Opus 4.7) with the assembled prompt. No temperature/thinking/
   budget_tokens per Opus 4.7 rules.
5. Upsert a Post record in PocketBase with `status='draft'`,
   `ai_drafted=true`, `published_at=null`. Also self-heals `team_slug`
   records for both teams (bootstraps their URL-friendly names).

## Usage

Draft one specific fixture (recommended for the highest-impact games):

    python -m football_blog.draft --fixture-id 1591866

Dry-run first to inspect the prompt without spending tokens:

    python -m football_blog.draft --fixture-id 1591866 --dry-run

Sweep for finished-but-undrafted fixtures across published Publications
(defaults: last 30 days, max 3 drafts per run):

    python -m football_blog.draft

Sweep a specific competition, ignoring the Publication's `published` gate:

    python -m football_blog.draft --league-id 1 --limit 5

Available flags:
  --fixture-id ID    Draft one fixture (overwrites any existing draft).
  --league-id ID     Sweep only this Postgres competition; ignores the
                     PocketBase `published` gate. Useful for backfilling
                     an unpublished publication.
  --since ISODATE    Sweep window start. Default: 30 days ago.
  --limit N          Max drafts per sweep. Default: 3.
  --dry-run          Print the prompt only; skip LLM + PocketBase writes.
  --verbose / -v     Include DEBUG-level logs.

## After the drafter runs

The Drafter only *creates* the draft. Publishing is a manual gate:

1. Review the Post in the PocketBase admin (localhost:8090/_/).
2. Edit `narrative_md` if the LLM got anything wrong.
3. First time you publish for a new publication: flip the publication's
   `published=true` (see pocketbase.PocketBaseClient.set_publication_published).
4. Set the Post's `status='published'` and `published_at=<now>`.
5. Rebuild the site (`astro build`) or wait for the nightly rebuild cron.

## Fixture-id lookups

Query Postgres for the fixture id you want:

    SELECT id, date, home_team_name, away_team_name, status, round
    FROM fixture
    WHERE league_id = 1  -- 1=World Cup, 253=MLS, 262=Liga MX
    ORDER BY date DESC LIMIT 20;

## Model + caching notes

Model: claude-opus-4-7 (default per Anthropic guidance). Change the MODEL
constant if a cheaper model is acceptable for the workload.

Prompt caching is intentionally NOT used: our system prompt is ~1000 tokens
and Opus 4.7 requires a 4096-token minimum cacheable prefix, so cache_control
would silently no-op. If the system prompt ever grows past 4096 tokens, add
`cache_control: {"type": "ephemeral"}` to the system message block.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from anthropic import Anthropic

from . import FINAL_STATUSES
# Aliased: `bundle` is taken throughout this module by the local FullFixture variable,
# and a shadowed module import fails at the call, not at the import.
from . import bundle as bundle_mod
from .config import require_env
from .loader import load_post_bundles
from .pocketbase import PocketBaseClient
from .prompts import assemble_prompt
from .slugs import team_name_to_slug, fixture_slug
from .types import FullFixture

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"


def draft_narrative(
    bundle: FullFixture,
    lang: str,
    display_timezone: str,
    extra_style: Optional[str] = None,
    *,
    dry_run: bool = False,
    client: Optional[Anthropic] = None,
    instruction: Optional[str] = None,
) -> tuple[str, dict]:
    """Returns (markdown_narrative, telemetry_dict). If dry_run=True, returns the
    prompt as the narrative and a telemetry dict of {mode: "dry_run"}."""
    # One assembler, shared with the Desk's preview — see prompts/__init__.py.
    system_prompt, user_prompt = assemble_prompt(
        bundle, lang, display_timezone, extra_style, instruction)

    if dry_run:
        return user_prompt, {"mode": "dry_run", "chars_system": len(system_prompt), "chars_user": len(user_prompt)}

    if client is None:
        client = Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=[{"type": "text", "text": system_prompt}],
        messages=[
            {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
        ],
    )
    parts = [b.text for b in resp.content if b.type == "text"]
    narrative = "".join(parts).strip()
    telemetry = {
        "mode": "live",
        "model": MODEL,
        "input_tokens": resp.usage.input_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
        "output_tokens": resp.usage.output_tokens,
        "chars_output": len(narrative),
    }
    return narrative, telemetry


def draft_fixture(
    fixture_id: int,
    pb: PocketBaseClient,
    *,
    dry_run: bool = False,
    instruction: Optional[str] = None,
) -> Optional[dict]:
    """End-to-end: load fixture from Postgres, fetch its League from PocketBase,
    prompt Claude, upsert draft Post. Returns the created/updated Post record
    (or None on dry-run).

    `instruction` is the operator's one-off steer for this draft ("lead with the
    comeback"). It goes into the prompt *and* onto the Match Post: a Narrative cannot
    be regenerated, so an input that is not written down is an input nothing can
    recover (ADR 0034). Those two must not drift apart — what was recorded is the
    claim that what was sent.
    """
    bundles = load_post_bundles([fixture_id])
    bundle = bundles.get(fixture_id)
    if not bundle:
        log.error("draft_fixture: fixture %s not found in Postgres", fixture_id)
        return None

    publication = pb.get_publication_by_competition_id(bundle.fixture.league_id)
    if not publication:
        log.error("draft_fixture: no PocketBase publication for competition_id %s", bundle.fixture.league_id)
        return None
    lang = publication["default_language"]
    tz = publication["display_timezone"]
    extra_style = publication.get("llm_prompt_overrides") or None

    narrative, telemetry = draft_narrative(
        bundle, lang, tz, extra_style, dry_run=dry_run, instruction=instruction)
    log.info("draft_fixture %s: %s", fixture_id, telemetry)
    if dry_run:
        print(f"\n===== DRY RUN: fixture {fixture_id} =====\n")
        print(narrative)
        print(f"\n===== end dry run =====\n")
        return None

    # Upsert team_slugs (bootstrapping) — the Onboarding CLI does this too but
    # sweep-mode Drafter should also self-heal.
    home_slug = team_name_to_slug(bundle.fixture.home_team_name)
    away_slug = team_name_to_slug(bundle.fixture.away_team_name)
    pb.upsert_team_slug(bundle.fixture.home_team_id, home_slug, bundle.fixture.home_team_name)
    pb.upsert_team_slug(bundle.fixture.away_team_id, away_slug, bundle.fixture.away_team_name)

    post_slug = fixture_slug(home_slug, away_slug, bundle.fixture.date, tz)

    record = pb.upsert_post({
        "postgres_fixture_id": fixture_id,
        "publication": publication["id"],
        "slug": post_slug,
        "status": "draft",
        "narrative_md": narrative,
        # Written on every draft, null when none was given, so null means exactly one
        # thing. The Desk's per-run steer lands here from ADR 0034's --instruction;
        # until that flag exists this is always None, and that is the point — the
        # column predates the first instructed draft rather than trailing it.
        "draft_instruction": instruction,
        "ai_drafted": True,
        "possibly_stale": False,
        "hero_image_override": None,
        "seo_title": f"{bundle.fixture.home_team_name} vs {bundle.fixture.away_team_name} — Crónica",
        "seo_description": f"Crónica del partido de {bundle.fixture.league_name} entre {bundle.fixture.home_team_name} y {bundle.fixture.away_team_name}.",
        "author": "Gerardo M.",
        "published_at": None,
    })

    # The Match Bundle this post renders from (ADR 0044). Written here rather than left
    # to the nightly pass because a post without one renders an empty page, and the gap
    # would sit until 04:00 — long enough for the operator who just drafted it to open
    # it, see nothing, and conclude the drafter failed.
    #
    # After the post, not before: a bundle for a post that was never written is an orphan
    # nothing cleans up, while a post without a bundle is a gap the nightly pass closes.
    # And wrapped, because the two halves are worth very different amounts — the
    # Narrative cost a model call and, where `instruction` was given, an operator's steer
    # that nothing reconstructs; the bundle costs one rebuild. A failure here must not
    # take the post down with it.
    try:
        bundle_mod.build_bundles(pb, [fixture_id])
    except Exception as exc:                       # noqa: BLE001 — see above
        log.warning("draft_fixture: Match Post %s written, but its Match Bundle failed "
                    "(%s). The nightly --bundles pass will close the gap.",
                    fixture_id, exc)

    return record


# --------------------------------------------------------------------------- #
# CLI + sweep                                                                 #
# --------------------------------------------------------------------------- #

import argparse
from datetime import timedelta

from .postgres import get_conn


def find_undrafted_fixtures(
    pb: PocketBaseClient,
    *,
    league_id: Optional[int] = None,
    since: Optional[datetime] = None,
    limit: int = 3,
) -> list[int]:
    """Find finished fixtures (FT/AET/PEN) in *published* Publications that don't
    have a Post yet in PocketBase. Ordered newest first. Capped at `limit`."""
    conn = get_conn()

    if league_id is not None:
        # Explicit publication — ignore published flag.
        pb_publications = [pb.get_publication_by_competition_id(league_id)]
        pb_publications = [l for l in pb_publications if l is not None]
    else:
        # Sweep — only Publications flagged published.
        pb_publications = pb.list_publications(only_published=True)

    if not pb_publications:
        return []

    competition_ids = [l["postgres_competition_id"] for l in pb_publications]
    since = since or (datetime.now(timezone.utc) - timedelta(days=30))
    since_naive = since.strftime("%Y-%m-%d %H:%M:%S")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM fixture
            WHERE league_id = ANY(%s)
              AND status = ANY(%s)
              AND date >= %s
            ORDER BY date DESC
            """,
            (competition_ids, list(FINAL_STATUSES), since_naive),
        )
        candidate_ids = [row[0] for row in cur.fetchall()]

    if not candidate_ids:
        return []

    existing = pb.list_posts_by_fixture_ids(candidate_ids)
    undrafted = [fid for fid in candidate_ids if fid not in existing]
    return undrafted[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(prog="football_blog.draft", description="Draft match narratives via Claude.")
    parser.add_argument("--fixture-id", type=int, help="Draft one specific fixture (overwrites existing draft).")
    parser.add_argument("--league-id", type=int, help="Sweep only this competition id (overrides published gate).")
    parser.add_argument("--since", type=str, help="ISO date; sweep fixtures on/after this date. Default: 30 days ago.")
    parser.add_argument("--limit", type=int, default=3, help="Max drafts per run (default: 3).")
    parser.add_argument("--instruction", type=str,
                        help="One-off editorial steer for this draft, e.g. 'lead with "
                             "the comeback'. Shapes emphasis only — it cannot license a "
                             "fact the data does not carry. Recorded on the Match Post.")
    parser.add_argument("--dry-run", action="store_true", help="Print prompts, do not call LLM or write to PocketBase.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # An instruction is about one match. Applied to a sweep it would steer every
    # unrelated Fixture in the batch the same way, and be recorded on each of them as
    # if it had been meant — a provenance lie, which is the one thing the field exists
    # to prevent (ADR 0034).
    if args.instruction and args.fixture_id is None:
        print("--instruction applies to one match; use it with --fixture-id.",
              file=sys.stderr)
        return 2

    pb = PocketBaseClient()

    if args.fixture_id is not None:
        fixture_ids = [args.fixture_id]
    else:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
        fixture_ids = find_undrafted_fixtures(
            pb, league_id=args.league_id, since=since, limit=args.limit,
        )
        if not fixture_ids:
            print("No undrafted fixtures found.")
            return 0
        print(f"Sweeping {len(fixture_ids)} fixture(s): {fixture_ids}")

    exit_code = 0
    for fid in fixture_ids:
        try:
            post = draft_fixture(fid, pb, dry_run=args.dry_run,
                                 instruction=args.instruction)
            if post is not None:
                print(f"  ✓ fixture {fid} → post id={post['id']} slug={post['slug']} status={post['status']}")
        except Exception as e:
            log.exception("Draft failed for fixture %s: %s", fid, e)
            exit_code = 1
    pb.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
