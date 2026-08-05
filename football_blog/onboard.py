"""Onboarding checklist per ADR-0003. Each check_* function returns
(status, message) where status is one of 'pass', 'warn', 'fail'."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Callable, Literal, Optional

from . import FINAL_STATUSES
from .loader import load_post_bundles
from .pocketbase import PocketBaseClient
from .postgres import get_conn

log = logging.getLogger(__name__)

Status = Literal["pass", "warn", "fail"]


@dataclass
class CheckResult:
    status: Status
    message: str
    detail: Optional[str] = None


def check_competition_exists(competition_id: int) -> CheckResult:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, country FROM competition WHERE id = %s", (competition_id,))
        row = cur.fetchone()
    if not row:
        return CheckResult("fail", f"No competition row for id={competition_id} in Postgres.")
    return CheckResult("pass", f"Competition: {row[1]} ({row[2]})")


def check_recent_fixtures(competition_id: int, days: int = 90) -> CheckResult:
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM fixture WHERE league_id = %s AND date >= %s",
            (competition_id, since),
        )
        (count,) = cur.fetchone()
    if count == 0:
        return CheckResult("fail", f"No fixtures in the last {days} days for competition {competition_id}.")
    return CheckResult("pass", f"{count} fixtures in the last {days} days.")


def check_finished_coverage(competition_id: int, days: int = 180) -> CheckResult:
    """Verify ≥80% of recently-finished fixtures have events, squad, and stats."""
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH finished AS (
              SELECT id FROM fixture
              WHERE league_id = %s AND date >= %s AND status = ANY(%s)
            )
            SELECT
              (SELECT COUNT(*) FROM finished) AS total,
              (SELECT COUNT(DISTINCT fixture_id) FROM event WHERE fixture_id IN (SELECT id FROM finished)) AS with_events,
              (SELECT COUNT(DISTINCT fixture_id) FROM squadentry WHERE fixture_id IN (SELECT id FROM finished)) AS with_squad,
              (SELECT COUNT(DISTINCT fixture_id) FROM teammatchstat WHERE fixture_id IN (SELECT id FROM finished)) AS with_stats
            """,
            (competition_id, since, list(FINAL_STATUSES)),
        )
        total, with_events, with_squad, with_stats = cur.fetchone()
    if total == 0:
        return CheckResult("warn", "No finished fixtures in the coverage window — nothing to verify.")
    pct = min(with_events, with_squad, with_stats) / total
    detail = f"{total} finished; events {with_events}/{total}, squad {with_squad}/{total}, stats {with_stats}/{total}"
    if pct < 0.8:
        return CheckResult("fail", f"Coverage below 80% ({pct*100:.1f}%).", detail)
    return CheckResult("pass", f"Coverage {pct*100:.1f}%.", detail)


def check_venues(competition_id: int) -> CheckResult:
    """Warn if fixtures reference venue ids that don't exist in the venue table."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(DISTINCT f.venue_id) AS referenced,
              COUNT(DISTINCT v.id) AS resolved
            FROM fixture f
            LEFT JOIN venue v ON v.id = f.venue_id
            WHERE f.league_id = %s AND f.venue_id IS NOT NULL
            """,
            (competition_id,),
        )
        referenced, resolved = cur.fetchone()
    if referenced == 0:
        return CheckResult("warn", "No fixtures reference a venue_id.")
    if resolved < referenced:
        return CheckResult("warn", f"Only {resolved}/{referenced} referenced venues exist in the venue table.")
    return CheckResult("pass", f"All {referenced} referenced venues resolved.")


