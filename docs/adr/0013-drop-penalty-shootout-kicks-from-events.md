# Drop penalty-shootout kicks from the Event timeline

The `Event` table (ADR 0007) is the match timeline — one row per goal, card,
substitution, or VAR decision. But the provider's `fixtures/events` feed also
emits a **penalty-shootout** kick as a `Goal` event, so those kicks were landing
in the table and inflating event-based goal counts for shootout fixtures. Fixture
158990 (a real **0-0** that went to a shootout) shows **14** minute-0 "goals";
across the dataset ~1,197 shootout kicks polluted 282 fixtures. A shootout is a
tie-break, not the match, and ADR 0012 already made its outcome a Fixture-level
fact (`penalty_home`/`penalty_away`), so a kick has no business in the play-by-play
timeline.

A scan of the raw feed showed shootout kicks are marked **three** ways across feed
eras, with no single field common to all: `comments == 'Penalty Shootout'` (modern,
1,060 events, all at minute 120+), `elapsed == -1` (older feeds, 63), and
`elapsed == 0` (14). A further 60 uncommented kicks sit at exactly minute 120 in 8
old fixtures — indistinguishable by any field from a genuine extra-time goal.

**Decisions:**

- **A shootout kick is not an Event; drop it at parse time.** Consistent with
  CONTEXT.md (an Event is a moment of *play*, regulation or extra time) and ADR 0012
  (the shootout is a Fixture-level fact). No schema change, no new column — the
  timeline simply becomes the match. Preferred over tagging kicks with a
  `shootout` flag: the kick-level data is too dirty to support analysis anyway
  (misses recorded as `Goal`/`Missed Penalty`, takers sometimes off-scope, three
  encodings), so preserving it would only invite incorrect queries.

- **Identify a kick with a zero-risk, per-event predicate:** `type == 'Goal'` **and**
  (`comments == 'Penalty Shootout'` **or** `elapsed <= 0`). Both signals are
  verified clean — `comments == 'Penalty Shootout'` never appears in a non-shootout
  fixture, and **no** real goal is ever recorded at `elapsed <= 0`. Scoping to
  `Goal` is essential: the provider files ~1,800 legitimate **Cards** at negative
  minutes (`elapsed == -5` etc.), which must survive. This removes 1,137 of ~1,197
  kicks, shrinking contamination from 282 fixtures to **8**.

- **Accept the 8-fixture residue rather than reconcile it.** The remaining 60
  uncommented kicks at minute 120 cannot be told apart from a real 120' extra-time
  goal (17 such genuine goals exist in non-shootout fixtures), so catching them
  would mean a fixture-level reconciliation against ADR 0012's on-pitch score —
  coupling event parsing to score logic, adding per-team counting to the streaming
  build, and introducing attribution ambiguity. Not worth it for 8 old fixtures;
  the residue is documented here, not silently swallowed.

- **Dropped kicks leave a gap in `event_index`; survivors keep their raw-array
  position.** ADR 0007 keys `Event` on `(fixture_id, event_index)` = position in the
  raw response array. A skipped kick is simply not inserted (the `enumerate` index
  is unchanged), so every real event keeps the exact key it had before, a re-parse
  is a no-op for existing rows, and the "position in the raw array" invariant holds
  literally. Renumbering survivors was rejected: it would shift real events' keys
  and break that invariant for a cosmetic contiguity nothing relies on.

## Considered Options

- **Tag kicks with a `shootout` boolean instead of dropping.** Rejected: a schema
  change to preserve data too unreliable to trust, and it contradicts the glossary
  (a kick is not a play Event). The shootout result already lives on the Fixture.

- **Filter kicks in queries (e.g. the notebook) rather than at parse.** Rejected:
  every consumer would have to re-derive the three-way marker forever, and the
  `Event` table would stay a misleading source of truth. Fix it once, at the
  boundary where raw becomes modeled.

- **Full reconciliation against the on-pitch score to also catch the minute-120
  residue.** Rejected for the coupling/complexity/ambiguity above; the marginal 8
  fixtures don't justify tying two independent concerns together.

- **Key `Event` so kicks never mattered (e.g. renumber, or a surrogate id).**
  Rejected: ADR 0007 chose the raw-array position deliberately for determinism; a
  pure skip preserves it, and renumbering would sacrifice it.
