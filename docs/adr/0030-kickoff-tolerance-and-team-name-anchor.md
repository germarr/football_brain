# Verifying a Fixture link on a kickoff tolerance, anchored by a team name

---
Status: accepted — amends ADR 0026 (ESPN Commentary Store) and ADR 0029 (Editorial
Store and blog draft pipeline).
---

ADR 0026 made the **Fixture** link on a **Narrated Match** hand-supplied and *verified
before it is stored*, and stated the rule that made verification cheap: the two providers'
kickoffs "agree exactly, in UTC". That was true of everything checked at the time —
national-team competitions and the leagues whose fixtures we had linked — so
`fixture_link._compare` compared kickoffs with `==`.

It is not true of Liga MX. **ESPN rounds a kickoff to the hour where API-Football keeps the
broadcast minute.** Cruz Azul v Atlante FC on 2026-08-02 is ESPN `2026-08-02T03:00Z` and
API-Football `2026-08-02 03:05:00` — the same match, refused. This is not an edge case:
**139 of the 487 Liga MX fixtures in 2026 kick off off-the-hour** (`:05`, `:06`, `:07`,
`:10`, `:15`, `:45`), so exact equality refuses a large recurring class of *true* links,
and refuses them permanently — a re-run cannot fix a disagreement that is structural.

`--force-link` could not help, and should not have: it waives **team names** only, and is
checked *after* the kickoff raise. That ordering was deliberate (ADR 0026: "a forced link
with a wrong kickoff is just a wrong link") and stays.

**Decisions:**

- **Kickoffs must agree within `KICKOFF_TOLERANCE` (15 minutes), not exactly.** Consecutive
  fixtures in one Competition are hours apart, so 15 minutes cannot reach the neighbouring
  match. It is deliberately far below the ~2h fixture spacing and far above the observed
  provider drift (≤10 min), so widening it further should require a new observation, not a
  new convenience.

- **An *inexact* kickoff must be anchored by at least one exactly-agreeing team name.** An
  exact kickoff stands on its own, as before. A merely-close one is weaker evidence, and
  the risk it opens is specific: Liga MX routinely runs **two fixtures in the same broadcast
  slot** (2026-08-01 had Atletico San Luis v Tijuana and FC Juarez v Pumas both at 03:00),
  so a typo'd id can land on a genuinely different match a few minutes away. Requiring one
  shared team name closes that: the real link keeps its anchor ("Cruz Azul" agrees; only
  "Atlante" vs "Atlante FC" does not), the typo does not.

- **`--force-link` cannot waive the anchor.** Without this the two waivers compound —
  inexact kickoff *plus* waived names is a link where nothing at all was verified, which is
  precisely the silent mislink ADR 0026 exists to prevent. The refusal names the drift and
  the tolerance so the operator can see it is the *anchor* that is missing, not the clock.
  A genuine same-slot-different-spelling case has no automated escape hatch by design; it
  is rare, and it should be ingested unlinked rather than guessed.

**Consequences:**

- One implementation still: `_compare` remains the single "is this the same match", shared
  by `football.db` and the **Published Store**, so neither source can drift from the other
  (ADR 0029).
- A forced link still prints `FORCE-LINKED on kickoff alone`. That wording now covers a
  kickoff that agreed *within tolerance*; the printed Fixture kickoff shows the real value,
  so the operator sees the drift in the output rather than being told a clean "verified".
- No stored data changes, and no previously-accepted link becomes invalid — the rule only
  admits links that were refused before.

**Rejected:**

- **A `--force-kickoff` flag.** It makes the operator retype an assertion for a *systematic*
  provider difference — 139 fixtures a season — which trains them to force by reflex, and a
  reflexive force is worth less than the check it replaces.
- **Normalising both kickoffs to the hour.** Cheap, but it discards the very precision that
  distinguishes two same-slot fixtures, and it would silently accept an hour-boundary
  neighbour.
- **Falling back to a kickoff-window *search* when the supplied id disagrees.** This module
  verifies an operator's assertion; it does not pick a Fixture. A search would make a wrong
  id resolve to *something*, which is the failure mode the refusal exists for.
