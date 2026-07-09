# Extract a single Competition into its own SQLite DB from the raw cache

`football.parse` builds one store, `data/football.db`, holding **every**
Competition — it drops and rebuilds all tables over `config.targets()` (all
competitions × seasons). Sometimes we want just one Competition in isolation — e.g.
a Premier League–only `data/premier-league.db` to hand off, explore, or ship
without the other leagues. The raw cache (Layer 1) already holds everything we've
collected, so this is a **re-parse**, not a re-collection: no network, no quota.

**Decisions:**

- **Parameterize `parse.build()`, don't fork it.** `build(db_path=None,
  targets=None)` defaults to the full store (`config.DB_PATH`, `config.targets()`)
  — the existing behaviour byte-for-byte, so `orchestrate`/`cups` are untouched.
  Passing a `db_path` and a filtered `targets` list writes a scoped DB through the
  *same* memory-batched pipeline (venues → fixtures/squads → bios → careers →
  events → team stats). Duplicating that pipeline — with its per-batch commit/
  expunge discipline that keeps the ~1M-row build inside memory (ADR 0002/0009) —
  would be the real risk; the loop is identical, only the output file and the
  `(competition, season)` set differ.

- **A thin entrypoint, `football.scope`, cache-only like `parse`.** It uses a
  zero-budget `CachedClient` (a miss raises, never fetches), so it can only ever
  scope data that's already collected. An uncollected Competition errors out with
  the `orchestrate <id>` command to collect it first, rather than silently
  spending API quota. Create and delete are one command behind a `--delete` flag.

- **Name the file `data/<slug>.db` from the canonical name.** `Premier League` →
  `data/premier-league.db`; accents are stripped to plain ASCII (`Brasileirão` →
  `brasileirao`). The slug derives from *our* canonical name (CONTEXT.md), so it
  inherits the "two Serie A's" disambiguation for free. The build lock is
  per-`db_path` (`<slug>.build.lock`), so a scoped build never blocks — or is
  blocked by — the full one (ADR 0009's concurrent-build lock).

- **Keep Career Stints in full — the whole cross-competition history.** A
  `PlayerTeam` row is deliberately *not* FK-constrained to the `Team` table
  (CONTEXT.md: "Career Stint"), so a scoped DB keeps every stint for each of its
  players — including out-of-scope clubs and national teams. A Premier League DB
  therefore carries a PL player's Real Madrid / Portugal stints, with `team_id`s
  that are intentionally absent from its own 34-team table. "All the information we
  have about this Competition" includes its players' full careers, not a truncated
  view.

- **Resolve a player's bio from any cached season, not the first-seen one.**
  Bios are cached keyed by `(player_id, season)`, and the build picks the season a
  player is *first seen* in. A scoped build sees only its one Competition, so it
  computes a different first-seen season than the full collection cached the bio
  under — e.g. a Liga MX–only build looks up a cross-competition player under a
  Liga MX season, but the bio was cached under his Serie A / Argentina season — and
  the exact `(pid, season)` file is absent, silently degrading the row to a
  name-only stub. Since the biography is season-independent, `parse._fetch_bio`
  prefers the requested season, then falls back to *any* cached season's bio for
  that player (indexed once up front). This is the only per-season cache key in the
  pipeline; fixtures/events/stats key on the fixture and career stints on the
  player, so they scope without this hazard.

- **`--delete` removes only the scoped `.db` (and its lock), never the raw cache.**
  The cache is the shared, expensive-to-rebuild asset and is common to every
  league; a scoped DB is cheap to regenerate from it. Delete is idempotent and
  never touches `data/football.db`.

- **Partial collection scopes cleanly.** `scope` probes each of the Competition's
  seasons against the cache and builds only the ones present, reporting any it
  skipped. This both handles a half-collected league and keeps the build from
  crashing on the one fixture read (`fetch_fixtures`) that `parse` does *not* guard
  with a `QuotaExceeded` catch.

## Considered Options

- **A `--league` filter and an output path on `parse` itself.** Rejected as the
  *user-facing* surface: `parse` is the full-store rebuild that `orchestrate`/`cups`
  call, and overloading its CLI with scoping flags muddies that. The underlying
  `build()` *is* parameterized (above); `scope` is the separate, cache-only,
  slug-naming, delete-capable driver — the same split as `collect` vs
  `orchestrate` (ADR 0009).

- **Collect-if-missing (auto-run `orchestrate` when a Competition isn't cached).**
  Rejected: it would let a "make me a small DB" command silently launch a
  multi-thousand-request collection and hit the daily cap. Scoping is a pure,
  fast, offline re-parse by design; collection stays an explicit, separate step.

- **Restrict Career Stints to in-scope teams (or drop the `playerteam` table).**
  Rejected: it contradicts the definition of a Career Stint and discards the
  cross-competition career view that is one of the dataset's distinctive assets.
  The out-of-scope `team_id`s are expected — the column was never an FK.

- **Copy/prune the full `football.db` with `DELETE FROM … WHERE league_id != ?`.**
  Rejected: fragile against the FK-linked tables (players, career stints, events,
  venues, team stats would each need their own cascade), and it needs the full DB
  to already exist. Re-parsing from the cache is simpler and correct by
  construction — the scoped DB contains exactly what a from-scratch single-league
  build would.
