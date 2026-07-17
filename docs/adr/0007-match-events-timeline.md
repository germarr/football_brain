# Collect the match-event timeline into a normalized Event table

`fixtures/players` gives per-player **aggregate** match totals (goals, assists,
yellow/red, penalties) but no timeline — not the minute a goal or card happened,
the goal type (normal / penalty / own goal), the assist behind a goal, or the
substitution minute. That data lives only in the **`fixtures/events`** endpoint
(one call per fixture). We add it to the existing cache-first pipeline and model
it as a new normalized `Event` table — one row per occurrence — rather than
denormalizing minute-lists onto `SquadEntry`, consistent with ADR-0006's
one-row-per-fact stance.

**Decisions:**

- **New `Event` table, one row per event.** Columns: `fixture_id`, `team_id`,
  `minute` (`time.elapsed`), `extra` (`time.extra`, the Added Time in 90+3'),
  `type` (Goal | Card | subst | Var), `detail` (Normal Goal | Penalty | Own Goal |
  Yellow Card | Red Card | …), `player_id`, `assist_id`, `comments`. Timeline
  questions ("goals after 80'", "own goals", "stoppage-time cards") become plain
  SQL, not JSON extraction.
- **Store all four event types**, not just Goal + Card. The single call returns
  them regardless, so filtering is a `WHERE` clause and free at collect time;
  `subst` events also recover the exact substitution minute that `SquadEntry`
  lacks. Dropping a type would only force a re-parse later.
- **Primary key `(fixture_id, event_index)`.** The endpoint returns no event id,
  and `(fixture, player)` isn't unique (a player can score twice). `event_index`
  is the event's position in the fixture's response array — deterministic, stable
  across drop-and-rebuild while the raw cache is stable, and it preserves
  chronological order.
- **`team_id` is a FK to `Team`; `player_id`/`assist_id` are nullable FKs to
  `Player` with a parse-time guard** that nulls any id not in the known Player set.
  Events come only from collected fixtures, so the team is *usually* in scope, but the
  provider occasionally names an off-scope actor (a **coach** card) or a null
  player (some `Var`); the guard keeps those from failing the build, mirroring the
  existing "unknown player id 0/None → skip" handling. (See amendment below: the
  team-in-scope assumption does not always hold, and `_build_events` now guards
  `team_id` the same way.)
- **Added Time is per-event only.** We store `extra` on each event; we do **not**
  store a per-half announced-added-time total — the provider does not expose it and
  inferring it from events undercounts (see CONTEXT.md → Added Time).
- **A separate entrypoint, `football.collect_events`**, not folded into
  `collect()` (unlike `COLLECT_CAREERS`, which rides along). The backfill is
  ~20,780 calls (one per fixture) — a full day under the 75k/day Ultra cap — so it
  is triggered deliberately on its own. It is cache-first and resumable (a stop or
  the daily cap loses nothing), and `parse` reads whatever events are cached,
  skipping fixtures not yet fetched (the same cache-miss-tolerant pattern used for
  bios and careers).

## Considered Options

- **Denormalize minute-lists onto `SquadEntry`** (e.g. a JSON `goal_minutes`).
  Rejected: opaque to SQL, and it can't cleanly represent own goals or the assist
  credit that belongs to a different player. Same reason ADR-0006 rejected JSON
  season lists.
- **Only Goal + Card.** Rejected: no collection saving (same one call), and it
  discards substitution minutes and VAR events the payload already contains.
- **Surrogate autoincrement PK.** Rejected: non-deterministic across rebuilds and
  carries no meaning, unlike the natural composite keys used elsewhere.
- **Denormalize `player_name`/`assist_name` with no FK** (as `PlayerTeam` does for
  off-scope clubs). Rejected: event players ARE in scope (they play in our
  fixtures), so a guarded FK keeps referential integrity without duplicating names.

## Amendment (2026-07-17): guard `team_id` too

The original decision assumed an event's `team_id` was always in scope because
events come only from collected fixtures. That does not always hold: the provider
occasionally names a team on **neither side** of the fixture (observed: team 864 in
an event whose fixture is between two other teams), which is absent from the `Team`
table entirely. SQLite tolerated the dangling FK; the Postgres Published Store
(ADR 0027) rejected it, aborting the whole COPY+swap on the `event` load.

`_build_events` now takes `known_teams` and **drops** any event whose `team_id`
isn't a known Team — a required FK can't be nulled — mirroring the guard
`_build_team_stats` already applied and the existing player-id guard above. The
model docstring (`Event`) states this as enforced rather than assumed.
