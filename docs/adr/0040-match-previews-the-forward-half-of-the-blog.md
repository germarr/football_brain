# Match Previews: derived cards in an authored store

---
Status: accepted — extends ADR 0029 (Editorial Store), ADR 0025 (standings + leaders),
ADR 0034/0037 (the Desk, the Competitions board). Amends the **Editorial Store**
definition in CONTEXT.md and adds **Match Preview**, **Team Leaders**, **Winner Market**,
**Quote** and **Market Probability**. Depends on ADR 0041 (Kalshi team registry) and on
the ADR 0028 amendment that publishes scheduled Fixtures.
---

The blog covers only matches that have been played. This adds the other direction: for
every Fixture kicking off in the next seven days whose Competition has a **Publication**,
a card carrying each Team's table position and points, its leading scorer and assister,
and what a prediction market thinks of the result. The blog reads it from PocketBase.

Six decisions were not obvious.

**Decisions:**

- **A Match Preview is derived, and it lives in the authored store anyway.** ADR 0029
  made PocketBase the **Editorial Store** on the strength of one property: it is the only
  store here that is authored rather than derived, the only one with no rebuild path, and
  therefore the only one that genuinely needs backing up. A Match Preview is the opposite
  of all three — every field falls out of a rebuild and losing the collection costs one
  run. It goes there regardless, because the consumer is the blog and the blog talks to
  PocketBase; standing up a second delivery store to preserve a definition would be
  tail-wagging. So the definition moves instead: the split is **per-collection, not
  per-store**. "Back this up" stays true of the instance; "cannot be regenerated" narrows
  to `match_post`. We rejected putting Previews in the **Published Store** — it is a
  Postgres replica for open-ended SQL consumers, not a read path for a web front end, and
  the blog would have needed a second client and credentials to reach it.

- **The collection is `match_preview`, and there is exactly one of them.** The name follows
  `match_post`'s reasoning unchanged (ADR 0029): the instance is co-tenanted with a personal
  site, `preview` is a generic enough noun that the co-tenant could plausibly want it, and
  the rename costs nothing now and everything later. The *singular* is the sharper decision.
  The obvious design — a `preview` collection for upcoming games and an `old_preview` one
  for played ones — was rejected because moving a record between collections is the only
  genuinely new failure mode this feature could have introduced: a mover job that half-fails
  leaves a Fixture in both collections or in neither, and nothing detects it. A lifecycle
  field cannot half-fail. The blog gets the same two lists from a filter.

