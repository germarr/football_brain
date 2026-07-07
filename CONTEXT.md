# Alt Data — Football

Collecting per-match player performance and biographical data across multiple
competitions — **La Liga, Premier League, Serie A, Bundesliga** and
**Brasileirão** (2015/16 onward) plus **Liga MX** (2024/25 onward) — sourced from
the API-Football provider. (FC Barcelona is the default view for exploration, not
a data boundary.)

## Language

**Competition**:
A league we collect, identified by a stable provider league id: La Liga (140),
Premier League (39), Serie A (135), Bundesliga (78), Brasileirão (71), Liga MX
(262). Its name is **our** canonical name, not the provider's — the API labels
both Italy's and Brazil's league "Serie A", so we override (Italy → Serie A,
Brazil → Brasileirão).
_Avoid_: using bare "league" as the canonical noun; confusing it with a
Tournament (a sub-division of one Competition's season); trusting the provider's
league name as a key.

**Season**:
The provider's season for a Competition, identified by a single year (e.g.
`2024`). For European leagues and Liga MX this is the **starting** year of a
campaign that straddles two calendar years (2024/25). For calendar-year leagues
(Brasileirão) the Season **is** that one calendar year — so its label is `"2024"`,
not `"2024/25"`. A Season contains one or more **Tournaments**.
_Avoid_: year (ambiguous); labelling a calendar-year Season as "YYYY/YY".

**Tournament**:
A self-contained championship within a Season — its own round-robin and champion.
La Liga: one, `"Regular Season"`. Liga MX: two, `"Apertura"` and `"Clausura"`.
Standings are computed per (Competition, Season, Tournament), never per Season
alone.
_Avoid_: phase, stage, split, torneo.

**Matchday**:
The round number within a Tournament's regular schedule, parsed from the provider
`round` string. Playoff fixtures (finals, semis) have no Matchday and are excluded
from the standings table.
_Avoid_: gameweek, jornada, week.

**Fixture**:
A single scheduled match between two teams, identified by a stable provider
fixture id.
_Avoid_: game, match.

**Team**:
A club competing in the league, identified by a stable provider team id. La Liga
fields 20 teams per season. A Fixture is played between a home and an away Team.
_Avoid_: club (use "Team"), side.

**Player**:
An individual footballer, identified by a stable provider player id, carrying
biographical attributes (nationality, birth, height, weight).
_Avoid_: athlete.

**Nationality**:
The footballing nation a player represents — the canonical meaning of "country"
in this project. Distinct from **birth country** (where the player was born),
which is stored separately and can differ (dual-nationals, diaspora players).
_Avoid_: using bare "country" to mean birthplace.

**Age**:
Always a *derived* quantity, computed from the player's birth date at the
**fixture date** (how old they were in that match) — never the provider's `age`
field, which is computed as-of-fetch-time and drifts.
_Avoid_: storing a static age column.

**Squad Entry**:
A player's slot in a fixture's matchday squad (~22 per team), tagged with a
status: `started`, `came_on`, or `unused_sub`. Every named player has one,
including unused substitutes.
_Avoid_: lineup (the lineup is only the starting XI, not the whole squad).

**Appearance**:
The subset of Squad Entries where the player actually played (`minutes > 0`) —
i.e. `started` or `came_on`. Per-90 and per-appearance stats are computed over
Appearances, never over all Squad Entries.
_Avoid_: cap (informal), calling an `unused_sub` an appearance.

**Career Stint**:
One `(Player, Team, Season)` the provider records for a player, sourced from the
players/teams endpoint. Unlike an Appearance, a Career Stint spans the player's
**whole career across every competition and national team** — including clubs
outside our collected Competitions (e.g. Ronaldo's Manchester United, Juventus,
Al-Nassr, Portugal) — so its `team_id` is a global provider id that is _not_
constrained to the `Team` table and carries a denormalized `team_name`. "All the
teams a player played for" is `SELECT DISTINCT team_id FROM playerteam`.
_Avoid_: treating a Career Stint as an Appearance (it carries no per-match stats
and is not scoped to a Fixture); assuming its seasons align with our fixture data.
