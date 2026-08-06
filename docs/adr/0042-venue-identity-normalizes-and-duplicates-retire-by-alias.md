# Venue identity matches on a normalized pair, and a duplicate merges into a canonical Venue

---
Status: accepted, with the merge itself staged — amends ADR 0028 (the stable venue
registry), whose key this changes. Builds on ADR 0019 (a Registry is committed and
reviewed as a diff) and follows ADR 0039's precedent of comparing names canonically.
---

ADR 0028 made `Venue.id` globally stable by minting it once into a committed append-only
registry keyed on `(name, city)` — the pair `parse._venue_key` reads off the provider's
fixture. That fixed the collision it set out to fix. What it did not fix, because nothing
had surfaced it, is that **the provider does not hand us a stable pair**. The same ground
arrives as `Arena do Gremio` and `Arena do Grêmio`, as `Beşiktaş Park` and `Besiktas Park`,
and — most often — with a city one day and a null city the next. Keyed literally, each
spelling mints its own id, and the registry's guarantee decays from *one id per ground* to
*one id per way the provider spelled it*.

Onboarding DBU Pokalen made it visible at volume: its Round of 128 reaches Danish
lower-division pitches, and the 465 grounds it appended included `ALPI Arena Naesby` beside
`ALPI Arena Næsby`. A census then showed this was never a Danish problem: **2,658 of 8,497
venue ids (31%) sit in a cluster where another entry may be the same ground, carrying 43,899
of 132,842 sited fixtures (33%)**, across the FA Cup, the Championship, MLS, the Premier
League, Ligue 1 and the Champions League. It has been latent since the registry was seeded,
because a duplicate is invisible until you count grounds or group fixtures by one.

The census sorts into three classes, and that sort is the whole design:

- **Spelling** — one town, the name or city written two ways. `Arena Nationala` /
  `Arena Națională`, both Bucharest. A fact about strings, decidable from the data.
- **Missing city** — a null city beside exactly one named city. `(A. Le Coq Arena, null)`
  and `(A. Le Coq Arena, Tallinn)`. One ground, almost certainly — but an *inference about
  the world*, not a fact about strings.
- **Ambiguous** — a null city beside several named cities. `Adams Park` in `High Wycombe`,
  `Wycombe` and `High Wycombe, Buckinghamshire`; `Allianz Stadium` in `Sydney` and `Torino`.
  Not resolvable by any rule.

The line this ADR draws runs between the first and the rest, and the reason is not tidiness.
A **missing city** resolution can be *wrong*, and it can *change*. Classifying the registry
as it stood before the DBU batch and again after, one of 852 missing-city names flipped to
ambiguous on that single batch:

```
5912  Stade Pierre Pibarot  null
5913  Stade Pierre Pibarot  Alès
8403  Stade Pierre Pibarot  Clairefontaine-en-Yvelines   ← added 2026-08-06
```

A rule recomputed at load time would have folded `5912` into Alès last week and un-folded it
this week — ids moving silently through the nightly build, which is the one thing ADR 0028
exists to prevent.

**Decisions:**

- **Identity matches on a normalized pair; the raw strings survive as display.** The fold is
  Unicode NFKD, an **explicit** `æ→ae / ø→o / å→aa / ß→ss` map, casefold, collapsed
  whitespace, stripped trailing punctuation. The explicit map is not decoration: NFKD does
  **not** decompose `æ`, so the naive `NFKD → encode('ascii','ignore')` silently *deletes*
  the letter and folds `Næsby` to `nsby`, matching nothing and hiding the duplicate it was
  written to find. That mistake was made while measuring for this ADR and undercounted the
  spelling class by more than half. The fold is a match key only and is never rendered.

- **Resolution lives in `load()`; the stored record stays `{id, name, city}`.** `load()`
  folds every entry, groups collisions and returns *every* raw key pointing at its group's
  canonical id; `_load_raw()` returns the file's records for `mint()` to append to. Nothing
  is added to the record — which is not a stylistic choice: `mint()` does
  `mapping = load(); _write(mapping)`, rebuilding each entry as `{id, name, city}` from a
  `dict[VenueKey, int]`, so **any extra field committed into `venues.json` is silently
  discarded by the next nightly build**. An alias field would have vanished without a diff.

- **The canonical Venue is the lowest id in the group.** Ids only ever grow, so
  lowest-id-wins is the one choice that cannot flip when a new spelling is appended. A merged
  id keeps its line in the registry forever and is simply never assigned again — never
  renumbered, never deleted, because ADR 0028's delta publish is sound only while existing
  ids do not move.

- **A merged Venue supplies its id, but not necessarily its name.** In 89 of 129 spelling
  clusters the lowest id holds the *mangled* spelling, so lowest-id-wins would fix the merge
  and visibly degrade the Viewer — `Arena do Gremio`, `Besiktas Park`. The display strings
  therefore come from the richest spelling in the group (most non-ASCII characters), with an
  optional `display` override on a merge row for the cases the heuristic misses: for
  `Doosan Arena` / `Doosan Aréna` / `Plzeň` it picks the city correctly and the name wrongly,
  since only the city takes the háček. The asymmetry is deliberate — an **id** is
  load-bearing, referenced by fixtures in four stores and unable to move, while a **display
  string** is cosmetic and correcting one is a value update no delta constraint touches.

