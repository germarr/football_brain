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

  *Amendment (2026-07-17): the ledger records the Coverage it collected under, so a
  widened Coverage re-heals.* The gate above — "collect a Final's per-fixture stages iff
  it is **not already in the ledger**" — silently assumes a Fixture's set of *applicable*
  stages is fixed. It is not. Those stages are Coverage-gated (squad/goals behind
  `statistics_players`, team match stats behind `statistics_fixtures`), and a Season's
  Coverage **widens over its life**: the provider opens a brand-new Season (e.g. Liga MX
  2026 Apertura) with every Coverage flag `false` and flips them `true` days later, once
  it starts populating player and fixture stats. Events are collected *unconditionally*,
  so a Final played on opening night is fully processed *for its then-Coverage* — events
  only — and written to the ledger. When Coverage later flips `true`, the ledger-absence
  gate no longer fires, so the now-applicable squad/stats stages never run: the Fixture
  keeps its `event` rows forever but never gains a Squad Entry or Team Match Stat. This is
  the **stale-empty trap reincarnated one level up** — not a stale-empty *cache file*, but
  a stale-*complete* ledger entry, recorded true against a Coverage that has since grown.
  (Observed live: fixtures 1550894/1550895, Apertura matchday 1, have `event` rows but
  zero Squad Entries; their `fixtures/players` cache is *absent*, not empty — the fetch was
  Coverage-gated away — and both are already ledgered `FT`, so no future run re-collects
  them.)

  Decision: **the ledger entry records the Coverage it was collected under, and the gate
  re-collects a Final whose current Coverage strictly widens the recorded one.**
  - The ledger value gains a `coverage` fingerprint — the Coverage-gated stages that
    actually ran that night: `{"players": bool, "fixture_stats": bool}`. Events is
    unconditional and never part of the fingerprint. `record()` takes the applied Coverage
    alongside `status`/`collected`.
  - Step 3's selector changes from *"Final and `fid not in ledger`"* to *"Final **and**
    (`fid not in ledger` **or** current Coverage widens the recorded fingerprint)"*, where
    *widens* means some flag now `true` that the fingerprint has `false`. A re-collect runs
    only the newly-applicable stages (cache-first via `_heal_get`, so the Coverage-false
    window's absent cache is a clean live fetch), then rewrites the entry with the wider
    fingerprint and the new date. Once a Fixture is recorded under full Coverage it never
    re-triggers.
  - Coverage only ever widens in practice (the provider adds stats, never removes), so
    *narrowing* is ignored — it would strand no already-collected data anyway.
  - **Migration:** existing entries have no `coverage` field. Treat a missing fingerprint
    as *nothing covered* so it is re-evaluated against current Coverage on the next run.
    This is close to free: a Final collected under true Coverage already has a present,
    non-empty `fixtures/players` cache, so `_heal_get` returns a counted **cache hit** and
    the only effect is stamping an accurate fingerprint; live calls happen solely for the
    genuinely-stranded Finals (players cache absent). Re-evaluation is bounded to each
    Competition's current Season — past Seasons never enter step 3.

  This keeps the ledger's original virtue (its `status` field disambiguates
  "not-played-yet" from "Final-but-eventless") and adds the missing second axis: *which
  Coverage-gated stages a recorded Final actually carries*, so a Season that gains Coverage
  mid-life heals exactly the Fixtures collected before the flip, and nothing else. See the
  new rejected options below.

  *Amendment 2 (2026-07-17): the season Coverage flag lags the data, so an expected stat is
  probed optimistically rather than waited on.* Amendment 1 heals a Final **when the season
  flag flips**. But a live probe of the two stranded fixtures showed the flag is not just
  coarse, it **lags**: with Liga MX 2026 still flagged `statistics_players=false`,
  `fixtures/players` for 1550895 already returned 40 players and `fixtures/statistics` 36
  stat lines — while 1550894 was still empty. So the per-*Season* flag is a poor gate for
  *per-Fixture* collection: real data can sit available-but-uncollected for days until the
  flag catches up, and it lands unevenly across a matchday. Waiting on the flag needlessly
  delays data we could already have.

  Decision: **decouple collection from the season flag — collect a stat stage whenever the
  data is actually there — while bounding the probing so a genuinely stats-light Fixture is
  not re-fetched forever.** Two signals bound it:
  - **Expected Coverage.** From the same force-refreshed `/leagues` record, a stage is
    *expected* for the current Season iff the Competition's most recent *completed* Season
    carried it. Liga MX 2026 is expected (2025 had both flags); Liga MX Femenil — stats-light
    every Season — is not. Only an *expected* stat on a stats-light current Season is probed
    optimistically; a genuinely stats-light Competition is never probed on a hunch, so it
    costs zero extra calls.
  - **A recency window** (`OPTIMISTIC_PROBE_DAYS`, 14). Optimistic probing is limited to
    Finals played within the window of the run date. A stat that never arrives (an expected
    Competition the provider quietly leaves stats-light, or one match with no data) stops
    being probed once it ages out — the cost is bounded to ~one window of recent Finals per
    night, not the whole Season.

  Mechanically, the selector/`needs_collection` of amendment 1 becomes `_stages_to_attempt`,
  which returns, per stage: **collected already?** skip; **season covers it?** collect
  definitively (first collection *or* a flag-flip widen — amendment 1 subsumed here);
  **stats-light but expected and recent?** probe optimistically. A stat stage's fingerprint
  flips `true` when the season covers it **or** an optimistic probe actually returned data;
  an empty probe leaves the stage owing, to retry next night until it lands or ages out.
  Events stay a once-only, is-new-gated fetch. A Fixture is reported "Updated" (and counted)
  only when it is new or a stage we had recorded `false` just landed — a legacy entry being
  back-stamped, or an empty re-probe, is silent and does not churn its `collected` date.

  Net effect for the motivating case: on the **next** Refresh — with the flag still `false` —
  1550895's squad and team stats are collected, and 1550894 keeps being probed until its data
  lands. No manual de-ledgering, and no wait for the provider to flip the flag.

  *Amendment 3 (2026-07-17): the flag lags per-Fixture too, so a covered Season's empty Final
  is left owing within the window rather than trusted as empty.* Amendment 2 stamped a stage
  `true` "when the season covers it **or** a probe returned data" — i.e. once the season flag
  is on, an empty payload was trusted definitively. But the lag amendment 2 documented runs
  below the Season as well: with Liga MX 2026 **covered**, `fixtures/players` for 1550895
  returned 40 players while 1550894 — same matchday, same league — returned nothing, because
  the provider populates unevenly across a matchday. Trusting the empty stamped 1550894
  `players=true` off a zero-row payload and never retried it: the original stale-empty trap,
  reincarnated one level lower — the flag is on, but this Fixture's data has not landed yet.
  Decision: **the fingerprint flips `true` on *data*, not on the season flag.** A stage is
  stamped `true` iff the fetch returned data, **or** it came back empty *and* the Final has
  aged past `OPTIMISTIC_PROBE_DAYS` (a genuinely-empty Final we stop chasing). A covered
  Final that fetches empty while still recent is left owing and retried — exactly the
  treatment a stats-light expected Final already got — unifying both under one recency rule:
  an empty within the window is *lagging*, not *absent*. This also folds the season-covers-it
  branch of `_stages_to_attempt`'s stamping into the same test; the *attempt* logic is
  unchanged (a covered stage is still attempted whenever its fingerprint is absent). One-off
  remediation: 35 recent Finals (incl. 1550894) that amendment 2 had stamped `true` off empty
  payloads were reset to owing in the ledger so the new rule re-evaluates them.

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

  *Amendment (2026-07-11): `--scope-only` for interactive iteration.* The nightly cron
  keeps this default. But the full `parse.build()` is ~13 min and dominates a run's
  wall-clock (the frontier collection is seconds now that the plan is un-throttled),
  while a scoped `data/<slug>.db` re-parse is ~6 s. When you are at the keyboard and only
  read a scoped DB (e.g. `world-cup.db`), the full rebuild is dead weight. `--scope-only`
  collects the frontier and runs only `_rescope_existing()`, skipping `parse.build()`, so
  the scoped DB you read refreshes in seconds. The trade-off: `football.db` is left stale
  until the next full run — acceptable because that is deliberate and self-healing (the
  new Finals are already ledgered and cached, so the next full rebuild simply picks them
  up; nothing re-collects). This is a *skip*, not the "incremental parse" rejected below —
  no second parser, just one fewer step. `--no-rebuild` (skip both) and `--scope-only`
  (skip only the full rebuild) are mutually exclusive.

  *Amendment 2 (2026-07-19): `--only <league_id …>` to scope the collection itself.* The
  nightly cron still refreshes every Competition — that is the point of a nightly run, and
  keeping the whole current-Season frontier warm makes the next full rebuild free (every new
  Final is already ledgered and cached). But the frequent partial path — `football.refresh_pg`
  (ADR 0027), which pushes fresh results to the Postgres Published Store many times a day for
  the Liga MX + MLS pair — paid the whole config's forced-call cost on every run: one forced
  `/leagues` + fixture-list pair per Competition, ~84 calls for all ~42 tracked, of which ~40
  are for Competitions it never publishes. `--only` filters the collection loop to the named
  league ids (order preserved from config; an untracked id is a hard error, never a silent
  narrowing), and `refresh_pg` now passes exactly the set it is about to publish (`--all`
  publishes and so refreshes everything). This scopes *which Competitions are collected*, an
  orthogonal axis to `--scope-only`/`--no-rebuild` (*which rebuild steps run*), so it composes
  with either. The trade-off mirrors `--scope-only`'s: the un-named Competitions' current
  Seasons stay frozen in cache until the next full `python -m refresh`, which force-refreshes
  and self-heals them — nothing is lost, only deferred to that run's expense instead of the
  partial run's. The nightly full run therefore stays authoritative; `--only` is purely a
  quota lever for high-frequency partial runs.

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

