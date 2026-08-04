# Alt Data — Football

Collecting per-match player performance and biographical data across dozens of
competitions — domestic **leagues** (La Liga, Premier League, Serie A, … from
2015/16 onward) and **cups** (Champions League, World Cup, Copa del Rey, …) —
sourced from the API-Football provider. (FC Barcelona is the default view for
exploration, not a data boundary.)

The modeled store is one SQLite file, `data/football.db`, holding every
Competition (`football.parse`). A single Competition can be extracted into its own
`data/<slug>.db` from the same raw cache with `python -m football.scope <id>` (and
removed with `--delete`) — a zero-API re-parse, so it needs the data already
collected (ADR 0011).

## Language

**Competition**:
A league or cup we collect, identified by a stable provider league id (e.g. La Liga
is 140, Italy's Serie A 135). Its name is **our** canonical name, not
the provider's — the API labels both Italy's and Brazil's league "Serie A", so we
override (Italy → Serie A, Brazil → Brasileirão); Argentina's top flight is kept
as "Liga Profesional Argentina" rather than the bare "Primera División", which
collides with Spain (La Liga is officially Primera División too).
A Competition is of one **type**: a **league** (a domestic round-robin table) or a
**cup** (a group+knockout competition such as the Champions League or World Cup). The type is the provider's own `league.type` and
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

**Registry**:
A small committed file naming the entities the recurring jobs must cover. There are
two: the **Competition registry** — the single source of truth for every Competition
we collect, league and cup alike (ADR 0019) — and the **Venue registry**, an
append-only `(name, city) → stable id` map that gives the same stadium the same id in
every store (ADR 0028).
A Registry is **input** to the pipeline, never its output: alone among the data here
it is *decided* rather than fetched or derived, so it has no rebuild path from the raw
cache, and every change to it is reviewed as a diff. Nothing is collected, parsed or
published for an entity no Registry names.
_Avoid_: config (a Registry names entities; it does not tune behaviour); store,
database (every store is derived by a run that *reads* a Registry); cache (a Registry
cannot be refetched); treating a Registry as regenerable.

**Onboarding**:
Admitting an entity to a **Registry** so the recurring jobs begin covering it — the
decision that a Competition is ours to collect, or (as a **Publication**) ours to
write about. It is one-time and idempotent, and its effect is entirely forward: every
later **Refresh**, parse and publish includes the entity without being told again.
Onboarding is not **backfilling**. A backfill bulk-fetches an entity's Seasons into
the raw cache; it is resumable, quota-bound, and admits nothing. Onboarding is the
decision, backfilling the labour that follows — one command may do both
(`football.orchestrate` registers a Competition and then collects it) — but they fail
differently, and that is why they are named apart: a backfill cut short resumes for
free, while an entity that was never onboarded is covered by nothing, however much of
its data already sits in the cache.
_Avoid_: collecting, importing, ingesting (those move data; onboarding admits an
entity); treating onboarding a Publication and onboarding a Competition as one act
(same shape, different Registries, and either can happen without the other).

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

**Coverage Fingerprint**:
The record, on each ledger entry, of which Coverage-gated stat stages a Final actually
carries — `{"players": bool, "fixture_stats": bool}` (Events is unconditional, so never
fingerprinted). It is per-*Fixture*, distinct from the per-*Season* **Coverage** flag,
because the season flag both is coarse and *lags the data*: a brand-new Season opens
**stats-light** and the provider flips `statistics_players` / `statistics_fixtures` true
days later — but a Final's `fixtures/players` and `fixtures/statistics` may already return
populated payloads *before* the flag flips, and unevenly across a matchday. So **Refresh**
does not gate collection on the season flag; it uses the fingerprint to decide, per Final,
what stat data it still owes, and it fingerprints a stage `true` on **data actually landing**,
never on the season flag alone:
- if the Season covers a stage, attempt it (a first collection, or a flag-flip *widen* that
  re-heals a Final collected earlier under narrower Coverage);
- if the Season is stats-light but the stage is **expected** (the Competition's last
  completed Season carried it — a lag, not a genuinely stats-light Competition like Liga
  MX Femenil) and the Final is recent (within `OPTIMISTIC_PROBE_DAYS`, 14), probe it
  optimistically.
Either way, the stage is stamped `true` only when the fetch **returns data**, or when it comes
back empty *and* the Final has aged past the window (a genuinely-empty match we stop chasing).
An empty fetch on a still-recent Final — even one whose Season is covered — is left **owing**
and retried nightly, because the flag lags per-*Fixture* too: data lands unevenly across a
matchday, so an empty within the window is *lagging*, not *absent* (amendment 3). A legacy
entry with no fingerprint counts as *nothing collected* — re-evaluated (near-free, cache hits)
and re-stamped on the next run. See ADR 0018 (amendments 2026-07-17).
_Avoid_: reading the fingerprint as the Season's Coverage (it is what a match was actually
collected under, which for a lagging Season can be *ahead* of the season flag); expecting
a genuinely stats-light Competition (no prior covered Season) to be probed — it never is.

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

