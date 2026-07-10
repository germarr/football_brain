# Alt Data — Football

Collecting per-match player performance and biographical data across multiple
competitions — **La Liga, Premier League, Serie A, Bundesliga** and
**Brasileirão** (2015/16 onward) plus **Liga MX** and **Liga Profesional
Argentina** (2016/17 onward) — sourced from the API-Football provider. (FC
Barcelona is the default view for exploration, not a data boundary.)

The modeled store is one SQLite file, `data/football.db`, holding every
Competition (`football.parse`). A single Competition can be extracted into its own
`data/<slug>.db` from the same raw cache with `python -m football.scope <id>` (and
removed with `--delete`) — a zero-API re-parse, so it needs the data already
collected (ADR 0011).

## Language

**Competition**:
A league we collect, identified by a stable provider league id: La Liga (140),
Premier League (39), Serie A (135), Bundesliga (78), Brasileirão (71), Liga MX
(262), Liga Profesional Argentina (128). Its name is **our** canonical name, not
the provider's — the API labels both Italy's and Brazil's league "Serie A", so we
override (Italy → Serie A, Brazil → Brasileirão); Argentina's top flight is kept
as "Liga Profesional Argentina" rather than the bare "Primera División", which
collides with Spain (La Liga is officially Primera División too).
A Competition is of one **type**: a **league** (a domestic round-robin table —
all seven collected so far) or a **cup** (a group+knockout competition such as the
Champions League or World Cup). The type is the provider's own `league.type` and
governs how a Season's Fixtures are structured into Tournaments and Phases.
A Competition also carries provider-verbatim **metadata**: its **country** (the
nation it belongs to — `"Spain"`, or `"World"` for a continental/international cup
such as the Champions League or World Cup), a two-letter ISO **country_code**
(`"ES"`; null for a `"World"` competition), and **logo** / **flag** image URLs
(the flag null for `"World"`). Unlike the canonical `name`, these are taken exactly
as the provider gives them — none of them collide the way the two "Serie A"s do, so
there is nothing to override — and all four come from the single `/leagues`
catalogue record, not the per-fixture league block (ADR 0015).
It also carries a **continent** — but this one is **derived, not a provider fact**:
the API exposes no continent, so we map the `country` name through a static
country→continent table (`"Spain"` → `"Europe"`). A `"World"` cup maps to
`"International / Intercontinental"`, so every continental cup (even the European
Champions League) lands there rather than under its own confederation's continent
(ADR 0016).
_Avoid_: using bare "league" as the canonical noun (a Competition may be a cup);
confusing a Competition with a Tournament (a sub-division of one Competition's
season); trusting the provider's league name as a key; overriding
country/code/logo/flag (only `name` is canonical — the metadata is provider-
verbatim); reading a cup's `"World"` country or null country_code/flag as missing
data; treating `continent` as a provider field (it is derived from `country`);
expecting the Champions League's continent to be `"Europe"` (a `"World"` cup is
`"International / Intercontinental"`).

**Season**:
The provider's season for a Competition, identified by a single year (e.g.
`2024`). For European leagues and Liga MX this is the **starting** year of a
campaign that straddles two calendar years (2024/25). For calendar-year leagues
(Brasileirão) the Season **is** that one calendar year — so its label is `"2024"`,
not `"2024/25"`. A Season contains one or more **Tournaments**.
_Avoid_: year (ambiguous); labelling a calendar-year Season as "YYYY/YY".

**Coverage**:
What the provider exposes for a given Season — which classes of data exist to
collect at all. Two facts matter: **player coverage** (`statistics_players`)
governs whether per-player data exists (Squad Entries, and the bios and Career
Stints keyed off the players they surface); **fixture coverage**
(`statistics_fixtures`) governs whether per-team match aggregates exist (possession,
shots, xG). Fixtures and Events are collected **regardless** of Coverage — they
always exist. A Season (or a whole Competition, e.g. Liga MX Femenil) with neither
flag is **stats-light**: a valid fixtures+events-only store, not broken or
half-collected data. Coverage is per-Season, so one Competition may hold
stats-light early Seasons and fully covered recent ones.
_Avoid_: reading a missing Squad Entry / team match stat as a collection failure;
assuming every Season of a Competition shares one Coverage; treating "stats-light"
as a Competition type (it is a Coverage state — the Competition is still a league
or a cup).

**Tournament**:
A self-contained championship within a Season — one champion crowned. La Liga:
one, `"Regular Season"`. Liga MX: two, `"Apertura"` and `"Clausura"`. A Tournament
is played out over one or more **Phases**: a domestic league is a single
round-robin phase, whereas a Champions League / World Cup Tournament runs a group
phase then knockout phases. Standings are computed per (Competition, Season,
Tournament, Phase, Group) — for a phase-less league that collapses to the familiar
per-Tournament table, and for a cup they apply only to the group Phase.
_Avoid_: split, torneo; treating a group or a knockout round as its own
Tournament (they are Phases of one Tournament).

