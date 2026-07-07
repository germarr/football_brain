# Football Brain

Per-match player performance and biographical data across Europe's top leagues
and beyond, sourced from the [API-Football](https://www.api-football.com/) provider.

**Coverage** — La Liga, Premier League, Serie A, Bundesliga and Brasileirão
(2015/16 onward) plus Liga MX (2016/17 onward). For each fixture we collect the
full matchday squad of both teams (starters, substitutes who came on, and unused
subs), per-game stats (minutes, goals, assists, shots, passes, tackles, rating…),
and each player's biography (nationality, birth, height, weight).

On top of those per-player totals, the **match-event timeline** captures *when*
and *what kind*: every goal, card, substitution and VAR call with the minute it
happened, the goal type (normal / penalty / own goal), the assist behind each
goal, and added time. It comes from a separate endpoint and is collected on its
own run (see [ADR-0007](docs/adr/0007-match-events-timeline.md)).

See [`CONTEXT.md`](CONTEXT.md) for the project's domain language and
[`docs/adr/`](docs/adr) for the design decisions behind the scope and architecture.

## Architecture

The pipeline is two layers, so re-modelling the data never re-spends API quota:

```
API-Football  ──collect.py──▶  data/raw/       ──parse.py──▶  data/football.db
 (network)                     (raw JSON cache,               (modeled SQLite,
                                cache-first)                   disposable)
```

- **Layer 1 — raw cache** (`data/raw/`): every API response is cached to disk,
  keyed by endpoint + params. The network is touched only on a cache miss, so
  each fixture is fetched exactly once, ever. Re-running collection is free and
  resumes wherever a previous run stopped.
- **Layer 2 — SQLite** (`data/football.db`): a disposable store rebuilt from the
  cache with **no network access**. `parse.py` drops and recreates every table
  on each run, so you can change the schema and reparse without refetching.

Neither `data/` nor the `.env` secret is committed — both are regenerable /
private (see `.gitignore`).

## Setup

Requires **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/).

```bash
# 1. Clone
git clone git@github.com:germarr/football_brain.git
cd football_brain

# 2. Install dependencies into a local .venv
uv sync

# 3. Add your API-Football key to a project-root .env file
echo 'football_api=YOUR_API_KEY' > .env
```

The key is read directly from `.env` (`football_api=...`); no `python-dotenv`
dependency. Get a key from [api-football.com](https://www.api-football.com/) — a
paid plan is needed for the full historical range.

## Usage

### 1. Collect raw data (network — costs API quota)

```bash
uv run python -m football.collect
```

Fetches, per season in `football/config.py`, the league fixture list, each
fixture's player data, and every unique player's biography — all cache-first.
Interrupt and re-run any time; cached responses are never refetched. The run
stops cleanly when the provider's daily request quota is hit.

Edit the `COMPETITIONS` list in `football/config.py` to add/remove a league or
season. Player career histories (the `players/teams` endpoint, ~18.5k extra
calls) are deferred by default — enable with `COLLECT_CAREERS = True`.

### 1b. Collect the match-event timeline (network — separate run)

```bash
uv run python -m football.collect_events
```

Fetches one `fixtures/events` call per fixture (~20,780 calls — a full day under
the Ultra plan's cap), populating the goal/card/substitution/VAR timeline. It is a
**deliberately separate entrypoint** rather than part of `collect`, because it is a
day-long run in its own right ([ADR-0007](docs/adr/0007-match-events-timeline.md)).
Cache-first and resumable like `collect`: interrupt and re-run any time; the
provider's daily quota stops it cleanly. `parse` reads whatever events are cached,
so you can build the DB before the backfill finishes — those fixtures simply carry
no events yet.

### 2. Build the database (offline — no network)

```bash
uv run python -m football.parse
```

Rebuilds `data/football.db` from the raw cache and prints a summary (row counts
plus fixtures per competition / season / tournament). A cache miss fails loudly
rather than silently spending quota, so this is safe to run repeatedly.

### 3. Explore

An interactive [marimo](https://marimo.io/) notebook reads the SQLite store:

```bash
uv run marimo edit football/explore.py   # editable notebook
uv run marimo run  football/explore.py   # read-only app
```

Pick a Competition, Season, and Tournament (Liga MX splits into Apertura /
Clausura) to browse squads, appearances, per-90 stats, and standings. Once the
event timeline is backfilled, the **Match events** section adds goal-timing
distributions (by minute and type), most-booked players, and stoppage-time goals.

## Project layout

```
football/
  config.py        target leagues/seasons, API config, .env key loader
  client.py        cache-first API-Football client (Layer 1)
  collect.py       fetch fixtures, squads, bios, careers into data/raw/  (network)
  collect_events.py  backfill the match-event timeline — separate run  (network)
  parse.py         rebuild data/football.db from the cache  (offline)
  models.py        SQLModel tables (Fixture, Player, SquadEntry, Event, …) + age_at
  explore.py       marimo exploration notebook
docs/adr/       architecture decision records
CONTEXT.md      domain glossary (Competition, Season, Tournament, Appearance…)
data/           raw cache + SQLite DB (git-ignored, regenerable)
```