**Team Match Stat**:
The per-Team, per-Fixture aggregate line — possession, shots (and their break­down),
corners, offsides, fouls, saves, passes and pass accuracy, plus **Expected Goals** —
two rows per Fixture (one per Team). It is the Team-level counterpart to a Squad
Entry (per player) and an Event (per moment): it says how a **Team** performed over
the whole match, not who did what or when. Present only for a Season with player
**Coverage**; absent for stats-light Seasons.
_Avoid_: conflating with Squad Entry (that is per player) or Event (that is per
moment); reading a Team Match Stat's `yellow`/`red` as a timeline (it is a count).

**Match Rating**:
The provider's 0–10 performance score for a player in a Fixture, carried on the
Squad Entry (`rating`). Defined only for players who featured and only where the
provider supplies it — Coverage-dependent and often absent, so any consumer must
degrade gracefully when a Fixture has no ratings. "Man of the Match" is a **derived**
label — the highest Match Rating in a Fixture — not a stored fact.
_Avoid_: treating a Match Rating as objective or always present; inventing a Man of
the Match when ratings are missing.

**Expected Goals**:
A model estimate of how many goals a Team's chances were worth in a Fixture (xG),
supplied by the provider as a single per-Team, per-Fixture number on the Team Match
Stat (`expected_goals`) — **not** stored per shot or per player, so it cannot be
attributed to an individual from our data. Comparing xG to the actual scoreline is
the canonical "did the result flatter them?" read.
_Avoid_: attributing xG to a player or a shot; treating xG as a count of real goals.

**Tournament Run**:
One Team's sequence of played Fixtures within a single Competition edition
(fixed Competition + Season + Tournament) — its group games plus every knockout
tie it has reached, ordered by date. A Run is scoped **as of** a given Fixture:
only games on or before that Fixture's date, so a Run reflects the story *up to*
a match and never leaks a later result. "Ever-present" describes a player who
**started** (Squad Entry `started`) every Fixture in the Run — a property of the
Run, not of the player. A Run has no bracket topology: which knockout tie feeds a
Team's next round is **not** stored, so a Team's next opponent is only knowable
once a later Fixture already names both teams; until then the next round is known
but the rival is not.
_Avoid_: campaign, journey; extending a Run across editions (a Run is one Season);
including Fixtures after the analysed match in an as-of Run; inferring a next-round
opponent from bracket position (there is none — see the later Fixture or nothing).

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
Two `player_id`/`assist_id` conventions read **backwards** unless you know them:
for a **subst** Event, `player_id` is the player going **OFF** and `assist_id` is
the player coming **ON** (verified against Squad-Entry `status`: the `player_id`
side is overwhelmingly `started`, the `assist_id` side `came_on`) — the opposite of
the "player + assist" framing. For a **Goal → Own Goal** Event, `player_id` is the
scorer but the goal counts for the **opposing** Team, so a per-Team tally that
groups goals by the Event's `team_id` credits the wrong side.
_Avoid_: incident, moment; conflating an Event with a Squad Entry's stat totals;
counting a shootout kick as a Goal Event; reading a subst's `player_id` as the
player who came on; crediting an Own Goal to the scorer's Team.

