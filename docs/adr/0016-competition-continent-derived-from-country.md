# Derive a Competition's continent from its country

The `/leagues` record we already read for a Competition's country/code/crest/flag
(ADR 0015) exposes **no continent** — only `country {name, code, flag}`. We still
want a `continent` on the Competition row (to group and filter competitions by
region), so we **derive** it: parse maps the country name through a static
`config.COUNTRY_CONTINENT` table.

**Decisions:**

- **`continent` is a derived column, not a provider fact.** Every other metadata
  field on Competition is taken verbatim from the provider; this one is computed.
  The model comment and CONTEXT.md both say so, so a future reader does not go
  hunting for a `/leagues` continent field that never existed.

- **The mapping is the full 171-country table, extracted from
  `context/leagues-by-continent.md`.** That doc is the API's own `/leagues`
  catalogue grouped by continent, so its country→continent grouping is authoritative
  and its country spellings match the API by construction ("USA", "England", "Congo
  DR"). Baking in the whole table — not just the ~18 countries we collect today —
  means a newly orchestrated league in any country auto-resolves its continent with
  no code change, the same "just works" property country/logo/flag have. The table
  lives grouped-by-continent for readability and is inverted to a flat
  `COUNTRY_CONTINENT` lookup at import.

- **A `"World"` competition maps to `"International / Intercontinental"`.** The
  provider labels every continental and global cup — Champions League, Libertadores,
  Copa America, World Cup — with `country="World"`, so a country-derived continent
  necessarily puts all of them in one international bucket. We accept that: the
  Champions League's continent is `"International / Intercontinental"`, **not**
  `"Europe"`. This matches the source doc exactly and keeps continent a pure
  function of country.

- **An unmapped country yields null**, not an error. `COUNTRY_CONTINENT.get(country)`
  returns None for a country outside the table (or when the whole `/leagues` record
  is absent), so the build never fails on an unrecognized country — it just leaves
  `continent` null, the same graceful degradation the other metadata fields use.

## Considered Options

- **A per-competition override so continental cups get their real confederation
  continent** (Champions League → Europe, Libertadores → South America). Rejected:
  it makes continent a function of *two* inputs (country *and* an override table),
  reintroducing the maintenance burden the pure-country map avoids, and it disagrees
  with the source doc. If confederation-level grouping is ever needed, a separate
  `confederation` column (UEFA/CONMEBOL/…) is the honest way to model it, not a
  fudged continent.

- **A country→continent library (e.g. pycountry-convert).** Rejected: a new runtime
  dependency for a static 171-row table, and its country names/spellings would not
  necessarily match the provider's, forcing a reconciliation layer anyway. The doc
  we already have is the exact, provider-aligned source.

- **Only the ~18 countries we collect today.** Rejected: a new orchestrated league
  from an uncovered country would silently get a null continent, breaking the
  "register a league and every field populates" property (ADR 0015). The full table
  costs nothing extra to carry.