def check_team_logos(competition_id: int, days: int = 180) -> CheckResult:
    """Scope the team pool to fixtures within the last `days`. For cups where
    a single competition_id spans multiple editions (e.g., World Cup 2010–2026
    all share id=1), this prevents historical national teams from dragging
    down the logo-coverage percentage for the current edition."""
    conn = get_conn()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT tp.id) as with_logo, COUNT(DISTINCT team_id) as total
            FROM (
              SELECT home_team_id AS team_id FROM fixture WHERE league_id = %s AND date >= %s
              UNION SELECT away_team_id FROM fixture WHERE league_id = %s AND date >= %s
            ) t
            LEFT JOIN teamprofile tp ON tp.id = t.team_id AND tp.logo IS NOT NULL
            """,
            (competition_id, since, competition_id, since),
        )
        with_logo, total = cur.fetchone()
    if total == 0:
        return CheckResult("warn", "No teams found.")
    if with_logo < total:
        return CheckResult("warn", f"Only {with_logo}/{total} teams have logos in teamprofile.")
    return CheckResult("pass", f"All {total} teams have logos.")


def check_pocketbase_publication(competition_id: int, pb: PocketBaseClient) -> CheckResult:
    publication = pb.get_publication_by_competition_id(competition_id)
    if not publication:
        return CheckResult("fail", f"No PocketBase publication row for competition {competition_id}. Run interactive setup.")
    return CheckResult("pass", f"PocketBase publication: slug={publication['slug']}, published={publication['published']}.")


def check_sample_draft(competition_id: int, pb: PocketBaseClient) -> CheckResult:
    """Pick a random recently-finished fixture and dry-run the drafter on it."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM fixture
            WHERE league_id = %s AND status = ANY(%s)
            ORDER BY date DESC LIMIT 1
            """,
            (competition_id, list(FINAL_STATUSES)),
        )
        row = cur.fetchone()
    if not row:
        return CheckResult("warn", "No finished fixture available to sample.")

    fixture_id = row[0]
    # Import here to avoid circular import at module load.
    from .draft import draft_fixture
    print(f"\n  Dry-running drafter on fixture {fixture_id}...\n")
    draft_fixture(fixture_id, pb, dry_run=True)
    return CheckResult("pass", f"Dry-run completed on fixture {fixture_id}. Verify the prompt above looks right.")


# --------------------------------------------------------------------------- #
# Interactive helpers                                                         #
# --------------------------------------------------------------------------- #

VALID_LANGUAGES = ("es", "en")
COMMON_TZS = {
    "1": "America/Mexico_City",
    "2": "America/New_York",
    "3": "America/Los_Angeles",
    "4": "Europe/Madrid",
    "5": "America/Bogota",
}


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default


def _ask_choice(prompt: str, options: dict[str, str]) -> str:
    print(prompt)
    for k, v in options.items():
        print(f"  {k}) {v}")
    while True:
        c = input("Choose: ").strip()
        if c in options:
            return options[c]


def _ask_yes_no(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        v = input(f"{prompt} [{d}] ").strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def interactive_create_publication(
    competition_id: int,
    pb: PocketBaseClient,
    overrides_file: Optional[str] = None,
) -> dict:
    """Prompt for the League config fields and create the PocketBase record.

    If `overrides_file` is provided, its full text becomes `llm_prompt_overrides`
    on the new publication. Use this for tournament-specific LLM guidance (see
    football_blog/prompts/cup_override_es.md for the Mundial 2026 example)."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT name, country FROM competition WHERE id = %s", (competition_id,))
        (comp_name, comp_country) = cur.fetchone()

    print(f"\nCreating PocketBase publication for {comp_name} ({comp_country}).\n")

    display_name = _ask("Display name (shown as-is on the site)", comp_name)
    slug = _ask("URL slug (lowercase, hyphens, no punctuation)")
    lang = ""
    while lang not in VALID_LANGUAGES:
        lang = _ask("Default language (es or en)").lower()
    tz = _ask_choice("Display timezone:", COMMON_TZS)
    brand_color = _ask("Brand color (#RRGGBB or blank)", "").strip()
    if brand_color and not brand_color.startswith("#"):
        brand_color = f"#{brand_color}"

    llm_overrides = None
    if overrides_file:
        from pathlib import Path
        p = Path(overrides_file)
        if not p.exists():
            raise FileNotFoundError(f"--overrides-file not found: {overrides_file}")
        llm_overrides = p.read_text()
        print(f"  Loaded {len(llm_overrides)} chars from {overrides_file} into llm_prompt_overrides.")

    payload = {
        "postgres_competition_id": competition_id,
        "display_name": display_name,
        "slug": slug,
        "default_language": lang,
        "display_timezone": tz,
        "brand_color": brand_color or None,
        "published": False,
        "llm_prompt_overrides": llm_overrides,
    }
    # Use the raw client — no upsert helper for Publications since it's rare.
    import httpx
    r = httpx.post(
        f"{pb.base_url}/api/collections/publication/records",
        headers=pb._headers(),
        json=payload,
        timeout=10.0,
    )
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