**Narrated Match**:
A match as **ESPN** publishes it, identified by ESPN's own **game id** — the unit a
**Commentary Line** belongs to, and the only identity a Commentary Line has.
A Narrated Match is **not a Fixture**. A Fixture is an API-Football record of a
match in a **Competition** — one we collect — and *most Narrated Matches have no
Fixture at all*, because ESPN narrates competitions we do not collect (Swedish
Allsvenskan, NWSL, LigaPro Ecuador, Argentine Nacional B). Absence of a Fixture is
therefore the normal case, not missing data.
Where a Narrated Match *is* also a Fixture, the two may be **linked** — but the
link is optional, hand-supplied, and **verified before it is stored**: the Fixture's
kickoff and team names must agree with ESPN's, and an unverifiable link is refused
rather than guessed. Kickoffs are compared in UTC within a **15-minute tolerance**,
because ESPN rounds to the hour where API-Football keeps the broadcast minute
(`03:00Z` against `03:05` is one Liga MX match, not two); a kickoff that agrees only
*within* the tolerance must additionally be **anchored** by at least one exactly
agreeing team name, which `--force-link` cannot waive (ADR 0030). The Fixture id is
a **bridge, never the key**.
_Avoid_: fixture, game, match; assuming a Narrated Match has a Fixture (most do
not); keying a Narrated Match on a Fixture id; reading an absent Fixture link as a
collection failure (it means ESPN narrates a competition we do not collect);
calling ESPN's league a **Competition** (a Competition is one we collect).

**Commentary Line**:
One narrated line of a **Narrated Match's** play-by-play, as published by **ESPN** — a
different provider from the API-Football source of everything else here. It
carries the minute it happened, the **Team** it is attributed to (which may be
none), a **Category**, and the narration text itself. It carries **no Player**:
players are named inside the text, but are not modelled — a Commentary Line is
about an occurrence, not the people in it.
A Commentary Line is **not an Event**, and the distinction is the whole point of
the term. An Event is the API-Football `fixtures/events` timeline and covers only
goals, cards, substitutions and VAR. A Commentary Line also covers fouls, corners,
offsides and attempts — occurrences that are *not* Events and have no Event
counterpart. Where the two *do* describe the same occurrence (a goal), they are
**two records of one occurrence from two providers**, and they may disagree: the
two feeds are collected at different times, so one can be Final while the other is
still in play.
Attribution follows the occurrence, not the sentence: for "Corner, France.
Conceded by Pau Cubarsí." the Team is **France** (whose corner it is), not the
conceding side. An **own goal** Commentary Line is attributed to the Team that
**benefits**, mirroring the Event rule.
_Avoid_: event, incident, moment, play (an Event is a different, API-Football
thing); assuming a Commentary Line exists for every Event (an `events_only`
**Narration Coverage** narrates only goals, cards and substitutions, and some
matches publish no narration at all); reading a Commentary Line's Team as the team
named first in the text; expecting a Commentary Line to identify a Player (it names
them only inside its text).

**Narration Coverage**:
How much of a **Narrated Match** ESPN actually narrates — the **Commentary Line**
analogue of **Coverage**, but per *Narrated Match* rather than per Season, and about
a different provider. Neither value is exotic: the two are roughly **half the feed each**.
- **narrative** — the full play-by-play: fouls, corners, offsides and attempts
  alongside the goals and cards (~110 lines for a 90-minute match). Only the
  notable subset arrives typed, so most Categories here are **inferred**.
- **events_only** — goals, cards and substitutions and nothing else (~15 lines),
  every line arriving **already typed** by the provider. So every Category is
  **asserted** and none is ever inferred.
This is the difference between *not narrated* and *did not happen*, and it must be
consulted before aggregating: an `events_only` Narrated Match reports zero fouls
because fouls are never narrated there, not because none were committed. Any
per-match rate over a narrative Category (fouls, corners, attempts) is silently
wrong if it mixes the two.
An events_only Narrated Match is often said to be the **Event** timeline restated —
but that comparison only exists where the match is *also* a **Fixture**, which is
the minority case. Where it is not, there is no Event timeline for it to duplicate
and the Commentary Lines are the only record we hold.
_Avoid_: reading a missing Category as an absent occurrence; averaging a narrative
Category across both kinds; conflating this with **Coverage** (that is
API-Football, per Season, about which *classes of data* exist at all).

