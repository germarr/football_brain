"""Season rollover: decide when a Competition's new Season may join the registry (ADR 0045).

Refresh targets one Season per Competition — `max(comp["seasons"])` — so the moment the
provider opens a new one, config is behind and the new Season is collected into the raw
cache but never modeled. ADR 0018 answered that with a warning and a manual edit, and
rejected auto-rollover because "a cron job should not rewrite your source and start
pulling a possibly preseason/friendly-only season with no human in the loop."

This module keeps the human in the loop and removes the guesswork. It does not decide
*whether* to roll — the operator still runs `--apply` — it decides whether rolling is
**safe today**, and says so per Competition in the nightly log.

Two things the ADR 0018 warning could not tell you, and this can:

**The new Season may be months away.** The provider's `current: true` flag means "query
this by default", not "this has begun" — on 2026-08-10 it was true for a Super Cup whose
first match is 2027-02-02. `max(year) > ours` alone would have rolled it half a year
early. G1 below reads the season's actual start date instead.

**Rolling abandons the outgoing Season.** Because Refresh only ever targets the newest
Season, appending a year silently stops every recurring job from re-fetching the previous
one; any fixture still unplayed there is stranded until someone hand-runs `orchestrate`.
That — not preseason friendlies — is the real hazard of unattended rollover, and G2 is
the check for it. It is free: last night's Refresh force-refreshed exactly the fixture
list it reads.

Deliberately *not* gated on a Final having been played in the new Season. The operator's
own practice is to roll ahead of kickoff (on 2026-08-10, thirteen Competitions were
pinned to a 2026 Season with a full calendar and zero Finals), and there is a mechanical
reason: Match Previews are built over a seven-day horizon (ADR 0040), and a Fixture
cannot have a Preview unless its Season is in the registry. A Final-gated rollover would
black out every Competition's opening matchday, every season, by construction.

Run with:
    uv run python -m football.onboard.rollover              # review every Competition
    uv run python -m football.onboard.rollover --apply      # roll the ones that pass
    uv run python -m football.onboard.rollover 203 673      # scope to these league ids
    uv run python -m football.onboard.rollover --force 45 2026 --apply   # override the gate
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from typing import NamedTuple

from football import config
from football.client import CachedClient
from football.status import TERMINAL

#: How far ahead of a Season's first match we are willing to roll. The floor is 7 —
#: the Match Preview horizon (ADR 0040) — below which the opening matchday cannot be
#: previewed; three weeks leaves a fortnight of slack for a missed nightly.
LEAD_DAYS = 21

#: How far back an unplayed Fixture in the outgoing Season still counts as a live
#: frontier. Older than this it is a ghost the provider never settled (on 2026-08-10 the
#: FA Cup carried two non-terminal fixtures dated 2025-08-05) and must not block forever.
STALE_DAYS = 7


class Verdict(NamedTuple):
    """What rollover thinks should happen to one Competition, and why.

    `action` is one of:
      - "pinned"  — config already holds the provider's newest Season. Nothing to do.
      - "roll"    — a newer Season exists and both gates pass. `--apply` will take it.
      - "wait"    — a newer Season exists but a gate refused; `reason` says which.
      - "unknown" — no /leagues record to judge from (never fetched, or fetch failed).
    """
    league_id: int
    name: str
    current: int              # our pin — max(comp["seasons"])
    newer: int | None         # the provider's newest Season, when it is ahead of ours
    action: str
    reason: str               # "", "not-imminent", "frontier-open", "no-record", …
    starts: str | None        # the provider's start date for `newer`
    due: str | None           # for a "not-imminent" wait, the date G1 will pass

    def line(self) -> str:
        """One operator-facing line — the nightly log's replacement for the old warning."""
        if self.action == "roll":
            return (f"⚠ NEW SEASON READY: {self.name} {self.newer} (starts {self.starts}) — "
                    f"pinned to {self.current}; roll with "
                    f"`python -m football.onboard.rollover {self.league_id} --apply`")
        if self.action == "wait" and self.reason == "not-imminent":
            return (f"· new season pending: {self.name} {self.newer} starts {self.starts} — "
                    f"not rolling before {self.due}")
        if self.action == "wait" and self.reason == "frontier-open":
            return (f"⚠ NEW SEASON BLOCKED: {self.name} {self.newer} — season {self.current} "
                    f"still has unplayed fixtures; rolling now would strand them")
        if self.action == "unknown":
            return f"· new season unknown: {self.name} — no /leagues record to judge from"
        return f"· {self.name}: pinned to {self.current} (provider agrees)"


