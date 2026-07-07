# Add Liga Profesional Argentina, accepting its irregular format

ADR-0006 deferred Argentina (league 128) for two reasons: no per-player stats
before 2016, and a format that changes season to season (varying team counts,
rounds labelled `"2nd Phase"` rather than `"Regular Season"`), which its standings
logic could not model cleanly. On the 150,000/day plan we now add it from 2016/17
onward, treating the format irregularity as a **known, non-fatal limitation**
rather than a blocker. This supersedes the Argentina deferral in ADR-0006.

**Decisions:**

- **Canonical name `"Liga Profesional Argentina"`** (the provider's own label), not
  the bare `"Primera División"` — which is *also* the official name of Spain's La
  Liga, so it would collide. Same rationale as the Serie A / Brasileirão overrides
  (ADR-0006): a competition name must be an unambiguous key.
- **Per-player-stats floor is 2016**, verified empirically (ADR-0005's method, not
  the coverage flags — which claim 2016 has stats but whose *first* 2016 fixture
  returns none). Sampling across seasons: 2015 fixtures/players is empty; 2016 is
  broadly populated (with scattered early-season gaps the pipeline already
  tolerates), 2017+ solid. So we start at 2016, not the 2015 the catalog lists.
- **Calendar-year labelling** (128 added to `CALENDAR_YEAR_LEAGUES`). Argentina is
  a mixed case: pre-2020 seasons straddled two calendar years (2016 ran Aug'16 →
  Jun'17, i.e. 2016/17), but since ~2020 it runs a single calendar year (2024 ran
  May → Dec). One flag cannot label both eras correctly; we choose calendar-year
  because it is right for the majority and most-recent seasons. The stored `season`
  integer is always the provider's own and stays correct for API calls regardless —
  only the display label is affected, so 2016–2019 labels read as single years.
- **Format degrades, it does not break.** Round prefixes vary by season —
  `"Regular Season"` (2016–19), `"Round"` (2023), `"2nd Phase"` (2024),
  `"1st Phase"`/`"2nd Phase"` plus playoff rounds (Round of 16 … Final) (2025). The
  existing `(Competition, Season, Tournament)` standings model treats each phase as
  its own table and excludes playoff fixtures (which carry no Matchday). So
  Argentina yields **per-phase tables**, not one clean league table — acceptable,
  since the raw fixtures, squads, and per-player stats (the point of the project)
  are all intact.

## Considered Options

- **Bespoke multi-phase modelling** that maps each season's phases + playoffs into
  a unified Argentine championship. Rejected for now: high effort for uncertain
  payoff; the per-phase tables and raw data are already useful. Revisit if
  standings fidelity for Argentina specifically becomes a goal.
- **Drop the pre-2020 straddling seasons** to keep every season label clean.
  Rejected: the data is valuable and the stored season integer is correct; a
  cosmetic label mismatch is not worth discarding four seasons of matches.
