# Collect coverage-light leagues; unify league and cup collection

`football.orchestrate` refused any league whose provider record exposed **no**
season with per-player stats: `_coverage_seasons` filtered to seasons with
`statistics_players`, and `main` hard-exited ("no seasons with per-player stats in
the requested range") when that set was empty. Liga MX Femenil (id 673) is exactly
that case — `statistics_players` **and** `statistics_fixtures` are false for every
season — so the tool would collect nothing, prompting "do we need a new
orchestrator for women's sports?"

The answer is no. A women's league is not a new kind of Competition; it is an
ordinary **league** that happens to have thin **Coverage** (CONTEXT.md). Fixtures
and Events exist for it regardless — only the per-player aggregates (`SquadEntry`,
bios, careers) and per-team match aggregates (`TeamMatchStat`) are coverage-gated.
And the cup path (ADR 0010) *already* models exactly this: `cups._collect_cup`
collects every season's fixtures + events and gates the per-player stages per
season on `statistics_players`. Leagues should adopt that model, not grow a third
near-duplicate collector.

**Decisions:**

- **A league collects like a cup: all seasons' fixtures + events, aggregates gated
  per season.** `orchestrate` no longer drops stats-less seasons or exits when none
  are covered. Every provider season's fixtures and events are collected; the
  per-player stages (squad/goals → bios → careers) run only for seasons with
  `statistics_players`, and team match stats run only for seasons with
  `statistics_fixtures`. A league with neither flag anywhere (Liga MX Femenil) lands
  as a valid **fixtures + events-only** Competition. "Stats-light" is a Coverage
  state, not a Competition type — there is no women's-sports orchestrator.

- **One coverage-gated collector, shared by both entrypoints.** Under the decision
  above, `_coverage_seasons`/`_collect_league` and `cups._cup_seasons`/`_collect_cup`
  become identical, so they collapse into `orchestrate._seasons` and
  `orchestrate._collect`. `cups.py` keeps only what is genuinely cup-specific: the
  `type == 'cup'` entry guard and `comp_type="cup"` registration (it already imports
  `_banner`/`_bar`/`_lookup`/`_resolve_name`/`_register` from orchestrate, so the
  dependency direction is established). Both entrypoints exercise the one collection
  path, so a regression in either surfaces immediately.

- **Coverage is carried as a named tuple, `SeasonCoverage(year, has_player_stats,
  has_fixture_stats)`.** The season list the collector iterates is
  `list[SeasonCoverage]`, replacing the cup path's positional `(year, has_stats)`
  tuple. Team match stats are gated on `has_fixture_stats` — the same discipline the
  per-player stages apply with `has_player_stats` — rather than run for every
  fixture, so a stats-light season spends **zero** wasted `fixtures/statistics`
  calls against the 150k/day cap (previously the cup path ran team stats
  unconditionally).

- **Parse is unchanged.** `parse._rebuild` already tolerates a fixture with no
  cached squad/bio/career/event/stat data — the cache-only client raises
  `QuotaExceeded` on a miss and each guarded stage catches it, leaving the Fixture
  row standing with no `SquadEntry` (parse.py, ADR 0010). A fixtures+events-only
  league is therefore a valid store the existing build produces with no change.

- **Normal new leagues now also collect their pre-coverage seasons as
  fixtures-only.** Because the stats floor is gone, orchestrating e.g. Ligue 1 now
  pulls fixtures + events for its early seasons that lack player stats, where before
  it collected nothing pre-coverage. Accepted deliberately: a league's full fixture
  history is a feature, per-season gating means no player-stats call is ever wasted
  on a season that lacks them, and the extra fixtures/events calls are cheap. The
  built-in seven are untouched — they carry hand-curated season floors in
  `config.py` and are never routed through `orchestrate`.

## Considered Options

- **A separate women's-sports / stats-light orchestrator.** Rejected: the trigger
  (Liga MX Femenil) is not about women's football — it is about Coverage, a
  per-Season provider fact that men's lower divisions and old seasons share. A third
  collector would duplicate the cup path a second time and encode an irrelevant
  distinction.

- **Fallback floor — filter to stats-covered seasons normally, but collect all
  seasons only when none are covered.** Rejected: a "usually filter, sometimes
  don't" branch is magic, and it keeps leagues and cups on two different mental
  models when the whole point is to unify them.

- **An explicit `--stats-light` / `--no-player-stats` opt-in flag.** Rejected: you
  would have to *know* a league is stats-light before the tool would collect it,
  which defeats the "just give me the id" contract of `orchestrate` (ADR 0009).

- **Keep team stats ungated (run for every fixture), matching the old cup path.**
  Rejected: for a large fixtures-only league that burns one empty `fixtures/statistics`
  call per fixture against the daily cap, for no data. Gating on `statistics_fixtures`
  costs one extra boolean on `SeasonCoverage` and mirrors the per-player gate.