def _shift(day: str, days: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=days)).isoformat()


def _season_record(record: dict, year: int) -> dict:
    for s in record.get("seasons", []):
        if s.get("year") == year:
            return s
    return {}


def classify(comp: dict, record: dict | None, current_fixtures: list[dict],
             today: str, lead_days: int = LEAD_DAYS) -> Verdict:
    """Judge one Competition. Pure — no network, no disk, so both callers share it.

    `record` is the provider's /leagues payload for this league (Refresh force-refreshes
    it nightly; the CLI reads it from cache). `current_fixtures` is the cached fixture
    list for the Season we are pinned to — the outgoing frontier G2 inspects.
    """
    league_id, name = comp["league_id"], comp["name"]
    current = max(comp["seasons"])

    if not record:
        return Verdict(league_id, name, current, None, "unknown", "no-record", None, None)

    years = [s.get("year") for s in record.get("seasons", []) if s.get("year")]
    newest = max(years) if years else None
    if newest is None or newest <= current:
        return Verdict(league_id, name, current, None, "pinned", "", None, None)

    starts = _season_record(record, newest).get("start")

    # G1 — imminence. A Season the provider has merely *announced* is not one to collect;
    # without this, a February cup is probed every night from August onward.
    if not starts:
        return Verdict(league_id, name, current, newest, "wait", "no-start-date", None, None)
    due = _shift(starts, -lead_days)
    if today < due:
        return Verdict(league_id, name, current, newest, "wait", "not-imminent", starts, due)

    # G2 — the outgoing frontier is closed. Rolling makes `newer` the only Season any
    # recurring job touches, so an unplayed Fixture left behind here is never collected.
    cutoff = _shift(today, -STALE_DAYS)
    for f in current_fixtures:
        fx = f.get("fixture") or {}
        status = ((fx.get("status") or {}).get("short")) or ""
        date = (fx.get("date") or "")[:10]
        if status not in TERMINAL and date >= cutoff:
            return Verdict(league_id, name, current, newest, "wait", "frontier-open",
                           starts, None)

    return Verdict(league_id, name, current, newest, "roll", "", starts, due)


def add_season(league_id: int, year: int) -> bool:
    """Append one Season to a Competition in the registry, leaving everything else alone.

    Not `orchestrate._register`: that is a whole-entry upsert built for onboarding — it
    drops the row, rebuilds `seasons` from the provider record, and appends, which
    rewrites fields nobody asked it to touch and moves the Competition to the end of the
    file. Rollover's diff should be one added integer, so the file stays reviewable and
    a `git diff` shows exactly what a night changed.
    """
    rows = config._load_competitions()
    for row in rows:
        if row.get("league_id") != league_id:
            continue
        if year in row.get("seasons", []):
            return False
        row["seasons"] = sorted([*row["seasons"], year])
        config.COMPETITIONS_FILE.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
        )
        config.reload_competitions()
        return True
    raise SystemExit(f"League {league_id} is not a tracked Competition.")


def apply(client: CachedClient, verdict: Verdict) -> bool:
    """Fetch the new Season's calendar, then write the registry — in that order.

    G3, and the ordering is the point. `parse.build()` reads every registry Season's
    fixture list from a zero-budget client and does **not** guard that read
    (`football/build/parse.py`, and `football/build/scope.py` says so in its own comment),
    so a Season in the registry with nothing cached aborts the whole ~13-minute rebuild.
    Refresh force-fetching it "later that night" is not a guarantee: Refresh catches
    QuotaExceeded, breaks its loop, and rebuilds anyway. Fetching first means a failure
    here leaves the registry untouched and the tree exactly as it was.

    Returns True if the registry changed.
    """
    assert verdict.newer is not None
    payload = client.get(
        "fixtures", {"league": verdict.league_id, "season": verdict.newer}, force=True
    )
    fixtures = payload.get("response") or []
    if not fixtures:
        print(f"  {verdict.name}: provider has no calendar for {verdict.newer} yet "
              f"— registry unchanged")
        return False
    changed = add_season(verdict.league_id, verdict.newer)
    if changed:
        print(f"  {verdict.name}: season {verdict.newer} added "
              f"({len(fixtures)} fixtures cached)")
    else:
        print(f"  {verdict.name}: season {verdict.newer} was already listed")
    return changed