**Category**:
The standardized bucket a **Commentary Line** falls into — `goal`, `foul`,
`corner`, `offside`, `attempt_saved`, `yellow_card`, `own_goal`, and so on. It is
**ours**, not a provider field: ESPN types only the notable subset of lines, so a
Category is either **asserted** (mapped from ESPN's own type — authoritative) or
**inferred** (assigned by a language model reading the text). Every Commentary
Line records which of the two it was, because they carry different confidence and
only the asserted ones can ever be checked against the provider.
_Avoid_: treating a Category as a provider fact; equating a Category with an
Event's type/detail pair (they are different vocabularies over different sets of
occurrences); trusting an inferred Category as heavily as an asserted one.

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
For a national-team Competition the stint at the tournament's own Season holds the
**national** team, not a club — a World Cup player's Season-2026 stint is "Spain,"
and the **club** they arrived with sits at the latest *earlier* Season (the
2025/26 club season, labelled 2025). So "current club" resolves to the most recent
non-national, non-youth stint at or before the tournament Season, never the
tournament Season itself.
_Avoid_: treating a Career Stint as an Appearance (it carries no per-match stats
and is not scoped to a Fixture); assuming its seasons align with our fixture data;
expecting a stint to tell you which league or country the team belongs to; reading
the tournament-Season stint as the player's club (it is the national team).

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

**Live Poll**:
The repeated fetch, on a fixed interval, of an **in-play** Fixture's events and
header (score, `status.short`, elapsed) while it is being played, writing them to
the **Live Mirror**. A Live Poll is the deliberate opposite of the cache-first
collector: it hits the provider live every cycle and *overwrites* the fixture's rows
each time. It watches a Fixture until that Fixture reaches **Final** (or another
terminal status), then stops — the authoritative per-match record is still the
**Refresh** into the main store, never the poll. A Live Poll collects only what is
in the events + fixtures payloads (ADR 0020); it does not fetch lineups or stats.
_Avoid_: treating a Live Poll's output as authoritative or final; polling a Fixture
past Final; confusing it with a Refresh (which collects once, after Final).