- **A spelling merge is derived; every other merge is decided and committed.** The principle:
  *a derived merge must be conservative because nobody reviews it; a proposed merge may be
  liberal because someone does.* So `load()`'s fold requires an identical folded name **and**
  an identical folded city — it merges spellings and nothing else, silently and
  deterministically. Missing-city and ambiguous merges are rows in a committed **Venue merge
  list**, `football/registry/venue_merges.json`, each `{"from": <id>, "into": <id>,
  "class": ...}`. Keyed on ids, not strings, so a later tightening of the fold cannot change
  what a past decision meant. It is a Registry in CONTEXT.md's sense: decided, not derived,
  reviewed as a diff.

- **Missing-city merges are machine-proposed, human-confirmed, then frozen.** A proposal tool
  emits each candidate with both entries and their fixture counts; confirmed rows enter the
  merge list and are **never recomputed**. A later contradiction like Pibarot moves nothing —
  the frozen decision stands, and revising it is a deliberate edit plus a wholesale publish.
  A frozen decision can therefore be *wrong*, and that is the accepted trade: a wrong merge
  is visible in a committed file and fixable, while a moving id corrupts the Published Store
  with no diff anywhere.

- **The proposal tool normalizes cities more loosely than the fold does.** Truncating the
  city at its first comma turns `Washington, District of Columbia` into `Washington` and
  moves **74 of 171** ambiguous clusters into the proposable class. Across all 8,497 entries
  that truncation fuses two distinct city strings exactly once — `Farsley, Leeds, West
  Yorkshire` and `Farsley, West Yorkshire`, correctly. It stays out of `load()`'s fold
  regardless, because a hypothetical `Newport, Wales` / `Newport, Isle of Wight` fusion must
  arrive as a proposal to reject, never as a silent merge.

- **A merge commit and a wholesale `publish_pg` are one unit of work.** The delta upserts the
  `venue` dimension (`DELTA_DIMENSIONS`), so between a merge landing and a wholesale publish
  the store is safe but **mixed**: touched fixtures carry canonical ids, untouched history
  keeps merged ones, both venue rows present, no FK broken. Only the wholesale publish — ADR
  0028's designated re-baseline tool — rebuilds the dimension and closes it. Since ADR 0032's
  nightly runs whatever branch is checked out and `scripts/nightly.sh` contains no wholesale
  publish, a merge left overnight widens that mixed state unattended.

- **This commit lands the mechanism, not the merge.** The fold, the derived resolution,
  `_load_raw()`, the merge-list format and the proposal tool ship first; `venues.json` is
  untouched, so the nightly is unaffected. The confirmed merge list, one full rebuild and one
  wholesale publish follow together in a single window.

## Consequences

- When the merge runs: **199 ids merge by derivation** (194 spelling clusters, 6,843 fixtures
  re-point) and **977 by decision** (975 missing-city clusters, 3,668 fixtures) — 1,176 ids
  and 10,511 fixtures in total. The registry keeps all 8,497 lines; merging adds a row to a
  second file and removes nothing from the first.
- **97 ambiguous clusters, 374 ids and 8,374 fixtures stay split** until reviewed. Anything
  grouping fixtures by ground is still wrong for those and should be read as such. That is
  the largest single block of affected fixtures, and it is the block no rule can touch.
- **One wholesale `publish_pg` becomes mandatory** after the merge lands. MLS alone has 3,390
  fixtures on ambiguous ids, so the Published Store is genuinely affected.
- **`--heal-venues` needs no change** — it resolves through `venues.load()`, inheriting the
  fold and the merge list for free.
- **`_venue_key`'s contract narrows**: it still returns the raw pair, but the raw pair is no
  longer the identity. Any future comparison of venue keys outside `load()` would be
  comparing the wrong thing.
- **A new spelling that folds differently still mints a new id** — a renamed sponsor, a
  translated city. The fold shrinks that class; it cannot close it. The merge list is the
  permanent escape hatch, which is why it is worth building even though spellings are
  automatic.

## Considered Options

- **Key on the provider's `venue.id`.** Rejected, and already rejected once in
  `_venue_key`'s docstring: it is null for many grounds and disagrees with itself across
  responses. That trades a duplicate problem for a null-identity problem.
- **An `alias_of` field on the registry record.** The first draft of this ADR. Rejected on
  reading `_write`: the field cannot survive the writer, and would have failed silently at
  the next nightly mint.
- **Merge by renumbering, then re-baseline.** ADR 0028 notes that losing the registry costs
  only "a one-time global re-baseline" and that the ids are "surrogates nobody pins
  externally." Rejected because it makes every merge a global event — delta publishing
  suspended, every scoped store rebuilt, the whole Published Store re-COPYed — to save one
  file. Merging by list is incremental and reversible; renumbering is a migration.
- **Resolve missing-city by a rule at load time.** Rejected on evidence: the Pibarot flip
  above, arriving silently through the nightly.
- **Auto-merge the ambiguous class on a "same town" heuristic.** Rejected on measurement, not
  principle — the heuristic was written and run, and split 171 clusters 86/85 while getting
  both `Audi Field` (Buzzard Point is a neighborhood of Washington) and `Allianz Stadium`
  (Sydney and Torino are two grounds; Torino and Turin are one town) wrong.
- **Normalize at read time in each consumer.** Rejected: it puts the fold in every query
  touching a Venue, leaves the stored ids duplicated forever, and does nothing for the
  Published Store, which has no query layer of ours in front of it.
- **Do nothing; the ids are surrogates.** Rejected because the duplication is not confined to
  the surrogate. It splits the Venue dimension in every store, and it is exactly the "same
  stadium, same id" property ADR 0028 guarantees and CONTEXT.md states as fact — a guarantee
  that is 31% untrue is worse than one that is qualified.
