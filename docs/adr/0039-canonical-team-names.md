# Team names are compared canonically, and a refusal's remedy depends on its caller

---
Status: accepted — **amends ADR 0030** (the team-name anchor) and completes **ADR 0038**
(the delayed-match path, whose remedy-at-the-raise-site rule this extends). CONTEXT.md's
**Narrated Match** entry gains the canonical-name rule.
---

Charlotte v Pumas, Leagues Cup, 2026-08-05, was refused:

    ESPN  401863553: '2026-08-05T01:22Z'    (Charlotte FC v Pumas UNAM)
    fixture 1530107: '2026-08-05 00:00:00'  (Charlotte v U.N.A.M. - Pumas, Leagues Cup)

Both providers say Leagues Cup, both say **3–0**, both say full time. The 82-minute
drift is comfortably inside ADR 0038's `DELAY_TOLERANCE`, so the delayed-match path
should have carried it. It did not, because that path requires both team names to agree
and `_norm_team` compared them after nothing but casefolding and whitespace collapse.
`"Charlotte FC"` is not `"Charlotte"`; `"Pumas UNAM"` is not `"U.N.A.M. - Pumas"`.

So ADR 0038's rule was correct and simply unreachable: **neither** name agreed, which
is the one case its anchor cannot survive. And `--force-link` could not help, because
the kickoff check is raised first and the flag waives names only.

The differences are not arbitrary. They are three recurring, mechanical kinds — a
club-type suffix, punctuation inside an acronym, and word order — plus accents. None of
them is evidence about *which match this is*.

**Decisions:**

- **Team names are compared canonically, not literally.** `_norm_team` folds accents,
  deletes dots bounded by single letters (so `U.N.A.M.` becomes `unam` rather than four
  one-letter tokens), turns remaining punctuation into space, drops the club tokens
  below, and compares what is left as an order-insensitive set. It returns a sorted
  string, so a refusal can still print the canonical form that agreed.

- **The club-token list is exactly `fc`, `cf`, `sc`, and that is an empirical result.**
  Every longer list was tested against all **4,413 teams** in `football.db`, counting
  *collisions* — distinct team ids normalising identically. A generous list
  (adding `club`, `de`, `usa`, `afc`, `ac`, `cd`, …) produced 56 collision groups and
  merged clubs that are genuinely different: Liverpool with **AFC Liverpool**, Blackpool
  with **AFC Blackpool**, Corinthians with **Corinthians USA**, Lyon with **Club De
  Lyon**. Those are separate clubs with separate ids that play separate matches, and
  merging them is exactly the silent mislink ADR 0026 exists to prevent.
  `fc`/`cf`/`sc` carry no such freight: no pair of distinct clubs in the store is told
  apart by them alone. They leave 38 collision groups, and on inspection those are
  provider *duplicates of the same club* — `Bournemouth`/`Bournemouth FC`,
  `Nashville`/`Nashville SC`, `Penarol`/`Peñarol` — with a short tail of arguable pairs
  (`Lyon`/`FC Lyon`, `CF Montreal`/`FC Montréal`, `Miami`/`Miami FC`).
  Digits are deliberately kept, which is what holds `Toronto FC II` apart from
  `Toronto FC`.

- **This is spelling reconciliation, not fuzzy matching.** No edit distance, no scoring,
  no threshold, no "closest match". Every transformation is a named, deterministic rule
  a reader can check by eye, and two names either canonicalise to the same string or
  they do not. That matters because the delayed path is *automatic* (ADR 0038 chose no
  flag), so a name comparison that could be *nearly* right would put a probabilistic
  step underneath an unverifiable link.
  The honest cost is that an **alternate name** is still refused, only a *respelling* is
  reconciled: `Atlético de San Luis` against `Atletico San Luis` still disagrees,
  because dropping the connective `de` is precisely what merged Lyon with Club De Lyon.
  Such a case remains `--force-link`'s job, where a human asserts it.

- **A residual collision cannot be reached by accident.** For one to cause a mislink an
  operator must typo a fixture id, the wrong Fixture must fall inside the same window,
  **and both sides** must collide. The surviving collisions are same-club duplicates, so
  no such pair can play each other; the anchor ADR 0038 relies on is therefore intact.

- **A refusal names the remedy its *caller* can actually perform.** ADR 0038 moved
  remedies to the raise sites because only they know which check failed. They do not
  know whether the caller can act on the advice, and every refusal ended with "omit
  `--fixture-id`" — impossible from `football_blog.pipeline`, which marks that argument
  `required=True` and drafts from the Fixture. An operator hitting this refusal was told
  to do the one thing the tool in their hands forbids, which is the same dead end 0038
  set out to remove, one level up.
  So the caller declares the constraint (`--link-required`, passed by the pipeline's
  stage 2) and the refusal names the escape hatch that caller has: **omit `--espn-id`**
  to draft the Fixture without the commentary — stage 2 is already skipped when it is
  absent — or run `commentary.ingest` alone to ingest the commentary unlinked. The flag
  waives no check and changes only wording.

**Consequences:**

- **A whole recurring class of true links stops being refused**, with no per-club upkeep.
  The alternative considered was a committed alias registry on the `venues.json`
  pattern: exact and auditable, but it reintroduces the manual, easy-to-forget step that
  ADR 0037's Competitions board was built to remove, and it fails closed on every club
  never seen before.

- **The delayed path is now reachable in the case it was written for**, which is how this
  ADR was found: 0038's Monterrey case happened to have identically-spelled names, so
  the gap did not show until a match where neither side matched.

- **`--force-link` is needed less often**, and for a better reason. It now marks a
  genuine judgment — an alternate name, not a suffix — rather than punctuation.

- **What fails silently here (ADR 0033):** a normalisation that merges two real clubs
  produces a link nothing downstream can detect. The guard cannot be downstream, so it
  is the token list itself, pinned by `test_the_token_list_stays_short` along with the
  named pairs that must stay apart. Anyone widening it must re-run the collision count
  over `team` and justify what it merges. The tests assert both directions: what must
  now agree, and every conflation rejected here.

- **Two test files changed for the right reason.** Six tests used `Atlante`/`Atlante FC`
  and `Orlando City`/`Orlando City SC` as their examples of a *disagreement* — pairs this
  ADR reconciles — so they were quietly testing the both-names path instead of the
  one-name path they document. They now use genuinely distinct clubs (`Atlas`,
  `Nashville SC`) and assert what they always meant to.
