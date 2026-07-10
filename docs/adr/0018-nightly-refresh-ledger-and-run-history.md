# Nightly Refresh: force-refresh the frontier, ledger the Finals, log every run

Every collector so far is a **backfill**: point it at a Competition and it fills the
raw cache once, cache-first, then rebuilds `football.db`. Nothing re-collects. But
the seven built-in leagues (and most registered ones) have a **live current season**
that gains matches every week, and cache-first actively hides them: the season
fixture list is cached as a single `fixtures?league&season` file, so once written it
never surfaces a match played since. Worse, the backfill fetched `fixtures/players`
for every fixture in that list *including ones not yet played*, caching an **empty**
response that never self-corrects when the match is later played — a **stale-empty**
trap. We want an unattended nightly job (cron) that pulls only the new data, logs
which Teams changed, and reports the API calls it spent — without re-collecting the
immutable past or breaking the cache-first contract everything else depends on.

A **Refresh** (CONTEXT.md) is that job: it re-collects only the **mutable frontier** —
the current Season of every Competition — and leaves immutable past Seasons untouched.

**Decisions:**

- **Refresh targets the current Season of every Competition, config-pinned.** The
  current Season is `max(seasons)` in each Competition's config, leagues and cups
  alike. Past Seasons are never re-fetched (they cannot change). Refresh is a distinct
  entrypoint, the top-level `refresh` package (`python -m refresh`), not a new flag on
  `orchestrate`. All of its runtime state (ledger, run-history DB, per-run logs) lives
  inside `refresh/` beside the code, not under `data/` — see `refresh/README.md`.

- **The frontier is force-refreshed; everything else stays cache-first.** Two cache
  entries per Competition are deliberately re-fetched live each night — the `/leagues`
  record (refreshes provider metadata and exposes the latest season) and the current
  Season's `fixtures?league&season` list (surfaces newly-played matches). This needs a
  new force-refetch capability on `CachedClient` (bypass-and-overwrite for a given
  key); every other endpoint stays purely cache-first, so an already-collected fixture,
  player, or team is still fetched once ever.

- **Per-fixture data is collected once a Fixture is Final, gated by a ledger.** A
  Fixture is **Final** (CONTEXT.md) iff its status is `FT`/`AET`/`PEN`. Refresh runs
  the per-fixture stages (squad/goals, events, team match stats — Coverage-gated
  exactly as today) only for Final fixtures **not already in the ledger**, then records
  them. This is what defeats the stale-empty trap: a Final fixture absent from the
  ledger is force-collected even if a stale empty cache file exists for it, so the
  legacy current-season empties heal on the first Refresh. Non-Final fixtures
  (scheduled, live, `PST`, `CANC`) are skipped and revisited for free on later nights
  until they become Final or their Season rolls over.

- **The ledger is a standalone `refresh/refresh_ledger.json`.** A map of
  `fixture_id -> {status, collected_date}` — richer than a bare id set so a run is
  auditable ("which night did this match land, in what state"). Fixture ids are global
  provider ids, so one flat map spans every Competition. A fixture is written to the
  ledger only after *all* its applicable per-fixture stages succeed, so an interrupted
  run re-does a half-collected match rather than skipping it.

- **Refresh runs the full enrichment chain, not just match data.** A new Final match
  can surface a midseason signing or a new career team; leaving them un-enriched would
  hole the modeled store (a squad member with no bio/career/Team Profile, a broken
  League of Origin). So Refresh runs the same seven-stage chain as `orchestrate._collect`
  (fixtures → squad/goals → bios → careers → Team Profiles → events → team stats). It is
  naturally incremental: cache-first makes every already-seen player and team a free
  cache hit, so the only live calls beyond the fixture data are for people and clubs
  seen for the first time that night.