def review(client: CachedClient, comps: list[dict], today: str,
           lead_days: int = LEAD_DAYS, force_lookup: bool = False) -> list[Verdict]:
    """Classify each Competition from cached provider data — zero live calls by default.

    Both inputs are force-refreshed by every nightly Refresh, so reading them cache-first
    costs nothing and is at most one day stale. `force_lookup` re-fetches the /leagues
    records (one call per Competition) for a tree where Refresh has not run in weeks.
    """
    out: list[Verdict] = []
    for comp in comps:
        league_id = comp["league_id"]
        record = client.get("leagues", {"id": league_id},
                            force=force_lookup).get("response") or []
        fixtures = (client.peek(
            "fixtures", {"league": league_id, "season": max(comp["seasons"])}
        ) or {}).get("response") or []
        out.append(classify(comp, record[0] if record else None, fixtures, today, lead_days))
    return out


def _select(only: list[int]) -> list[dict]:
    if not only:
        return list(config.COMPETITIONS)
    by_id = {c["league_id"]: c for c in config.COMPETITIONS}
    unknown = [lid for lid in only if lid not in by_id]
    if unknown:
        raise SystemExit(f"Not tracked Competition(s): {' '.join(map(str, unknown))}.")
    return [c for c in config.COMPETITIONS if c["league_id"] in set(only)]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.onboard.rollover",
        description="Review (and optionally apply) Season rollovers (ADR 0045).",
    )
    ap.add_argument("league_ids", type=int, nargs="*", metavar="LEAGUE_ID",
                    help="review only these Competitions instead of every tracked one")
    ap.add_argument("--apply", action="store_true",
                    help="write the registry for every Competition judged ready; without "
                         "it this is a read-only review (the default, because this module "
                         "runs on the nightly path)")
    ap.add_argument("--lead-days", type=int, default=LEAD_DAYS, metavar="N",
                    help=f"how many days before a Season's first match it may be rolled "
                         f"(default {LEAD_DAYS}; the floor is the 7-day Preview horizon)")
    ap.add_argument("--force", type=int, nargs=2, metavar=("LEAGUE_ID", "YEAR"),
                    help="add this Season regardless of the gate — the escape hatch for a "
                         "wrong provider start date or a frontier fixture nobody will ever "
                         "settle. Still fetches the calendar first, so it cannot leave a "
                         "registry Season the rebuild has no cache for. Needs --apply.")
    args = ap.parse_args(argv)

    client = CachedClient()
    today = dt.date.today().isoformat()

    if args.force:
        league_id, year = args.force
        comp = _select([league_id])[0]
        forced = Verdict(league_id, comp["name"], max(comp["seasons"]), year,
                         "roll", "forced", None, None)
        print(f"Forcing season {year} for {comp['name']} (gate bypassed).")
        if not args.apply:
            print("Review only — re-run with --apply to write.")
            return
        apply(client, forced)
        return

    comps = _select(args.league_ids)
    verdicts = review(client, comps, today, args.lead_days)

    ready = [v for v in verdicts if v.action == "roll"]
    pending = [v for v in verdicts if v.action == "wait"]
    for v in ready + pending:
        print(v.line())
    if not ready and not pending:
        print(f"Every Competition ({len(verdicts)}) is pinned to the provider's newest Season.")

    if not args.apply:
        if ready:
            print(f"\nReview only — {len(ready)} ready. Re-run with --apply to write.")
        return

    if not ready:
        return
    print(f"\nApplying {len(ready)} rollover(s):")
    changed = [v for v in ready if apply(client, v)]
    if changed:
        print(f"\n{len(changed)} Season(s) added to "
              f"{config.COMPETITIONS_FILE.relative_to(config.ROOT)}. Next:")
        print("  git add -p && git commit    # the registry is committed input, not output")
        print("  uv run python -m refresh    # collects the new Season's Finals, rebuilds")
    print(f"\nRequests this run: live {client.live_requests} / cached {client.cache_hits}")


if __name__ == "__main__":
    main()