- **Coverage re-heal: re-collect every current-season Final each night (drop the ledger
  gate for the gated stages).** Rejected — it is the stateless approach this ADR already
  rejected, just scoped to the gated stages: it re-pulls the whole current Season's Finals
  nightly. The per-Fixture Coverage fingerprint re-collects only on an actual Coverage
  change, not every night.

- **Coverage re-heal: a per-Competition "Coverage flipped" detector instead of a
  per-Fixture fingerprint.** When a `/leagues` record shows a flag flip since last run,
  re-collect that Competition's current-Season Finals. Rejected: simpler but coarse — it
  cannot tell which Finals predate the flip (some current-Season Finals were collected
  *after* it, already under full Coverage) so it re-collects indiscriminately, and it drops
  the per-Fixture audit of what each recorded match actually carries. The fingerprint on
  the ledger entry is the precise, self-describing form.

- **De-ledger the stranded Finals by hand.** The immediate unblock for 1550894/1550895
  (delete their ids from `refresh_ledger.json`, let the next Refresh re-collect once
  Coverage flips). Kept as the one-off remedy, rejected as *the* fix: it does not close the
  trap — the next brand-new Season reopens it on its opening matchday.

- **(Amendment 2) Wait for the season flag, don't probe at all.** The amendment-1-only
  design: heal purely when `statistics_*` flips. Rejected once the live probe proved the
  flag lags the data by days — waiting strands data that already exists (1550895's stats
  were served while the flag was still `false`). Optimistic probing collects it now; the
  flag-flip path remains as the definitive backstop for whatever the probe didn't catch.