import argparse


def _run_check(name: str, fn: Callable, *args) -> CheckResult:
    print(f"  ▶ {name}...", end=" ", flush=True)
    res = fn(*args)
    icon = {"pass": "✓", "warn": "⚠", "fail": "✗"}[res.status]
    print(f"{icon} {res.message}")
    if res.detail:
        print(f"      {res.detail}")
    return res


def main() -> int:
    parser = argparse.ArgumentParser(prog="football_blog.onboard", description="8-step League onboarding checklist.")
    parser.add_argument("--league-id", type=int, required=True, help="Postgres competition id (e.g., 253 for MLS, 262 for Liga MX, 1 for World Cup).")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmations (for CI). Fails if PocketBase publication row missing.")
    parser.add_argument("--overrides-file", type=str, help="Path to a Markdown file whose contents become llm_prompt_overrides on the new publication (used only if the PB publication row does not yet exist and we go interactive).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    pb = PocketBaseClient()

    print(f"\n=== Onboarding publication {args.league_id} ===\n")

    steps: list[CheckResult] = []
    steps.append(_run_check("1. Postgres competition exists",
                            check_competition_exists, args.league_id))
    steps.append(_run_check("2. Recent fixtures exist",
                            check_recent_fixtures, args.league_id))
    steps.append(_run_check("3. Finished-fixture coverage ≥ 80%",
                            check_finished_coverage, args.league_id))
    steps.append(_run_check("4. Team logos present in teamprofile",
                            check_team_logos, args.league_id))
    steps.append(_run_check("5. Referenced venues resolve",
                            check_venues, args.league_id))

    if any(s.status == "fail" for s in steps[:5]):
        print("\n✗ Blocking failures detected. Fix upstream ingest before continuing.")
        pb.close()
        return 1

    # Step 6 — PocketBase publication row (may not exist yet; interactive-create if missing).
    print(f"  ▶ 6. PocketBase publication row present...", end=" ", flush=True)
    publication = pb.get_publication_by_competition_id(args.league_id)
    if publication:
        print(f"✓ slug={publication['slug']}, published={publication['published']}")
    else:
        print("✗ (missing)")
        if args.yes:
            print("      --yes was set; refusing to create interactively. Rerun without --yes.")
            pb.close()
            return 1
        if not _ask_yes_no("      Create it now interactively?", default=True):
            pb.close()
            return 1
        publication = interactive_create_publication(args.league_id, pb, overrides_file=args.overrides_file)
        print(f"      Created: slug={publication['slug']}")

    # Step 7 — dry-run drafter on a sample fixture.
    print("\n  ▶ 7. Sample-fixture drafter dry-run (prompt only, no LLM call)")
    _run_check("     dry-run", check_sample_draft, args.league_id, pb)

    # Step 8 — publish confirmation.
    if publication["published"]:
        print("\n  ▶ 8. Publish confirmation: already published. Nothing to do.")
    elif args.yes:
        print("\n  ▶ 8. --yes was set; skipping publish confirmation. Set published=true manually if you want to flip.")
    else:
        if _ask_yes_no("\n  ▶ 8. Set publication to published=true on the public site?", default=False):
            pb.set_publication_published(publication["id"], True)
            print("      ✓ Published.")
        else:
            print("      Skipped. Rerun onboarding to publish later.")

    print("\n=== Onboarding complete ===\n")
    pb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
