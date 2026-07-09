# Model cup tournaments (group + knockout) via Phase columns and a cup entrypoint

Every Competition collected so far is a domestic **league** — a single
round-robin whose Fixtures need only `tournament` + `matchday`. Cups like the
**Champions League** (id 2) and **World Cup** (id 1) don't fit: one Tournament,
one champion, but played out over a **group** (or single-table "League Stage")
phase and then a **knockout** bracket, preceded by **qualifying** rounds. We want
to collect and model these without disturbing the league data or the house style.

A probe of three real fixture lists (CL 2023 old groups, CL 2024 new league phase,
WC 2022) grounded the round vocabulary these decisions rely on.

**Decisions:**

- **Phase is a set of columns on `Fixture`, not a new table.** The store
  denormalizes on purpose — `Fixture` already inlines `league_name`, team names,
  `tournament`, `matchday`, `round`, with no `Tournament`/`Matchday` tables. A
  Phase and a Group are low-cardinality *labels*, not entities with a provider id,
  so they follow suit: `phase` (`qualifying` | `group` | `knockout`),
  `group_label` (`Group A`…`Group H` | null), `stage` (the knockout/qualifying
  round name | null). The raw `round` stays stored, so any misclassification is
  recoverable.

- **A Competition carries a `type` (`league` | `cup`), from the provider's
  `league.type`.** The parser must know which branch to take, and the provider
  already tells us (orchestrate.py already read `league.type`). For a **league**,
  behaviour is unchanged: `tournament` = the round prefix, phase columns null. For
  a **cup**, `tournament` = the Competition's canonical name (one Tournament per
  Season) and the phase columns carry the structure.

- **Three phase kinds, and `phase` is null for leagues.** `qualifying` is its own
  kind (not folded into `knockout`) so the ~90 pre-tournament fixtures per CL
  season are cleanly filterable and don't pollute the real bracket — and because
  bare `Play-offs` (qualifying) and `Knockout Round Play-offs` (the new CL's main
  knockout round) would otherwise collide. A league leaves all phase columns null
  rather than being called "a group phase," so nothing about existing rows or
  queries changes.

- **The group letter is only recorded when the provider exposes it.** Old CL uses
  `Group A - 3` (letter recoverable); the WC `Group Stage - 2` and new CL
  `League Stage - 5` are single/lettterless in the fixture, so `group_label` is
  null there. Recovering the WC group would need the standings endpoint — out of
  scope.

- **A separate entrypoint, `football.cups`, sharing the daily budget.** It mirrors
  `orchestrate.py` (cache-first, per-stage banner + bar, clean `QuotaExceeded`
  stop, rebuild only on a clean finish, registers into `data/competitions.json`)
  through the *same* `CachedClient`, so it simply consumes what's left of the
  150k/day cap. It differs in collection policy: it collects **every** provider
  season's fixtures and events (bounded by `--from`/`--to`), and runs the
  expensive per-player stages (squad/bios/careers) **only** for seasons whose
  `coverage.fixtures.statistics_players` flag is set. A cup therefore always yields
  its full bracket + event timeline, with player stats filled in where they exist.

## Considered Options

- **A `Phase` (or `Tie`) table.** Rejected: inconsistent with the denormalized
  schema; a Phase has no provider id and no attributes of its own, and two-legged
  ties can't even be reconstructed from the data (both legs share one round string
  with no leg indicator), so a `Tie` table would have nothing reliable to key on.

- **Fold qualifying into the knockout phase.** Rejected: it would mix
  pre-tournament rounds into "the knockout bracket," and the `Play-offs` vs
  `Knockout Round Play-offs` name clash makes the two genuinely different things.

- **Reuse `orchestrate.py`'s `statistics_players` season gate for cups.** Rejected:
  it selects only stats-covered seasons and `SystemExit`s when none exist, which
  would drop the fixtures + phase structure + events that are a cup's main value
  (and skip statless older cups entirely). Gating per *stage*, per *season* keeps
  the bracket while still not burning quota on empty `fixtures/players` responses.

- **Hardcode CL and WC.** Rejected: the model keys off the round string and
  `league.type`, so one generic path covers any group+knockout competition; CL and
  WC are just the two validation targets that exercise every case (old groups, new
  league phase, national teams, `3rd Place Final`).