- **Run history and per-call accounting live in a standalone `refresh/refresh.db`.** Two
  tables: `refresh_run` (one row per night — date, duration, competitions refreshed,
  Final fixtures collected, Teams updated, live calls, cache hits, outcome) and
  `refresh_call` (run × Competition × endpoint → live-call count). The counters are the
  client's existing `live_requests`/`cache_hits`, surfaced rather than newly plumbed.
  This store lives under `refresh/`, **separate from `football.db` on purpose**: `parse.build()` drops and
  rebuilds `football.db` in full every night, so any run-history table inside it would
  be wiped — the whole point of the collector is to persist a trend the rebuild cannot
  touch.

- **Each run writes one timestamped `.txt` log**, `refresh/logs/refresh-YYYY-MM-DD.txt`,
  grouped by Competition. Per section: **Updated** Teams (each with its new Final
  fixtures — opponent, date, score) and **Unchanged** Teams (in the current Season, no
  new Final match). A club playing several Competitions is reported per Competition, so
  "updated in the Champions League, unchanged in La Liga" reads naturally. A summary
  header carries the timestamp, counts, and the night's live/cache call totals.

- **Rollover is config-pinned with a loud warning, never silent or automatic.**
  Because parse only models seasons in config, a season the provider has opened but
  config lacks would be collected into cache yet **never appear in the DB**. Refresh
  already re-fetches each `/leagues` record, so it compares the provider's latest
  season to config and, when the provider is ahead, writes a prominent
  `⚠ NEW SEASON AVAILABLE: <name> <year> — add to config` line to the log and run
  summary. Adding the season stays a deliberate one-line human edit.

- **Refresh always ends with a full rebuild, even after a partial run.** Because
  Refresh is ledger-gated and cache-first, the raw cache is always internally
  consistent — a half-finished run just means fewer Finals landed, never corruption.
  So unlike `orchestrate` (which skips the rebuild on `QuotaExceeded`), Refresh always
  finishes with `parse.build()`, then re-scopes each `data/<slug>.db` already present on
  disk (a zero-API re-parse), records `outcome='interrupted'` if it was cut short, and
  exits non-zero so cron surfaces the failure.

- **Concurrency and scheduling are the operator's, via `flock` + crontab.** We deliver
  the entrypoint and a documented `flock`-wrapped crontab line (recommended 04:00 server
  time, late enough for the day's matches to be Final) rather than touching the crontab.
  `flock` prevents a manual run or an overrun from double-collecting; the existing
  `parse` `.build.lock` still guards the rebuild underneath.

## Considered Options

- **Stateless re-collection: force re-fetch every current-season Final each night, no
  ledger.** Rejected: it re-pulls the whole current Season's Finals every night (a few
  hundred wasted calls) and, worse, an empty per-fixture cache file is ambiguous between
  "not played when fetched" and "Final but genuinely eventless" — with no recorded
  status you either re-fetch everything forever or trust the empties and never heal. The
  ledger's `status` field is exactly the disambiguator.

- **Auto-rollover: detect the provider's newest season and append it automatically.**
  Rejected for now: the seven built-in Competitions are hardcoded in `config.py`, so
  auto-extending them means a cron job rewriting source code, and it would silently
  start collecting a brand-new (possibly preseason/friendly-only) season with no human
  in the loop. Warn-and-wait keeps rollover deliberate; true auto-rollover first needs
  the built-ins migrated into the writable registry (deferred).

- **Incremental parse instead of a nightly full rebuild.** Rejected: `parse.build()` is
  a battle-tested zero-API full re-parse that every collector already ends with; a
  few minutes at 04:00 is a non-problem. An incremental parser is real complexity and
  a second code path to keep correct, for no operational gain.

- **Match-data-only Refresh (skip bios/careers/Team Profiles).** Rejected: it holes the
  enriched layer for every midseason debutant and breaks their League of Origin until a
  manual backfill. The full chain is nearly free incrementally (cache hits), so the gap
  buys nothing.

- **Run history as a table in `football.db`, or an appended `refresh_history.csv`.**
  The in-`football.db` table is wiped by the nightly rebuild — disqualifying. CSV
  survives but is a flat blob; a standalone `refresh.db` with a run table and a
  per-call breakdown table is queryable from marimo/SQL and keeps the auditable
  history the collector exists to provide.
