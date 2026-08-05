# Football Brain

Per-match player performance and biographical data across Europe's top leagues
and beyond, sourced from the [API-Football](https://www.api-football.com/) provider.

**Coverage** — La Liga, Premier League, Serie A, Bundesliga and Brasileirão
(2015/16 onward) plus Liga MX and Liga Profesional Argentina (2016/17 onward).
Argentina's format varies by season (single-table, phased, and playoff eras), so
its standings are per-phase rather than one clean league table — see
[ADR-0008](docs/adr/0008-argentina-primera-division.md). For each fixture we collect the
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
API-Football  ──onboard/──▶  data/raw/       ──build/──▶  data/football.db
 (network)     collect/       (raw JSON cache,             (modeled SQLite,
                               cache-first)                 disposable)
```

- **Layer 1 — raw cache** (`data/raw/`): every API response is cached to disk,
  keyed by endpoint + params. The network is touched only on a cache miss, so
  each fixture is fetched exactly once, ever. Re-running collection is free and
  resumes wherever a previous run stopped.
- **Layer 2 — SQLite** (`data/football.db`): a disposable store rebuilt from the
  cache with **no network access**. `build/parse.py` drops and recreates every
  table on each run, so you can change the schema and reparse without refetching.

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

### 1. Onboard a competition (network — costs API quota)

```bash
uv run python -m football.onboard.orchestrate 140   # a league
uv run python -m football.onboard.cups 2            # a cup
```

Registers the Competition in `football/registry/competitions.json` — after which
every later Refresh, parse and publish covers it without being told again — and
then collects it end to end: the league fixture list, each fixture's player data,
every unique player's biography and career history, the Event timeline and Team
Match Stats. All cache-first, so interrupt and re-run any time; cached responses
are never refetched, and the run stops cleanly when the daily quota is hit.

The old full-sweep `football.collect` entrypoint was retired by ADR 0031: it
predated this orchestrator, was not Coverage-aware, and collected neither events
nor team stats. Its shared fetch helpers live on as `football/fetch.py`.

### 1b. Collect the match-event timeline (network — separate run)

```bash
uv run python -m football.collect.events
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
uv run python -m football.build.parse
```

Rebuilds `data/football.db` from the raw cache and prints a summary (row counts
plus fixtures per competition / season / tournament). A cache miss fails loudly
rather than silently spending quota, so this is safe to run repeatedly.

### 3. Explore

An interactive [marimo](https://marimo.io/) notebook reads the SQLite store:

```bash
uv run marimo edit notebooks/explore.py   # editable notebook
uv run marimo run  notebooks/explore.py   # read-only app
```

Pick a Competition, Season, and Tournament (Liga MX splits into Apertura /
Clausura) to browse squads, appearances, per-90 stats, and standings. Once the
event timeline is backfilled, the **Match events** section adds goal-timing
distributions (by minute and type), most-booked players, and stoppage-time goals.

### 4. Operator dashboard (local UI — optional)

A local FastAPI + Jinja dashboard fires the same pipeline commands from a browser
and shows the tracked competitions plus this week's fixtures in NYC time
([ADR-0021](docs/adr/0021-operator-dashboard-command-registry.md)):

```bash
uv run python -m console           # then open http://127.0.0.1:8000
uv run python -m surfaces          # the Viewer at :8001/ and the Desk at :8001/desk
```

The Console keeps its own port because it writes `football.db` — the coupling
[ADR-0023](docs/adr/0023-split-viewer-app-and-serving-db.md) split apart. The Viewer and
the Desk share neither store nor rebuild window, so since
[ADR-0035](docs/adr/0035-one-port-two-surfaces.md) they answer on one port under one
header, composed by `surfaces/` without either package importing the other.

Bound to `127.0.0.1` only — it spawns subprocesses and spends API quota, so it is
never network-exposed. Every trigger runs the **exact** `python -m …` command you
would type, as a background subprocess with a live streaming log and a Stop button;
the terminal workflow is unchanged. A trigger that rebuilds `football.db` is refused
while another build is in progress (the same `*.build.lock` `parse` uses). The set of
triggers is defined in [`football/commands.py`](football/commands.py) — the documented
registry both the UI and this README read; add a command there to surface it in the UI.

## Roles

Folders are organised by **context** — each package owns exactly one store. A
script's **role** is declared in [`football/commands.py`](football/commands.py) and
here, never by where the file sits, because role cross-cuts context: `commentary/`
alone collects, builds and publishes ([ADR-0031](docs/adr/0031-package-by-context-role-in-the-registry.md)).

| Role | What it does | Where |
|---|---|---|
| **Onboard** | Admit an entity to a **Registry** so every later recurring job covers it. One-time, idempotent, forward-acting. | `football/onboard/`, `football_blog/onboard.py` |
| **Backfill** | Bulk-fetch Seasons into the raw cache. Resumable, quota-bound, admits nothing. | `football/collect/` |
| **Build** | Model the cache into a store. Cache-only — a miss raises rather than fetching. | `football/build/` |
| **Refresh** | Re-collect each Competition's current-season frontier nightly. | `refresh/`, `football_blog/candidates.py` |
| **Publish** | Derive a read surface: the Viewer's `serve.db`, the Postgres Published Store, the blog's Editorial Store. | `web/publish.py`, `football/publish/`, `football_blog/` |
| **Control** | Fire the pipeline. Populates nothing — the Console *renders* the registry, so it has no entry in it; the composed surfaces are a separate process and do have one. | `console/`, `surfaces/`, `football_blog/desk/` |

Onboard and Backfill were one group until ADR 0031. They split because they fail
differently: a backfill cut short resumes for free, while an entity that was never
onboarded is covered by nothing, however much of its data sits in the cache.

## Project layout

```
football/            the pipeline package — one package, one store (ADR 0011)
  paths.py           every path, resolved from one anchor  (ADR 0031)
  config.py          target leagues/seasons, API config, .env key loader
  client.py          cache-first API-Football client (Layer 1)
  fetch.py           the shared cache-first fetch helpers every stage uses
  models.py          SQLModel tables (Fixture, Player, SquadEntry, Event, …) + age_at
  commands.py        registry of triggerable commands (role, description, params)  (ADR 0021)
  registry/          the committed Registries — competitions.json, venues.json
  onboard/           admit a Competition to the registry, then collect it  (network)
  collect/           backfill events, team stats, team profiles  (network)
  build/             parse / scope / venues — cache to SQLite  (offline)
  publish/           pg (wholesale) + delta — the Postgres Published Store
console/         local FastAPI + Jinja Operator Console, :8000  (ADR 0021/0023)
surfaces/        mounts the Viewer at / and the Desk at /desk, :8001  (ADR 0035)
web/             the reader-facing Viewer, over its own serve.db  (ADR 0023)
commentary/      ESPN Commentary Store  (ADR 0026)
live/            provisional Live Mirror during a match  (ADR 0020)
refresh/         nightly frontier Refresh  (ADR 0018)
football_blog/   Editorial Store + match-report pipeline  (ADR 0029)
  desk/          the Desk — Drafting Candidates + prompt, at /desk  (ADR 0034)
notebooks/       marimo exploration notebooks (explore, ligamx, worldcup, …)
scripts/         nightly.sh (the cron's one entrypoint) + preflight  (ADR 0032/0033)
tests/           the silent-failure guards + a committed cache slice  (ADR 0033)
docs/adr/        architecture decision records
docs/reference/  background reference material
CONTEXT.md       domain glossary (Competition, Season, Tournament, Appearance…)
data/            raw cache + SQLite DB (git-ignored, regenerable)
```
