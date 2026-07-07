# Support multiple competitions and split seasons (Liga MX)

Adding Liga MX (league 262) alongside La Liga (140) breaks the one-round-robin-
per-season assumption baked into the schema and standings logic: a single Liga MX
`season` contains **two** self-contained championships, **Apertura** and
**Clausura** (verified: `season=2025` → 337 fixtures, phases `Apertura` 170 /
`Clausura` 167).

**Decisions:**

- **Introduce a `Tournament` dimension on every Fixture.** Parsed from the
  provider `round` string: the prefix before `" - "` (`"Apertura - 5"` →
  `"Apertura"`; `"Regular Season - 12"` → `"Regular Season"`). La Liga fixtures
  all carry `"Regular Season"`, so the model is uniform across both competitions.
- **The unit of a standings table is (Competition, Season, Tournament),** not
  Season. La Liga yields one table per season; Liga MX yields two.
- **Store a `Matchday`** (the numeric suffix of `round`, else null). Playoff
  fixtures (Final, Semi-finals) have no matchday and are excluded from standings —
  a league table reflects the regular phase only, not the Liguilla.
- **Add a `Competition` dimension table** and generalise config from a single
  `LEAGUE_ID`/`SEASONS` to a `COMPETITIONS` list of `{league_id, name, seasons}`.
  Collection and parsing iterate `(league, season)` targets.
- **Team ids stay global.** Provider team ids are unique across competitions, so
  the existing `Team` table just gains Liga MX rows; no per-competition namespacing.

## Considered Options

- **A separate table / DB per competition.** Rejected: players and the schema are
  identical; one `season`+`competition`+`tournament`-keyed store queries uniformly
  and supports cross-competition questions.
- **Encode Apertura/Clausura by splitting the season number** (e.g. `2025.1`).
  Rejected: overloads a field whose meaning (provider season year) must stay intact
  for API calls; a distinct `Tournament` column is explicit and self-documenting.
