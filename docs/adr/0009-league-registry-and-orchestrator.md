# Register new leagues via a JSON registry, driven by an orchestration entrypoint

The seven collected Competitions are hand-listed in `config.COMPETITIONS`, each
with a canonical name and a per-league season floor curated by hand (2015 for the
European leagues, 2016 for Liga MX and Argentina — ADR 0006/0008). Adding an
eighth league (e.g. Ligue 1, id 61) meant editing that source list by hand and
knowing the right floor. We want a single terminal command —
`uv run python -m football.orchestrate 61` — to add a league as a first-class
Competition and collect it end to end, without hand-editing source or guessing
the floor.

**Decisions:**

- **A JSON registry, `data/competitions.json`, merged by `config.py`.** The
  built-in seven stay in `config.COMPETITIONS` as `_BUILTIN_COMPETITIONS`;
  `config` loads the registry at import and appends any registered league not
  already built in. Registration is therefore **data, not code** — the
  orchestrator never rewrites Python source. `config.reload_competitions()` lets a
  running process (the orchestrator) pick up a freshly written entry before it
  rebuilds the DB. A registry record is `{league_id, name, seasons,
  calendar_year}`.
- **Seasons are coverage-driven, not a hardcoded floor.** The orchestrator reads
  the provider's per-season `coverage.fixtures.statistics_players` flag and
  collects exactly the seasons that expose per-match player stats — the data that
  populates `SquadEntry`. This rule *provably regenerates the existing hand floors*
  (La Liga → 2015+, Liga MX → 2016+, Argentina → 2016+) and gets any future
  league's floor right automatically. `--from`/`--to` narrow it.
- **Canonical name defaults to the provider's, but is collision-guarded.** Per
  CONTEXT.md a Competition carries *our* canonical name. The orchestrator adopts
  the provider name by default and refuses to register it if it collides with an
  existing canonical name for a *different* league id (the "two Serie A's" trap);
  `--name` overrides.
- **One orchestrator drives all stages for one league**, reusing `collect`'s
  cache-first fetch helpers: fixtures → squad/goals (`fixtures/players`) → bios →
  career histories → events. It then runs `parse.build()` to rebuild
  `football.db` (which, being a drop-and-rebuild over all config targets, now
  includes the new league). Collection is cache-first and resumable; if the daily
  cap is hit mid-run the orchestrator stops **before** rebuilding and prints
  resume instructions, so the DB is never built from a half-collected league.
- **Terminal UX: a per-stage banner and a live loading bar.** Hand-rolled with
  `\r`, no new dependency (the repo deliberately avoids extras — see `config.py`
  on not using python-dotenv).

## Considered Options

- **Programmatically edit `config.py`'s `COMPETITIONS` list.** Rejected: textual
  insertion into hand-curated, commented source is brittle and easy to malform;
  the registry keeps curated defaults untouched.
- **Runtime-only injection (no persistence).** Rejected: `parse.build()` drops and
  rebuilds every table from `config.targets()`, so a league absent from config
  vanishes from the DB on the next parse. Registration must survive the process.
- **Hardcode a 2015 floor for every new league** (mirror the European leagues).
  Rejected: wrong for leagues whose player-stats coverage starts later; the
  coverage flag already encodes the correct floor per league.
- **Fold into `collect()`** (which loops all targets). Rejected: `collect()` is the
  full-backfill entrypoint; a single-league driver with progress output is a
  distinct concern, and reuses the same `collect.fetch_*` helpers anyway.
