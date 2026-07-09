# Team Profile: a directory of every career team, enriched from the provider

A Career Stint (`PlayerTeam`) records every team a player has ever appeared for —
~7,000 distinct teams across the store — but stores only the team's id and a
denormalized name. It carries **no league and no country** (ADR 0006; the
players/teams endpoint exposes nothing more), so "what league did this player come
from" cannot be answered from our data: only ~1,466 of those 7,045 teams appear in
a Fixture we collect, and the other 5,579 (obscure lower divisions, foreign clubs,
youth national sides) are unresolvable. This came to a head building the Liga MX
"League of Origin" analysis, where a large **Unknown** bucket was pure
missing-mapping, not missing players.

We add a **Team Profile** table: one enriched row per distinct career team, giving
each club an identity (country, founded year, crest, national-team flag) and a
single **representative league** — persisting, once, the team→league answer that
League of Origin previously re-derived ad-hoc from fixtures. A new collection stage
fetches the two provider endpoints that carry this (`/teams?id`, `/leagues?team`);
`parse.build()` reads that cache to build the table; a standalone `football.teams`
entrypoint backfills and catches up.

**Decisions:**

- **One row per distinct `PlayerTeam.team_id` — all 7,045 career teams, including
  national and youth sides.** The table's job is to be the complete directory
  behind *every* Career Stint reference, so no career team is unresolvable.
  National teams are kept and flagged `is_national` (so a consumer can filter to
  clubs, as League of Origin does) rather than excluded.

- **A single representative league per team, not the full history.** `/leagues?team`
  returns every league a club ever played in — domestic divisions across
  promotion/relegation, domestic cups, continental cups. We store one: the
  `type = "league"` competition in the team's **own country** with the **most
  recent** season (Grenoble → Ligue 2, not its old National 1/2 spells nor Coupe de
  France; Barcelona → La Liga, never the Champions League). This matches "the league
  this club plays in," keeps the row a clean directory entry, and never lets a
  continental cup stand in as an origin. It is a **snapshot** — a club now promoted
  shows its current division, not the one it was in when a player left. The raw
  `/leagues?team` JSON stays cached, so a time-accurate team↔league bridge can be
  built later with zero new fetches.

- **Full club dossier, venue excluded.** We store `code`, `country`, `founded`,
  `is_national`, `logo` from `/teams?id` alongside the representative league. The
  `venue` block the endpoint also returns is **not** stored — we already model
  stadiums in the `Venue` table, and the raw JSON keeps it for any future backfill.

- **Cache-first for tracked teams; provider only for the rest.** A team that appears
  in one of our own Fixtures already has its league in cache, so we take the
  representative league from the most-recent tracked Competition it appears in and
  **skip `/leagues?team`** for it — honoring "for the leagues we have, use the
  cache." `/leagues?team` is called only for the ~5,579 out-of-scope teams. League
  *metadata* (country, type, continent, canonical name) always comes free from the
  already-cached `leagues/_all.json` catalogue plus our `Competition` name
  overrides. `/teams?id` is still called for every team (club country/founded live
  nowhere else). Net one-time cost ≈ 12k calls, trivial against the 150k/day cap and
  free on every re-run.

- **Enrichment is a collection stage; the table is built in parse.** Live calls
  belong to the collectors (they write `raw/teams/`, `raw/leagues/`); `parse.build()`
  stays strictly cache-only (`max_live_requests = 0`) and materializes Team Profile
  from that cache, exactly as Competition metadata (ADR 0015) is collected live and
  parsed cache-only. The new stage is wired into `orchestrate._collect` (so `cups`
  inherits it), gated by a `--no-teams` flag beside `--no-events`/`--no-stats`.

- **Orchestrate enriches incrementally; the standalone script full-sweeps.** A
  per-league run enriches only the teams surfaced by *that* run's career histories
  ("as new players arrive, review their teams, fetch the ones we don't have"),
  staying lean and cache-free on repeats. `python -m football.teams` scans every
  career team in the store and backfills any missing — the initial 12k-call backfill
  and periodic catch-up — and can rebuild afterwards.

- **The table is always complete; rows fill progressively.** `parse.build()` emits
  a row for every distinct career team on every build, filling fields from cache
  where present and leaving **null** where enrichment hasn't run or the provider has
  no record — `name` always falls back to the denormalized `PlayerTeam.team_name`.
  This mirrors how `Player` falls back to a minimal row when a bio is missing. A
  quota stop mid-enrichment leaves a directory with some null bios/leagues, not a
  broken build; the next enrichment pass fills them.

- **`PlayerTeam.team_id` stays non-foreign-key.** Team Profile now covers every
  career team, so an FK is *technically* possible, but enrichment lags collection
  (a brand-new team has a Career Stint before it has a Profile), and an FK would make
  the build fail on that lag. `PlayerTeam.team_id` remains a bare id (ADR 0006);
  Team Profile is the documented **join target**, not a referential constraint.

## Considered Options

- **A full team↔league bridge table (one row per team-league-seasons).** Rejected as
  the primary model: time-accurate but heavier, and "which league did they come
  from" *still* needs a representative pick on top of it. Because we keep the raw
  `/leagues?team` responses, the bridge remains buildable later without re-fetching
  if a use case demands the historical division.

- **Extend the existing `Team` table with the new columns and widen it to all career
  teams.** Rejected: `Team` means "a side that contests a collected Fixture" and is
  the FK target for Squad Entries, Events, and team match stats. Widening it to
  7,000 mostly-fixtureless teams would break that meaning and force every FK to
  tolerate teams that never played a collected match. A separate directory keeps
  `Team` honest.

- **League-only rows (skip `/teams?id`, infer country from the league).** Rejected:
  halves the API cost but loses real club identity — founded year, ISO code,
  authoritative club country, crest — which is exactly the "information of that club"
  the table is meant to provide. At Ultra's cap the saving isn't worth the loss.

- **Call `/leagues?team` uniformly for every team.** Rejected: always-current, but
  spends ~1,466 calls re-deriving what our own Fixtures already tell us, against the
  explicit "use the cache for leagues we have." The snapshot caveat for tracked teams
  is acceptable — a team in our league data reliably belongs to that league.

- **Only store enriched teams (omit un-enriched career teams).** Rejected: leaves the
  directory incomplete until a 12k-call backfill finishes and lets a Career Stint
  reference a team with no Profile at all. Always-emit-with-nulls makes the table
  trustworthy as "all teams" from the first build.