**Live Mirror**:
The standalone `live/live.db` a **Live Poll** writes — the *current best-known*,
overwrite-on-poll state of the fixtures currently being watched. Its `event` and
`fixture` columns **mirror** the main store's (same models), so a reader built for
the main store points at it unchanged; it adds a `polled_at`/`status` freshness
marker. Its data is **provisional**: revisable until Final (a Yellow Card can become
a Red, a Goal can be VAR-cancelled between polls), superseded by the authoritative
`world-cup.db` once the Refresh has collected the Final Fixture. Like `refresh.db`,
it is a side store the main rebuild never touches. Squad, lineups and team stats are
absent (out of a Live Poll's scope), so a reader must degrade gracefully on them.
_Avoid_: reading the Live Mirror as authoritative or complete; expecting squad/stats
in it; keeping a poll's row once the Refresh has written the Final record.

**Published Store**:
The **remote Postgres** replica of a chosen subset of Competitions **plus the whole
commentary store** — the only store that is not a local SQLite file, and the one other
tools and people query over the network rather than by opening a file on this box. It is
**derived, never authored**, kept current two ways (ADR 0028): a fast **delta publish**
(the default `refresh_pg` path) applies only the Finals whose data actually changed —
new or Coverage-re-healed — and a **wholesale** rebuild-and-swap (`publish_pg`, the
manual reset) replaces the whole thing. Either way a re-run makes it match what we hold
locally. Its Competitions are one **multi-Competition** build, not a concatenation of
per-Competition stores — **Venue** ids once collided because they were enumerated per
build (two separately scoped stores each number their stadiums from 1), so they are now
assigned from the committed append-only **Venue registry** that gives the
**same stadium the same id in every store** and lets the additive delta reference
existing venues without renumbering (ADR 0028, amending ADR 0027). That union is also
what lets a player appearing in two of its Competitions be one Player with one career,
rather than two unrelated rows.
Its two halves are scoped differently, and this is the thing to know about it: the
Competitions are a **chosen subset**, while the **Narrated Matches** and **Commentary
Lines** are always **all of them** — a Narrated Match is ESPN's unit and is not
Competition-scoped at all. So a Narrated Match's **Fixture** bridge may be present and
still resolve to nothing here, when that Fixture's Competition was not published; the
narration is no less real for it.
Because it replicates a *subset* of Competitions, it is complete for the ones it names
and simply silent about every other — a Competition it does not hold is out of its
scope, not missing data.
_Avoid_: mirror (a **Live Mirror** is provisional and per-poll; a Published Store is a
faithful replica); warehouse (it is schema-identical to the stores it replicates, not
dimensionally modelled); export, dump (it is re-runnable, not one-shot); confusing it
with the serving store (`serve.db` is a *window* over every Competition for the Viewer;
a Published Store is *every* row of *some* Competitions for an open-ended SQL consumer);
reading a Competition's absence from it as a collection gap; reading a dangling Fixture
bridge as a broken link (the bridge is optional and never a foreign key); expecting its
commentary half to follow its Competition selection (it never does — it is always copied
whole, on a delta run too); assuming it is only ever replaced wholesale (the default
intraday path is now an additive **delta** of changed Finals — ADR 0028); expecting a
provider-retracted Final to disappear on a delta run (only a wholesale `publish_pg`
removes retracted rows — an accepted blind spot of the additive path).

**Editorial Store**:
The PocketBase instance holding **Publications**, **Match Posts** and their
**Narratives** — the only store here that is **authored rather than derived**, and so
the only one with no rebuild path: there is no re-parse that reconstructs it and no
raw cache behind it. Losing it loses the writing.
It is also the only **co-tenanted** store: the same instance serves a personal site
whose `posts`, `pages`, `projects`, `profile` and `users` collections are nothing to
do with football. The pipeline never reads or writes those, but they share one
`pb_data`, so a restore or a loss takes both down together — and the name collision on
`posts` is why ours is `match_post`.
_Avoid_: blog store, CMS; **Published Store** (that is the remote Postgres replica —
derived, re-runnable, and about data rather than writing); treating its co-tenants as
ours; assuming a rebuild exists.

**Publication**:
A Competition we have decided to cover on the blog, together with how it renders
there: a URL **slug**, a display name, a **default language** (`es`/`en`, which picks
the system prompt the model is given), a **display timezone** (which fixes the local
date in every **Match Post**'s slug and every kickoff we print), a brand colour, and
any per-Competition **prompt overrides**. It joins to a Competition by that
Competition's provider league id, and carries its own **published** gate — false
until a human flips it, so a Competition can be drafted against long before anything
about it is public.
A Publication is *not* a Competition: it is our editorial decision to cover one, and
the two have separate lifecycles. Dropping a Publication stops the blog covering that
Competition and changes nothing about what we collect.
_Avoid_: league (a Publication may cover a **cup** — the World Cup is one of the three
published Competitions, and this is exactly the confusion the bare word was banned to
prevent); competition (that is the upstream thing a Publication points at); reading
its `published` flag as saying anything about collection or about the **Published
Store** (it gates the public site alone).

**Match Post**:
The blog's unit of work: exactly one per **Fixture**, identified by that Fixture's id
and by nothing else. It carries the **Narrative**, a URL slug composed from both teams
and the kickoff date *in its Publication's timezone*, an SEO title and description, an
author, and a **status** of `draft` or `published` with the publication timestamp that
goes with it. The drafter only ever writes `draft`; moving a Match Post to `published`
is a manual, human act, and the only one.
_Avoid_: post (the PocketBase instance also serves a personal site with its own,
entirely unrelated `posts` collection — hence `match_post`); article; treating the slug
as its identity (the Fixture id is — the slug is derived and could change).

**Narrative**:
The prose body of a **Match Post** — the match report itself, in its Publication's
language, drafted by the model from the Fixture's Events, Squad Entries, Team Match
Stats and **Commentary Lines**, and then edited by hand before it is published.
It is the one thing in this project that **cannot be regenerated**. Every other store
falls out of a re-parse of the raw cache, and even a Commentary Line's *inferred*
**Category** can at least be re-inferred at a price. An edited Narrative cannot: a
re-run produces different prose and silently loses the edit. That is why redrafting a
*published* Match Post is refused rather than merely warned about.
_Avoid_: crónica (that is only its Spanish rendering — a Publication may be `en`);
copy, article, content; treating it as derived data (it is **authored**, and it is the
only authored thing here).
