# Season rollover is gated, not guessed

---
Status: accepted — supersedes the **Auto-rollover** rejected alternative in ADR 0018
(Nightly Refresh) and replaces its "NEW SEASON AVAILABLE" warning. Writes the
**Competition registry** of ADR 0019; bounded by ADR 0028 (Registries are committed
input) and ADR 0040 (the seven-day Match Preview horizon). Adds **Season Rollover** to
CONTEXT.md and amends the **Season** entry.
---

Refresh targets exactly one Season per Competition — `max(comp["seasons"])`, the **pin**.
When the provider opens a new Season, the pin is behind, and ADR 0018 answered that with
a log line telling the operator to edit the registry by hand:

```
⚠ NEW SEASON AVAILABLE: La Liga 2026 — add to config (currently pinned to 2025)
```

It fired for four Competitions on 2026-08-10 and had been firing, unread, for weeks. The
warning is not wrong; it is unactionable. It says a newer Season *exists*, which is the
one thing that is never in doubt, and stays silent on the only question the operator
actually has: **is it safe to take it today?** Two of those four could have been rolled
that morning. The other two start in October and February.

This adds `football/onboard/rollover.py`: a gate that answers that question per
Competition, reported by every nightly Refresh and applied by an explicit command.

Rollover stays a deliberate act. The registry is committed input, reviewed as a diff
(CONTEXT.md), so a cron job may say "this one is ready" and may not write it. What
changes is that "ready" is now a computed claim rather than the operator's recollection
of when the Turkish league starts.

Four decisions were not obvious.

**Decisions:**

- **Gate on the new Season's start date, not on the provider's `current` flag and not
  on `max(year) > ours`.** `current: true` was set on all four pending Competitions,
  including a Super Cup whose first match is 2027-02-02 — six months out. The flag means
  "query this Season by default", not "this Season has begun", which is why the repo has
  never read it and should continue not to. A bare `max(year) > ours` inherits the same
  fault. The gate instead reads `seasons[].start` from the same `/leagues` record and
  requires it within `LEAD_DAYS = 21`. Rejected alternative: a hand-maintained rollover
  date per Competition — a second thing to keep true that the provider already tells us.

- **The real hazard is the Season being left behind, not the one being taken.** ADR 0018
  worried about "a possibly preseason/friendly-only season". Empirically that worry is
  thin: a new Season's fixture list is the full published calendar (La Liga 2026 = 380
  fixtures dated Aug–May), and the provider files friendlies under its own league 667,
  outside any Competition's Season. The unnamed hazard is the other end. Because Refresh
  only ever touches the pin, moving it **abandons the outgoing Season** — any Fixture
  still unplayed there is collected by nothing, ever again, with no error anywhere. So
  the second gate requires the outgoing Season to have no non-`TERMINAL` Fixture dated
  within `STALE_DAYS = 7`. It reads the fixture list Refresh force-refreshed hours
  earlier, so it costs nothing. The age filter is not decoration: the FA Cup carries two
  `NS` fixtures dated 2025-08-05 that the provider never settled, and a bare
  "any unplayed Fixture" test would refuse that Competition every night forever.

- **Not gated on a Final having been played.** The conservative-looking choice is a
  regression here. Match Previews are built over a seven-day horizon (ADR 0040) and a
  Fixture cannot have a Preview unless its Season is in the registry, so a Final-gated
  rollover blacks out every Competition's **opening matchday** in the blog, every season,
  by construction. It also contradicts practice: on 2026-08-10 thirteen Competitions were
  already pinned to a 2026 Season holding a full calendar and zero Finals. Rolling ahead
  of kickoff is correct; `LEAD_DAYS` has a floor of 7 for exactly this reason.

- **Fetch the new Season's calendar first, then write the registry.** `parse.build()`
  reads every registry Season's fixture list from a zero-budget client and does **not**
  guard that read — `football/build/scope.py` says so in its own comment, having built
  its own probe to work around it. A Season in the registry with nothing cached therefore
  aborts the entire ~13-minute rebuild. "Refresh will force-fetch it later that night" is
  not a guarantee: Refresh catches `QuotaExceeded`, breaks its loop, and rebuilds anyway,
  so a quota stop before the newly-rolled Competition leaves exactly that state. Ordering
  the fetch first means a failure leaves the registry untouched. It also makes `--force`
  safe to offer: the escape hatch bypasses the two judgement gates but not this one.

**Consequences:**

- The nightly log's warning block becomes a verdict block, ready-to-roll first: `NEW
  SEASON READY … roll with <the exact command>`, `new season pending: … not rolling
  before 2026-09-25`, or `NEW SEASON BLOCKED: … season 2025 still has unplayed fixtures`.
  `CompResult.newer_season: int | None` becomes `CompResult.rollover: Verdict | None`.
- `scripts/nightly.sh` runs the review after Refresh, into `refresh/logs/rollover-cron.out`.
  Read-only, and no API calls: both payloads it judges were force-refreshed minutes before.
- `add_season()` appends one integer and preserves file order, rather than reusing
  `orchestrate._register` — a whole-entry upsert that rebuilds `seasons` from the provider
  record and moves the Competition to the end of the file. A rollover's diff should be one
  line, because that diff is the review.
- Applying still leaves the new Season uncollected until the next `python -m refresh`
  (incremental) or a `python -m football.onboard.orchestrate <id>` (one-shot backfill).
  The command prints both, along with the `git commit` the registry expects.
- The gate can be wrong in the operator's favour only by waiting. Both refusals are
  overridable with `--force LEAGUE_ID YEAR --apply`.
