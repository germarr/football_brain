# ESPN Commentary into a non-disposable `data/commentary.db`

*Relates to ADR 0002 (raw cache then SQLite — whose disposability invariant this
one deliberately breaks), ADR 0011 (per-competition databases), ADR 0014
(coverage-light leagues), and ADR 0018 (the nightly Refresh's Final rule).*

Everything else in this repo comes from API-Football. This is the first store fed
by a **second provider**, ESPN, and the first whose content is partly produced by a
**language model** rather than parsed. Both facts bend rules the other ADRs set.

ESPN publishes per-match narration that API-Football has no counterpart for: fouls,
corners, offsides and attempts are *not* Events, and `fixtures/events` will never
carry them. That is the whole reason to collect it. The exploratory work in
`commentary/` established the join (`(text, clock)` with a `(text, displayValue)`
fallback for ESPN's stoppage-time clock clamp) and a 22-category taxonomy scoring
147/147 against ESPN's own labels on a blind audit. This ADR is the step from that
spike to a store.

Three measured facts drove the decisions, all checked against 49 cached matches
rather than assumed:

- **ESPN's universe is much wider than ours.** 18 of 20 sampled matches are from
  competitions `football.db` does not track at all (Allsvenskan, NWSL, LigaPro
  Ecuador, Argentine Nacional B). A **Competition** is by definition one we collect;
  most narrated matches are not in one.
- **The two feed shapes are ~50/50, not 3-in-10** as `commentary/README.md`
  claimed: 24 narrative, 25 `events_only`. `events_only` is not exotic and cannot
  be dismissed as an edge case.
- **`events_only` needs neither the join nor the model.** Every row carries an
  embedded typed `play` object; all **362/362** rows map through the existing
  `espn_category()` with zero unmapped. The README's "a real piece of work, not a
  patch" judged the *join* — which that shape does not need.

**Decisions:**

- **The unit is a Narrated Match, keyed on ESPN's game id — not a Fixture.** A
  Fixture is an API-Football record of a match in a Competition we collect. Most
  Narrated Matches have no Fixture and never will, so keying on `fixture_id` would
  make the store unable to hold the majority of its own subject matter.

- **`fixture_id` is an optional, verified bridge.** Absent by default (the normal
  case). When an operator supplies one, it is looked up in `data/football.db` — a
  strict superset holding all 122,100 fixture ids, including every id in
  `liga-mx.db` and `world-cup.db`, so one lookup suffices despite ADR 0011 — and
  the Fixture's kickoff and team names must agree with ESPN's. The two providers'
  kickoffs agree **exactly**, in UTC (verified across the cache), so kickoff is a
  strong key and names are confirmation. A mismatch, or an id absent from
  `football.db`, **refuses** — that is a typo, not an untracked league. This follows
  `join.py`'s existing principle: a wrong label is worse than a crash, because
  nothing downstream could detect it.

  **`--force-link` waives the team-name check only.** The providers disagree on
  names constantly — ESPN's "United States" is API-Football's "USA" — and the
  strict rule refused a provably-correct link whose kickoff, home/away
  orientation *and* 1–4 scoreline all agreed. The kickoff and existence checks are
  never waived: they are what make the link checkable at all. Two alternatives
  were considered and rejected: accepting any link where the kickoff and **one**
  team name agree (sound in principle — a team cannot play two matches at one
  instant — but it silently widens the rule for every future link), and a
  committed ESPN→API-Football name alias file (durable and reviewable, but a file
  to maintain and a wall to hit before each new team links). The escape hatch is
  per-run and deliberate. Its cost is real and accepted: nothing accumulates, so
  the next USA match needs the flag again, and a forced link is a human assertion
  that nothing downstream can check. Ingest therefore reports it as
  `FORCE-LINKED on kickoff alone`, never as `verified`, and the refusal message
  prints both scorelines and which team names agree so the operator decides on
  evidence rather than on faith.

- **Final is decided by ESPN, never by `football.db`.** Ingest requires ESPN to
  report the match complete, mirroring the Refresh rule that per-fixture data is
  collected exactly once Final and never before (ADR 0018) — freezing a match at 60'
  is the stale-empty trap in a partial, and worse, plausible-looking flavour. But
  the gate reads **ESPN's** status: `football.db` is eventually consistent and was
  observed saying `2H` for a match ESPN had already called Full Time (it read `FT`
  by the next day). ESPN is authoritative about its own feed's completeness.
  `football.db` is consulted for identity only, never for state.

  The gate tests ESPN's **computed `status.type.completed` boolean**, not its
  `description`. `build_match()` currently extracts only `description` — a display
  string — and gating on `"Full Time"` would refuse every extra-time and shootout
  match, whose descriptions we have never observed: all 50 cached matches are
  `STATUS_FULL_TIME`, so the cache cannot enumerate the vocabulary. `completed`
  covers AET and penalties without guessing at names. `narrated_match` stores
  `status_name` (`STATUS_*`) verbatim, so a terminal-but-irregular match (abandoned,
  awarded) is ingested yet remains visible and filterable in SQL rather than
  silently blended into the clean Full Time population.

- **Both feed shapes are ingested.** `narrative` runs join + classifier.
  `events_only` maps its embedded `play.type.text` through the same
  `espn_category()`: no join, no model, no new taxonomy, zero requests. Every
  `events_only` Category is therefore **asserted**, never inferred. It also makes
  `penalty_missed` reachable for the first time (the README lists it as
  unreachable — all three matches carrying it use this shape).

- **`commentary.db` is the system of record, and is NOT disposable.** This
  knowingly breaks ADR 0002's invariant. That invariant holds for the API-Football
  path because the transform is free and deterministic; here it is neither —
  classification costs money and re-running it will not reproduce the same labels.
  A second raw layer caching model output (`data/raw/espn-classify/`) would have
  preserved disposability and was **considered and rejected** in favour of one
  fewer moving part and one place labels live.

- **Ignored anyway, with no backup — an accepted risk.** `data/commentary.db` sits
  with the other stores under the blanket `data/` ignore. Its inferred Categories
  then exist as a single copy on one machine. The `.gitignore` comment is amended to
  say so rather than let its "regenerable via collect.py / parse.py" rationale imply
  something false about this one file.

- **Nothing is ever written outside `data/commentary.db`.** The existing spike has
  no database access at all — no `sqlite3`, no `sqlmodel`, nothing imported from
  `football/` — and the store must not change that. `data/commentary.db` is a new,
  standalone database; `football.db` and the per-competition stores of ADR 0011 are
  **never written**, and are opened **read-only** (SQLite `mode=ro` URI, so a write
  is impossible rather than merely absent) and **only** when `--fixture-id` is
  supplied. With no `--fixture-id`, `football.db` is not opened at all. The only
  other on-disk effect stays the raw cache under `data/raw/espn-summary/`.

- **The grain is exactly the classified line — seven fields, nothing more.**
  `narrated_match` (keyed on game id; league, date, venue, home/away team+score,
  `status_name`, `narration_coverage`, `model`, nullable `fixture_id`, `ingested_at`)
  → `commentary_line` (`game_id`, `sequence`, `minute`, `clock_seconds`, `team`,
  `category`, `source`, `text`) — the shape `synthesis-760514.json` already emits,
  verified key-for-key across its 115 rows — **plus `field_position`**.
  `team` is a **name**, not an id — ESPN's ids are its own and bridge to nothing.
  `model` is recorded because the labels are irreproducible: the store must say
  which model asserted them.
  **`field_position` is stored as opaque JSON text**, not as numeric columns:
  `{"x":88.5,"y":50.0,"goal_y":45.8}` or null. This is deliberate. The coordinates
  are worth keeping — they cost nothing and are the one part of the feed that would
  be tedious to reconstruct — but they exist only on scoring occurrences and cover
  just 56% of even those (43 of 77), non-randomly: a named method ("Goal - Header")
  always carries them, a bare "Goal" only 27 of 57 times. As numeric columns they
  would invite `SELECT field_x, field_y` and a goal map silently missing two goals
  in five. As a JSON string they are preserved but inert: nobody plots them without
  first parsing them, and anybody parsing them is deliberate enough to check the
  gap. `events_only` matches carry none at all (0 of 362 rows).
  Deliberately excluded: any **player** table. Ids are complete in narrative matches
  (673/673) but absent in `events_only` (0/590), so the column would be half-null by
  construction, and an ESPN athlete id bridges to nothing in `football.db` anyway.
  Players remain named inside the line's text, and the ids re-derive from the raw
  cache for free. Nor is the full `keyEvents` feed mirrored — it would be a second
  copy of raw JSON that dies with the raw JSON anyway.

- **Ingest is idempotent: skip if present, `--reclassify` to force.** With no
  classify cache, the DB *is* the cache. An already-ingested Narrated Match costs
  zero requests and its labels cannot be clobbered by accident, which is what makes
  "refused because live — re-run it later" practical: a sweep can be re-run freely
  and only genuinely new matches cost anything. Overwriting is explicit and replaces
  that match's lines wholesale.

**Consequences:**

- Commentary reaches leagues the rest of the store cannot see. For the ~20 in 25
  `events_only` matches outside our Competitions, these Commentary Lines are the
  only record we hold — they are not "the Event timeline restated", because there is
  no Event timeline for them to restate. That framing only applies to the minority
  (5 of 25, all Eliteserien) that are also Fixtures.

- **Narration Coverage must be consulted before any aggregate.** An `events_only`
  match reports zero fouls because fouls are never narrated there, not because none
  were committed. Any per-match rate over a narrative Category that mixes the two
  shapes is silently wrong. This is now a live hazard rather than a hypothetical:
  half the store is `events_only`.

- **No Player is queryable, by design.** "Who scored" cannot be answered in SQL —
  players are named only inside `text`. This is the deliberate price of the grain:
  ESPN's athlete ids bridge to nothing in `football.db` anyway (there is no id
  mapping), so a stored id would have been an ESPN-only identifier joinable to
  nothing. Where it eventually matters, the ids re-derive from the raw cache for
  free — but the name bridge to a Player still would not exist.

- **Coordinates are preserved but inert.** `field_position` is queryable only after
  a deliberate `json_extract` or client-side parse, which is the safeguard: the 44%
  gap cannot be sprung by an incurious `SELECT`. Anyone who unpacks the JSON has
  chosen to, and should read this ADR's grain decision before mapping it. If a real
  goal-map need ever appears, promoting the JSON to numeric columns is a migration
  over data already stored — no refetch, no reclassify.

- **Losing `data/` loses the labels for good.** Accepted deliberately. If that ever
  stings, the escape hatch is the rejected option above — start caching classifier
  output, and the store becomes disposable again with no schema change.

- **Accuracy is measured only where ESPN provides labels.** The audit covers the
  typed subset. The ~88 untyped lines per narrative match (`foul`, `corner`,
  `offside`, `attempt_*`) have no ground truth; their support is the templated feed
  and internal consistency (22 fouls ↔ 22 free-kicks-won), not measurement.

- Not wired to cron, the Refresh, or the Console. An operator runs it against an
  ESPN URL. Auto-discovery of a date's slate via ESPN's scoreboard endpoint is the
  natural next step and is not built here.

- Extra time and shootouts remain untested — none appeared in 49 scanned matches.
  `keyEvents` carries a `shootout` flag we pass through but have never seen set.
