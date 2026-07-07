# Expand to Europe's big leagues + Brazil; defer Argentina and careers

On the Ultra plan (75,000/day) we add four competitions from 2015/16, all with
per-player stats verified back to season 2015:

- Premier League (39), Serie A / Italy (135), Bundesliga (78) — standard
  starting-year seasons, single "Regular Season".
- Brasileirão (71) — single "Regular Season", but a **calendar-year** season
  (Apr–Dec), so its season number is the year itself, not a straddling "2024/25".

**Decisions:**

- **Canonical competition names override the provider's.** API-Football labels
  *both* Italy's and Brazil's league `"Serie A"`. Storing that verbatim would
  merge two competitions. `config.COMPETITION_NAMES` maps league id → our name
  (Italy `"Serie A"`, Brazil `"Brasileirão"`), applied in `parse`.
- **Calendar-year leagues are flagged** (`config.CALENDAR_YEAR_LEAGUES`) so season
  labels read `"2024"` for Brazil, not `"2024/25"`.
- **Argentina (128) is deferred.** No player stats before 2016, and the format is
  inconsistent season to season (26→28 teams, shifting windows, rounds labeled
  `"2nd Phase"` not `"Regular Season"`). Its standings would be unreliable; revisit
  with proper multi-phase handling before adding it.
- **Career histories are deferred** (`config.COLLECT_CAREERS = False`). The core
  match+bio collection is ~28k calls; adding players/teams for every player is
  another ~18.5k and is better done as its own run.

## Considered Options

- **Trust the provider league name.** Rejected — the "Serie A" collision alone
  makes it unusable as a key.
- **Include Argentina now with a caveat flag.** Rejected for this pass to keep the
  dataset's standings trustworthy; it needs real format modelling, not a footnote.