- **(Amendment 2) Bound probing with a persisted per-Fixture probe counter** (probe an
  empty stats-light Final up to K nights, then give up), instead of *expected Coverage +
  recency window*. Rejected: it burns K probes on **every** Final of a genuinely stats-light
  Competition before quitting, whereas "expected" (does the prior Season carry the stat?)
  skips those Competitions for zero cost, and needs no new ledger field — the recency window
  reads dates the payload already carries. The counter also has to be persisted and reasoned
  about on resume; the window is stateless.

- **(Amendment 2) Probe optimistically with no recency bound.** Rejected: an *expected*
  Competition the provider quietly leaves stats-light (or a single match that never gets
  stats) would be re-probed every night for the whole Season, growing without limit as the
  fixture list grows. The window caps live cost to ~one window of recent Finals per night.

- **(Amendment 3) Trust a covered Season's empty payload — stamp it collected immediately.**
  The amendment-2 behaviour: once the season flag is on, an empty `fixtures/players` /
  `fixtures/statistics` means genuinely-eventless, so stamp and stop. Rejected: the flag lags
  per-Fixture as well as per-Season (1550894 was empty while its same-matchday sibling 1550895
  had 40 players, both under a covered Liga MX 2026), so an immediate stamp strands a Final
  whose data is merely late — the exact trap this ADR closes. Deferring the stamp until the
  Final ages out of the recency window costs at most one force-refetch per recent empty Final
  per night (bounded, self-terminating) and reuses the window already in place for stats-light
  probing, at no new state.

- **Auto-rollover: detect the provider's newest season and append it automatically.**
  Rejected for now: the seven built-in Competitions are hardcoded in `config.py`, so
  auto-extending them means a cron job rewriting source code, and it would silently
  start collecting a brand-new (possibly preseason/friendly-only) season with no human
  in the loop. Warn-and-wait keeps rollover deliberate; true auto-rollover first needs
  the built-ins migrated into the writable registry (deferred).

  **Superseded in part by ADR 0045.** The conclusion holds — the write is still manual,
  because a Registry is committed input — but the warning it settled for was
  unactionable, and the stated worry was the wrong one. A new Season's fixture list is
  the published calendar, not friendlies; the actual hazard is that rolling *abandons*
  the outgoing Season, since this ADR's frontier is only ever `max(seasons)`. ADR 0045
  keeps warn-and-wait and makes the warning say whether taking the Season is safe today.

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