**Phase**:
A cup-only refinement: which part of a cup Tournament a Fixture belongs to, of one
of three kinds. **qualifying** — the pre-tournament elimination rounds that decide
who enters (`Preliminary Round`, `1st/2nd/3rd Qualifying Round`, `Play-offs`);
tagged so they can be filtered out of the real bracket. **group** — the
round-robin table portion, whether true parallel Groups (`Group A`…`Group H`), a
World Cup `Group Stage`, or the new Champions League single-table `League Stage`;
carries a Matchday. **knockout** — a bracket round that advances toward the
champion (`Knockout Round Play-offs`, `Round of 16`, `Quarter-finals`,
`Semi-finals`, `3rd Place Final`, `Final`); no Matchday, and a tie may span two
legs sharing one round string. A **league**-type Competition has **no** Phase (all
phase fields null) — it is a single round-robin, unchanged from before.
_Avoid_: lumping bare `Play-offs` (qualifying) with `Knockout Round Play-offs`
(knockout); treating a group or knockout round as its own Tournament; conflating
Phase (which part of the cup) with Matchday (the round number within a group).

**Group**:
One round-robin pool within a **group** Phase, named by a letter (`Group A`…`Group
H`) when the provider exposes it. Only old-format Champions League groups carry the
letter in the Fixture's round string; a World Cup `Group Stage` and the new
Champions League `League Stage` do not, so their Group is unknown (null) at
Fixture level — recovering it would need the standings endpoint (out of scope).
_Avoid_: assuming every group Phase has a recoverable Group letter.

**Matchday**:
The round number within a Tournament's regular schedule, parsed from the provider
`round` string. Playoff fixtures (finals, semis) have no Matchday and are excluded
from the standings table.
_Avoid_: gameweek, jornada, week.

**Fixture**:
A single scheduled match between two teams, identified by a stable provider
fixture id. `home_goals`/`away_goals` are the **on-pitch** result — the score after
extra time, else full time — and never include a penalty shootout; a shootout is
kept separately in `penalty_home`/`penalty_away` (both null unless the match was
decided on penalties, status `PEN`). This is deliberate: the provider's raw `goals`
field conflates the shootout into the scoreline for `PEN` games (ADR 0012).
_Avoid_: game, match; reading `home_goals`/`away_goals` as the shootout result;
deciding a `PEN` tie's winner from goals alone (compare the penalty columns).

**Team**:
A club **or national team** that contests Fixtures, identified by a stable
provider team id. In a domestic league the Teams are the ~20 clubs; in the World
Cup they are national teams (Argentina, France); the Champions League fields
clubs. All share one `Team` table and one id space. A Fixture is played between a
home and an away Team. The `Team` table holds only the Teams that contest a
**collected** Fixture (both sides of every Fixture we store); the wider universe
of teams a player's career touches lives in **Team Profile**.
_Avoid_: club (use "Team" — a national team is a Team too), side; conflating the
in-scope `Team` table with the all-career-teams **Team Profile** directory.

**Team Profile**:
The enriched **dossier** for a Team — any team a player's Career Stint touches,
including the thousands of out-of-scope clubs and national/youth sides beyond the
Competitions we collect. One row per distinct `PlayerTeam.team_id`: the club's
identity (name, country, founded year, crest, whether it is a national team) plus
its single **representative league** — the domestic league it currently plays in
(`type = "league"`, own country, most recent season), which is null for a national
team or a club we cannot resolve. It is the persisted answer to "what league does
this team belong to," backing **League of Origin**. Club identity comes from the
provider's teams endpoint and the league from its leagues-by-team endpoint, called
only for teams not already covered by our own Fixtures; national/youth sides and
provider-unknown ids degrade to a minimal name-only row rather than being dropped,
so the directory covers **every** career team. It is a superset of the `Team`
table by id, but a separate directory — `PlayerTeam.team_id` is still not a foreign
key (enrichment lags collection); Team Profile is the documented **join target**.
_Avoid_: club dossier, team registry; treating it as the in-scope `Team` table
(it is a superset directory); expecting a national team or an unresolved club to
carry a representative league; reading a null-heavy row mid-backfill as broken (it
fills progressively); making it a foreign-key target for Career Stints.

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
including unused substitutes — but only for a Season with player **Coverage**; a
stats-light Season's Fixtures carry no Squad Entries at all.
_Avoid_: lineup (the lineup is only the starting XI, not the whole squad); reading
a Fixture's absent Squad Entries as missing data (see Coverage).

**Appearance**:
The subset of Squad Entries where the player actually played (`minutes > 0`) —
i.e. `started` or `came_on`. Per-90 and per-appearance stats are computed over
Appearances, never over all Squad Entries.
_Avoid_: cap (informal), calling an `unused_sub` an appearance.

