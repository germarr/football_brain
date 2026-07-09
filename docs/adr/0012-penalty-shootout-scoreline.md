# Store the on-pitch result, keep the penalty shootout in its own columns

`Fixture.home_goals`/`away_goals` were stored verbatim from the provider's
top-level `goals` field. For a normal match that is the full-time score, but for a
game **decided on penalties** (status `PEN`) the provider's `goals` is unreliable:
it folds the shootout into the scoreline, inconsistently. Fixture 292003 (Liga MX
Apertura 2019 final, América–Monterrey) is the worst case — a real **2-1** (the
second leg), Monterrey winning the shootout **4-2**, was stored as `goals` **4-2**:
neither the football result nor a correctly-oriented shootout (the home/away are
even swapped relative to `score.penalty`). Across the dataset **33 shootout
fixtures** had a stored scoreline corrupted this way (out of 282 fixtures that went
to penalties).

The provider already exposes the breakdown we need, in `score`:
`{halftime, fulltime, extratime, penalty}`. We simply weren't storing it.

**Decisions:**

- **`home_goals`/`away_goals` mean the on-pitch result, never the shootout.**
  `parse._match_score` corrects the score **only for shootout fixtures**: the raw
  `goals` is authoritative for every other match — including extra-time-decided
  `AET` games, where it correctly includes the ET goals — so it is kept untouched
  (verified: 0 non-shootout fixtures change). When a shootout is present, the score
  is rebuilt from `score`: full time as the base, preferring the after-extra-time
  score only when it is a consistent *superset* of full time (both sides `>= FT`).
  The provider is inconsistent about `extratime` — sometimes the cumulative post-ET
  score (`2-1` over a `2-1` FT, fixture 292003), sometimes only the goals scored
  *within* the ET period (`0-0` over a `1-1` FT, the 2012 CL final 355812, which a
  naive "prefer extratime" would wrongly store as `0-0`). The superset test keeps a
  genuine ET winner while ignoring the incremental form.

- **The shootout gets its own two columns, `penalty_home`/`penalty_away`.** Both
  null unless the match was decided on penalties. Kept separate rather than folded
  into the goal columns so "goals scored in the match" and "who won the shootout"
  are each answerable, and so nothing downstream that sums goals silently inherits
  shootout penalties. This matches how the schema already splits related-but-
  distinct quantities (a Squad Entry's `goals` vs `penalty_scored`).

- **Only the two shootout numbers are promoted to columns, not the whole `score`
  breakdown.** `halftime`/`fulltime`/`extratime` are recoverable/rarely needed and
  would be four more denormalized columns for little use; the raw cache retains them
  if a future need arises. The penalty score is the one piece with no other home in
  the model and real analytical value (deciding a `PEN` tie).

- **Rebuild-only, no re-collection.** `score` is already in the Layer-1 cache, so a
  drop-and-rebuild (`football.parse`, and any `football.scope` DB) backfills the
  corrected scoreline and the new columns at zero API cost.

## Impact (what this did and did not touch)

- **Corrected:** the 33 shootout fixtures whose `home_goals`/`away_goals` were wrong,
  and everything derived from the fixture scoreline — per-fixture `result` (W/D/L),
  `scoreline`, goals-for/against, and any of these fixtures that carry a `matchday`
  and therefore reach the standings table (most are matchday-null knockouts already
  filtered out). All 282 shootout fixtures gain `penalty_home`/`penalty_away`.

- **Unaffected (already correct before this change):** `AET` (extra-time-decided,
  no shootout) games keep their raw `goals`, which already included ET goals — 0
  non-shootout fixtures change. Per-player and per-team goals from `SquadEntry` read
  in-match goals, not the fixture field. `TeamMatchStat` has no goals column.

- **Known separate issue, NOT addressed here:** the `Event` timeline (ADR 0007)
  stores penalty **shootout** kicks as ordinary `Goal` events (minute 0, no
  distinguishing marker) for many shootout fixtures, so event-based goal counts are
  inflated for them. That is a Layer-2 Event-parsing concern, independent of the
  `Fixture` scoreline corrected here; the events feed's contamination is also why it
  cannot serve as ground truth for the on-pitch score (`score.fulltime`/`extratime`
  is the authoritative source used instead).

## Considered Options

- **Store the full `score` breakdown (halftime/fulltime/extratime/penalty) as
  columns.** Rejected: eight columns where two carry all the analytical value; the
  rest stay in the raw cache for the rare case that needs them.

- **Leave `goals` as-is and just add penalty columns.** Rejected: it leaves 127
  fixtures with a scoreline that is simply wrong (a 2-1 stored as 4-2), so every
  result/standings view built on `home_goals`/`away_goals` stays corrupted. The
  point is that the goal columns must mean the on-pitch score.

- **Derive the on-pitch score from the `Event` goal timeline instead of `score`.**
  Rejected — and empirically wrong: the events feed records shootout kicks as
  `Goal` events (minute 0, unmarked) for many fixtures, so counting Goal events
  *includes* the shootout (fixture 158990: a real `0-0` shows 14 minute-0 goals).
  `score.fulltime`/`extratime` is the provider's own authoritative regulation
  result and is always present for a finished match.