- **A settled Match Preview is frozen, not archived — because its market half stops being
  derivable.** The football half stays computable forever; it is only history. But a
  **Quote** is a point-in-time read, and nothing reconstructs what the market thought an
  hour before kickoff (Kalshi's candlesticks return empty for these series). So at kickoff
  the record freezes around the last Quote read before it and is never rewritten. This is
  what makes a Match Preview the one record here that *starts* derived and stops being so,
  and it is the reason the archive is a state rather than a place: there is nothing to move,
  only something to stop touching.

  > **Corrected by ADR 0043.** The parenthetical is false. Kalshi's candlesticks return
  > hourly bid/ask OHLC for these series, on open and settled markets alike, and Polymarket
  > serves its own history after close — so what the market thought an hour before kickoff
  > *is* reconstructible (a **Market Track** reconstructs it). **The freeze stands; only
  > this reason is withdrawn.** Its replacement: a Quote does not stop at kickoff, it
  > converges on the result and settles at 1 and 0, so re-running a settled Fixture would
  > overwrite a *forecast* with an *outcome* — both spelled as three percentages, with
  > nothing on the card to say which was stored.

- **The two halves rebuild on two cadences and carry two timestamps.** The football half
  depends on a quota-bound **Refresh** and only changes when a Fixture goes Final, so it
  recomputes nightly at 04:00 behind the existing sequence. The market half is three
  unauthenticated GETs — one per series, ~90 markets each, unpaginated — so it re-reads
  hourly. A single `updated` field would misdate whichever half it did not describe, and
  the two staleness stories are genuinely different: a table that predates today's results
  is bounded and explainable, while a day-old price presented as current is simply wrong.

- **Standings and leaders are computed by a module shared with `serving.publish`, against
  the Published Store.** ADR 0025's rules are subtle — Final fixtures only, 3/1/0, on-pitch
  goals with the shootout excluded (ADR 0012), sort Pts → GD → GF, an explicitly unofficial
  ordering. A second implementation would let the Viewer's table and the blog's card
  disagree about the same team's position with nothing erroring and both pages rendering:
  the silent-failure class ADR 0033 exists to hunt. `_standings_rows` already takes tuples
  and returns dicts, so it lifts out of `serving/publish.py` unchanged and gains a
  `leaders_by_team` sibling; `serving.publish` keeps calling it with SQLite rows and the
  preview builder calls it with `psycopg` rows. We rejected reading `serve.db` from the
  blog pipeline (couples two surfaces, and the preview would be reading a file the Viewer's
  publish replaces underneath it) and rejected publishing the standings tables into Postgres
  (a third copy of the schema, and the preview's freshness would inherit the Viewer's).

- **Team Leaders carry two scopes and no threshold.** "Top scorer in the current tournament"
  read literally returns nothing useful for two of the three Competitions in scope: three
  matchdays into Liga MX Apertura 2026 (27 of 153 played) Club América's leading scorer is a
  four-way tie on one goal, and twelve games into Leagues Cup 2026 (12 of 54) it has none at
  all. Quietly widening the scope to the club's domestic league would fix the number and
  reintroduce, in miniature, the failure **Narration Coverage** was written about — a figure
  whose meaning depends on a scope the reader cannot see. So a Match Preview carries the
  domestic-league leader always and the Fixture's own Tournament leader when it differs,
  each stamped with competition, season, tournament and games played. The label is part of
  the fact, not a rendering choice. There is deliberately no "the tournament is too young"
  cutoff: any threshold is wrong somewhere, and carrying both has none to get wrong.

**Consequences:**

- **CONTEXT.md's Editorial Store entry no longer says the store is wholly authored.** That
  sentence was load-bearing for the backup argument, so it is restated rather than deleted:
  the store needs backing up, and only its `match_post` half is irreplaceable.

- **The preview builder knowingly encodes a wrong model, and this must not be read as
  design.** Leagues Cup (772) is registered as `type: "league"` though it is a cup, so its
  `phase` column is null for all 289 fixtures and its cup rounds — `Group Stage`,
  `Quarter-finals`, `Final` — sit in the **`tournament`** column, which CONTEXT.md
  explicitly forbids ("a group or a knockout round [is not] its own Tournament"). Its 2026
  Group Stage also carries no matchday where 2025's did, so `_build_league_tables`' `matchday
  is not null` filter drops it and **Leagues Cup has no standings in `serve.db` today** —
  invisible because ADR 0025 left cup chips inert. The preview builder therefore selects a
  cup's table by the tournament string `'Group Stage'`. Fixing the registry (772 → `cup`,
  `calendar_year` → true, re-parse into real Phases) is correct and deferred: it re-parses a
  live Competition mid-tournament, and sequencing this feature behind it was rejected on
  timing, not on merit. Every site of the workaround is commented back to this paragraph.

- **A knockout Fixture has no table position, and always will not.** That is not a gap to
  be filled later by fixing the registry — there is no round-robin behind a quarter-final.
  The card degrades, in the same spirit as ADR 0014's Coverage-light Competitions: the
  Publication still gets a preview, with fewer facts on it.

- **The preview writes only for Publications with `published = true`**, deliberately unlike
  `candidates.py`, which passes `only_published=False`. The Desk's board is an internal work
  list where the gate is irrelevant; this feed is read by the public site, where the gate is
  the entire point. All four Publications are currently published, so the divergence is
  inert today and is recorded here because it will not always be.

- **Cancelled Fixtures are stored under two spellings** — `CANC` (78) and `Canc` (5). The
  **Final** check is a whitelist and is unaffected, but any exclusion logic that blacklists
  cancelled matches misses five of them. The preview builder selects by whitelist.