**Event**:
A single time-stamped occurrence within a Fixture — a goal, card, substitution,
or VAR decision — sourced from the fixtures/events endpoint. Carries the minute it
happened (plus any Added Time), the Team and Player involved, and a type/detail
pair (Goal → Normal Goal | Penalty | Own Goal; Card → Yellow | Red; subst; Var).
An Event is the **timeline**; it is distinct from the aggregate per-match totals
carried on a Squad Entry (goals, cards, penalties), which say *how many* but not
*when* or *what kind*. An Event is a moment of **play** — regulation or extra time.
A penalty **shootout** kick is *not* an Event: it belongs to the tie-break, not the
match, and its outcome is a Fixture-level fact (`penalty_home`/`penalty_away`, ADR
0012), so shootout kicks are dropped at parse time and never reach the Event table
(ADR 0013).
_Avoid_: incident, moment; conflating an Event with a Squad Entry's stat totals;
counting a shootout kick as a Goal Event.

**Assist**:
The Player credited with setting up a **Goal** — a field on the Goal Event, not an
Event in its own right. The "minute of an assist" is therefore the minute of the
goal it created; there is no separate assist timeline.
_Avoid_: treating an assist as a standalone Event; counting assist minutes apart
from their goal's minute.

**Added Time**:
Minutes played beyond a half's nominal end, recorded **per Event** as an `extra`
value — a goal at 90+3' is minute `90`, extra `3`. We do not store the referee's
announced total added time per half: the provider does not expose it, and deriving
it from events undercounts.
_Avoid_: storing a per-half "stoppage time" total; inferring announced added time
from Events.

**Career Stint**:
One `(Player, Team, Season)` the provider records for a player, sourced from the
players/teams endpoint. Unlike an Appearance, a Career Stint spans the player's
**whole career across every competition and national team** — including clubs
outside our collected Competitions (e.g. Ronaldo's Manchester United, Juventus,
Al-Nassr, Portugal) — so its `team_id` is a global provider id that is _not_
constrained to the `Team` table and carries a denormalized `team_name`. "All the
teams a player played for" is `SELECT DISTINCT team_id FROM playerteam`.
A Career Stint carries **no league and no country** — only the team id and a
denormalized name (the provider's players/teams endpoint exposes nothing more).
Attaching a league or country to a stint is therefore a *derived* step, not a
lookup: it needs an external team→league resolution (see **League of Origin**).
_Avoid_: treating a Career Stint as an Appearance (it carries no per-match stats
and is not scoped to a Fixture); assuming its seasons align with our fixture data;
expecting a stint to tell you which league or country the team belongs to.

**League of Origin**:
For a player in a target Competition, the **domestic league of the club they
joined that Competition from** — the club held at the latest Career-Stint season
at or before they joined their current club, with **national teams excluded**
(clubs only). A stint has no league of its own, so the prior club's
`team_id` is resolved to a league through its **Team Profile** (the persisted
representative domestic league of every career team). A club whose Team Profile
has no representative league — a national team, or a club we cannot resolve —
is **Unknown**. (Before Team Profile existed this was derived ad-hoc by matching
the team id against the fixtures of the whole modeled store; Team Profile now
persists that answer for every team, resolved once via the provider rather than
re-inferred from our own fixtures.) A player
whose only club history is their current club has **no** League of Origin — they
are **homegrown/debut**, a distinct value from having come from another club in
the same league.
_Avoid_: reading a League of Origin off a Career Stint directly (it must be
resolved); letting a cup stand in as a league of origin; conflating homegrown (no
prior club) with a prior stint at another club in the same league; assuming every
prior club resolves (a club outside the collected Competitions is Unknown).

**Refresh**:
The nightly re-collection of only the **mutable frontier** of the data: for every
Competition (leagues *and* cups), the **current Season** — the latest season in the
Competition's config — and nothing older. Immutable past Seasons are never re-fetched.
Because the raw client is cache-first (a fixture, once played, is fetched once ever),
a Refresh must deliberately re-fetch the two things that can still change: the current
Season's **fixture list** (cached as a single `fixtures?league&season` file, so it
otherwise never surfaces matches played since it was written) and any **Fixture that
was not yet final** when last cached (its players/events/stats were cached empty while
it was still scheduled). A Refresh collects *new* data; it never mutates an
already-final Fixture.
_Avoid_: calling a full historical backfill a Refresh; re-fetching past Seasons;
assuming cache-first alone surfaces new matches (the season fixture list must be
force-refreshed); treating a Refresh as touching every Season of a Competition (only
the current one).

**Final**:
A Fixture's terminal, played-to-completion state — provider `status.short` of `FT`
(full time), `AET` (after extra time), or `PEN` (decided on penalties). Only a Final
Fixture has per-fixture data to collect (squad, events, team match stats), so a
**Refresh** collects a Fixture's per-match data exactly once it is Final and never
before. Non-terminal states (`NS`, live, `HT`, `TBD`) and terminal-but-dataless ones
(`PST` postponed, `CANC`, `ABD`, `SUSP`, `AWD`/`WO`) are **not** Final: they carry no
collectable played result and are simply revisited on later nights until they become
Final or their Season rolls over. Distinct from a scoreline existing: an `AWD`
walkover has a result but is not Final (no lineup or events).
_Avoid_: treating any non-null scoreline as Final; collecting per-fixture data for a
scheduled or live match (that is the stale-empty trap the Refresh avoids); reading a
`PST`/`CANC` Fixture as data loss.
