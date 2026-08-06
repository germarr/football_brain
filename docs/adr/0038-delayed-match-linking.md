# A delayed match links on both team names, not on a wider clock

---
Status: accepted — **amends ADR 0030** (kickoff tolerance and team-name anchor), which
amended ADR 0026 (the ESPN Commentary Store). CONTEXT.md's **Narrated Match** entry gains
the delayed-match case.
---

Monterrey v Orlando City, Leagues Cup, 2026-08-05, was refused:

    ESPN  401863559: '2026-08-06T00:55Z'    (Monterrey v Orlando City SC)
    fixture 1530116: '2026-08-05 23:30:00'  (Monterrey v Orlando City SC, Leagues Cup)

Both team names agree exactly. Both providers say Leagues Cup. The competition, the
teams and the scoreline are the same match by every available measure except the clock,
which disagrees by **85 minutes** — nearly six times `KICKOFF_TOLERANCE`.

It is not staleness. The raw cache, force-refreshed ninety seconds before the refusal,
holds `2026-08-05T23:30:00+00:00 / FT / 1-2`. And it is not this competition drifting
systematically the way Liga MX rounds: on ESPN's own slate for that night, every other
Leagues Cup match agrees with API-Football *exactly* — `23:30Z`, `00:30Z`, `00:30Z` —
and only this one differs. **The match was delayed about 85 minutes**, ESPN recorded the
actual kickoff and API-Football kept the scheduled one.

ADR 0030 anticipated being asked to widen the tolerance and set the bar: "widening it
further should require a new observation, not a new convenience." This is that new
observation, and it is a different *kind* of disagreement from the one 0030 measured —
not a provider rounding by minutes, but one provider tracking reality and the other the
schedule.

**Decisions:**

- **The tolerance is not widened. A second, differently-anchored path is added.**
  `KICKOFF_TOLERANCE` stays at 15 minutes, because ADR 0030's safety argument depends on
  it and that argument still holds: it turns on consecutive fixtures being hours apart,
  and the very slate that produced this refusal shows why. Two Leagues Cup matches kicked
  off at 23:30 that night (this one and Inter Miami v Atletico San Luis) and two more at
  00:30. Any tolerance wide enough for 85 minutes reaches its neighbours, which is exactly
  the silent mislink ADR 0026 exists to prevent.
  So `DELAY_TOLERANCE` (6 hours) is a separate constant governing a separate rule, and a
  reader comparing the two sees immediately that they are anchored differently rather
  than that someone lost their nerve about 15 minutes.

- **The delayed path requires *both* team names to agree, and that is what makes the
  window safe.** ADR 0030's anchor for an inexact kickoff is *one* exactly-agreeing name,
  because Liga MX runs two fixtures in one broadcast slot and a typo can land a few
  minutes away. At six hours that reasoning is far too weak — so this path demands the
  strictly stronger anchor. With both names agreeing, a mislink requires the same two
  teams to play *another* match inside the window, which cannot happen.
  What the six hours actually guards is therefore **not** a wrong fixture: it is our own
  data being wrong. A kickoff out by six hours is a delay; one out by a day is a bug we
  want to keep hearing about. That is what sets the number, and it is written here
  because 6h reads as arbitrary otherwise.

- **It is automatic, and loud.** No new flag. A flag would be friction theatre in ADR
  0034's sense — "a step you can learn to perform is not a guard" — and unlike
  `--force-link` there is no human *judgment* being asserted, because the machine can
  already see that both names agree. This path differs from one that already links
  silently (≤15 min drift, both names exact) only in the size of the drift, and it
  carries a stronger anchor than that one requires.
  The cost is that a delayed link now happens with nobody in the loop, so it would paper
  over a kickoff that is wrong for some *other* reason. That is why it prints the drift
  as prominently as `FORCE-LINKED on kickoff alone` does: the log says "linked despite 85
  min drift — delayed match", never a clean "verified".

- **Home/away orientation is deliberately not required, and `league` is reported but
  never required.** Both agreed in this case, and both were rejected as conditions.
  Orientation: ESPN put Monterrey "home" at *Orlando's* ground, because Leagues Cup plays
  at host stadiums — neutral-venue tournaments are precisely where providers disagree
  about which side is home, and precisely where delays happen, so requiring it would
  refuse real links in the case this ADR exists to admit. The existing set comparison
  stands. `league`: ESPN's "Leagues Cup" happens to match ours exactly, but their Liga MX
  is "Mexican Liga BBVA MX", so a competition-name condition would fail on the very
  competition ADR 0030 was written for. It is printed as evidence and gates nothing.

- **A refusal states its own remedy, and the pipeline stops inventing them.**
  `commentary.ingest` returns an exit *code*, so `football_blog.pipeline` could not know
  which of the three checks failed. It appended both guesses to every failure: "if the
  match is still live, re-run after full time" and "if the teams merely disagree by
  spelling, re-run with `--force-link`". Against this refusal the first was misleading —
  `football.db` did read `1H`, because stage 1 refreshes the cache with `--no-rebuild`
  while the Published Store already had `FT` — and the second was **impossible**, since
  `--force-link` waives team names only and is checked *after* the kickoff raise. The
  operator was sent to a flag that cannot apply, which is how this ADR started.
  Remedies move to the `raise` sites in `fixture_link._compare`, the only place that
  knows which check failed and, by ADR 0030's design, the *single* implementation every
  caller goes through. The pipeline prints "see the reason above" and nothing else.

**Consequences:**

- **Two links that would have been refused are now accepted**, and both are stated on
  screen: a delayed match, and a match whose provider disagreement happens to look like
  one. There is no way to tell those apart from the data, which is the honest cost of the
  path — mitigated only by the anchor being both names rather than one.

- **`--force-link` still cannot waive a kickoff, and now says so.** The delayed path is
  not a waiver: it is a different rule with its own evidence. An operator who wants a
  link across more than six hours has no escape hatch, by design and consistent with ADR
  0030's stance that such a case "should be ingested unlinked rather than guessed" —
  which `commentary.ingest` supports today by omitting `--fixture-id`.

- **The three refusal messages become the only place a remedy is written**, so every
  caller — the pipeline, `verify_fixture_pg`, and the Desk — gets the right advice
  without any of them knowing the rules. Previously only the pipeline offered remedies at
  all, and they were guesses.

- **What fails silently here (ADR 0033):** a delayed link is by construction unverifiable
  downstream — ADR 0026 already said nothing can detect a wrong link — so the guard has
  to be the anchor itself. The tests assert the boundary in both directions: both names
  plus 85 minutes links, both names plus seven hours does not, and one name plus 85
  minutes does not however hard it is forced.
