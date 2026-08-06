# The Competitions board: onboarding a Publication in one act, and the four things it refuses

---
Status: accepted — extends ADR 0029 (Editorial Store) and ADR 0035/0036 (the surfaces),
applies ADR 0021's second-front-door rule, and **narrows ADR 0027/0028** by making the
wholesale publish's Competition set derived rather than typed. CONTEXT.md gains
**Draftable**.
---

Putting a Competition on the blog took four commands across three stores, in an order
nothing enforced:

    uv run python -m football.onboard.orchestrate <id>       # if not already collected
    uv run python -m football.publish.pg 262 253 1 <id>      # every id, or lose one
    uv run python -m football_blog.onboard --league-id <id>  # interactive prompts
    # then remember to flip published in PocketBase

The second line is the sharp edge. `pg.publish` **replaces** the Published Store — the
football tables end up holding exactly the Competitions named and nothing else — so
forgetting one silently removes it. That is not hypothetical: `DEFAULT_LEAGUE_IDS` is
`[262, 253]` while there are three Publications, so a bare `python -m football.publish.pg`
today would drop the World Cup, taking 1,577 characters of hand-authored prompt overrides'
subject matter out of the store with it. Nothing would report this.

So this ADR adds a fourth surface at `/competitions`: every Competition we collect, as a
card, showing how far along it is, with one button that advances it. Most of what follows
is about what the button will not do, and about which of these "manual inputs" turn out to
be decisions rather than toil.

**Decisions:**

- **The board is the 45 Competitions in the Registry, not everything the provider offers.**
  A card means "we already collect this; is it on the blog?" — so every stage the button
  runs is seconds to minutes against data that already exists. Listing the provider's full
  catalogue was rejected because the button's promise stops being true: onboarding an
  uncollected Competition means `orchestrate`, a quota-bound backfill that can stop
  half-done and resume tomorrow, and a page that models a multi-day resumable job is a
  different product. Adding a Competition to the Registry stays a terminal act, which is
  also what CONTEXT.md already implies — admitting a Competition and admitting a
  Publication are two onboardings against two Registries, "either [of which] can happen
  without the other."

- **A card shows an ordered stage, not a boolean, and the button stops one stage short of
  the end.** The stages are *Collected → In the Published Store → Has a Publication →
  **Draftable** → Live*. A single "ready?" bit was rejected for hiding *where* a
  Competition is stuck, and this pipeline has a specific way of getting stuck that a bit
  renders as success: rows in Postgres, a Publication present, and zero Finals published,
  which reads "ready" while the Desk shows an empty board.
  The button never sets `published = true`. That gate is the Publication's, and CONTEXT.md
  is explicit that it is "false until a human flips it, so a Competition can be drafted
  against long before anything about it is public" — the same separation ADR 0034 keeps
  between drafting a Match Post and publishing one. A five-stage bar with one stage the
  button refuses will invite someone to close the gap; this paragraph is why not.

- **The wholesale publish stays, but its Competition set becomes *derived*.** The board
  computes it — every existing Publication's Competition, plus the one being onboarded —
  so the hazard is removed by construction rather than routed around: there is no longer a
  list a human can under-specify. This also retires the stale `DEFAULT_LEAGUE_IDS`, which
  is the same bug wearing a different hat.
  The additive `publish.delta` was the obvious alternative and was rejected, narrowly. It
  cannot drop anything and takes seconds, but it covers the **current Season only**, so
  Competitions onboarded after today would carry no history while the first three carry
  all of it — two classes of citizen in one store, distinguishable only by their onboarding
  date. It also keys on the Refresh ledger, which is written by `refresh` and never by
  `orchestrate`, so a delta run against freshly-collected data publishes the dimension rows
  and **zero fixtures**, silently. Wholesale re-parses from the raw cache and needs no
  ledger, which removes that ordering constraint entirely. The `refresh` stage stays, for
  cache freshness rather than as a precondition.

