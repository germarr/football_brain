# Enrich Competition with provider metadata from the /leagues record

The `Competition` table held only `id`, `name`, `type` — enough to key and label a
competition, but nothing about *what* it is: no country, no code, no crest. We add
four provider-verbatim columns — **country**, **country_code**, **logo**, **flag**
— sourced from the single `/leagues` catalogue record per competition.

**Decisions:**

- **Four nullable columns, taken exactly as the provider gives them.** `country`
  ("Spain"; "World" for a continental/international cup), `country_code` (ISO
  alpha-2 "ES"; null for a "World" cup), `logo` and `flag` image URLs (flag null
  for "World"). Unlike `name` — which we override because the provider labels both
  Italy's and Brazil's league "Serie A" (CONTEXT.md) — none of these collide, so
  there is nothing to make canonical. They are provider-verbatim on purpose, and
  CONTEXT.md's Competition entry now says so explicitly.

- **Single source of truth: the `/leagues` record, not the per-fixture league
  block.** Every fixture's `league` block already carries `country`/`logo`/`flag`,
  but *not* the two-letter `country_code` — that lives only in `/leagues`. Rather
  than split the four fields across two sources, parse reads all four from the one
  `/leagues` record it already reads for cups. One record, one place, no risk of the
  fixture block and the catalogue disagreeing.

- **Collection now fetches `/leagues` for every competition.** Only the
  registry/cup path (`orchestrate._lookup`) ever cached `/leagues`; the five
  flagship built-in leagues (Premier League, Serie A, Bundesliga, Brasileirão, Liga
  MX) had no `/leagues` cache at all. A new `collect.fetch_league(client,
  league_id)` runs once per competition in the collection path so the catalogue
  record is present for all of them — ~24 one-off, cache-fillable calls, negligible
  against the 150k/day cap.

- **Parse degrades to nulls if a record is absent.** The four columns are nullable
  and parse leaves them null when a competition's `/leagues` record is missing from
  the cache, so a rebuild never fails on a competition collected before this change
  — the same tolerance parse already applies to missing squad/bio/stat data
  (ADR 0014).

## Considered Options

- **Take country/logo/flag from the fixture block, code from `/leagues`.** Rejected:
  a two-source split for one logical record invites drift, and it still leaves the
  five flagship leagues with a null `country_code` until they are re-collected — the
  worst of both worlds. Once we fetch `/leagues` for completeness anyway, sourcing
  everything from it is strictly simpler.

- **Drop `country_code` and stay fixtures-only.** Rejected: the ISO code is the one
  field with real analytical value (joining to external country-keyed datasets); the
  image URLs alone would not justify the columns.

- **A per-Season table carrying dates + coverage.** Deferred, not rejected: the
  `/leagues` record also exposes per-season `start`/`end` dates and the full
  `coverage{}` block, which would give the DB a home for the Coverage concept
  CONTEXT.md defines. That is season-level, not competition-level, and belongs in its
  own table — out of scope for this change, which is strictly about constant
  competition attributes.