- **The six Publication fields stay on a form, because they are decisions, not toil.** The
  request that prompted this ADR was that onboarding has "a lot of manual inputs". Checked
  against the three Publications that exist, deriving them would have been wrong more often
  than right: the slug matches the derived value once in three (`mls`, not
  `major-league-soccer`; `mundial-2026`, not `world-cup`), the display name once in three,
  the language genuinely varies (MLS is `en`), and the World Cup's display timezone is
  `America/Mexico_City` — the *audience's* zone, which no rule about the competition could
  produce. So the form is shown up front, pre-filled with the derived defaults and clearly
  labelled as such, and the run is unattended after that. What this ADR removes is the
  orchestration, the hand-typed id list and the invisible state — not the editorial
  judgment.

- **`football_blog.onboard` becomes the one command that does all of it, and the board only
  spawns it.** It gains non-interactive flags for the six fields plus `--yes`, and takes on
  the publish stage with the derived set. ADR 0021's rule is that the UI is "a *second*
  front door to the same commands, never a reimplementation", and ADR 0034 already paid
  this cost once by promoting `--instruction` to a real flag so nothing was Desk-only. The
  rule bites twice here: the current Publication creation is `input()`-driven, which no
  form can drive; and had the board chained three subprocesses itself, the *ordering* would
  exist only in JavaScript and no terminal line would reproduce it. One command, one argv,
  streamed exactly as the Console and Desk do. The interactive path survives beside the
  flags, and `--yes` with a missing flag must fail rather than invent a slug.

- **Re-running is safe, and the Publication stage is create-if-absent — never update.**
  CONTEXT.md already asserts onboarding "is one-time and idempotent", so a second click
  resumes rather than duplicating; `interactive_create_publication`'s blind `httpx.post`
  ("no upsert helper for Publications since it's rare") becomes a lookup first. The
  never-update half is the load-bearing part. `llm_prompt_overrides` is hand-editable from
  the Desk (ADR 0034's layer 2) and the World Cup's holds 1,577 characters of authored
  guidance; a button that re-submitted form defaults over it would destroy that silently,
  in the one store with no rebuild path. So a second click reports "already exists,
  unchanged" and moves on. Editing those fields stays a PocketBase act.
  Rolling back a failed run by deleting the Publication was rejected for the same reason:
  a leftover Publication is `published=false` and therefore harmless, while a delete is
  destructive and irreversible.

**Consequences:**

- **The card's pre-publish readiness reads `serve.db`, not Postgres.** All eight existing
  checks in `football_blog/onboard.py` query the Published Store, which by definition holds
  none of the 42 Competitions a card would be offering to onboard. `serve.db` already
  carries every Competition plus `league_meta` (team count, matches played, stats-light),
  and ADR 0023 makes it the store a UI may read. The Postgres-based checks keep their role
  as the *post*-publish verification they already are.

- **Deleting a Publication now deletes data.** With the set derived, dropping a Publication
  means the next wholesale publish removes that Competition's rows from Postgres. CONTEXT.md
  says dropping a Publication "changes nothing about what we collect" — still true,
  `football.db` is untouched — but it is no longer true of the Published Store. That follows
  from the store existing to serve the blog, and it is a new consequence of an existing act.

- **The button gets slower with every Competition onboarded**, because each run republishes
  all of them. At four to six that is minutes. Past roughly twenty this is the wrong design
  and the answer is `publish.delta` for the bootstrap with wholesale demoted to a repair
  tool — recorded here so the tipping point is recognised rather than rediscovered.

- **`surfaces/competitions/`, and the nav's fourth item is "Competitions".** Two better-
  sounding names are refused by the glossary: **Leagues** is on the Publication entry's
  `_Avoid_` list precisely because a Publication may cover a cup, and **Coverage** already
  means the provider's per-season stats coverage (ADR 0014). This is the first nav item
  naming a set rather than a place; the alternative was inventing vocabulary.

- **Three things here fail silently (ADR 0033).** A Publication whose Competition has no
  Postgres rows, which reads as success and yields an empty Desk board — the stage on the
  card is what surfaces it. `--yes` accepting a missing slug and inventing one, which
  produces a permanent URL nobody chose. And the create-if-absent rule regressing to an
  upsert, which would overwrite authored prompt overrides with form defaults and report
  success. The first is visible by design; the other two are what the tests target.
