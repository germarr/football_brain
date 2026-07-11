# `refresh/` — the nightly Refresh job

The recurring job that keeps the store current. Every other collector
(`football.orchestrate`, `football.cups`, `football.collect`) is a **one-shot
backfill** — point it at a Competition, it fills the raw cache once, then stops.
`refresh` is the only thing meant to run **again and again**, unattended, from cron.

Each night it re-collects **only the mutable frontier** of the data — the current
season of every Competition — and leaves the immutable past untouched. It then rebuilds
`data/football.db` and re-scopes any `data/<slug>.db`, logs what changed, and records
the run. Design rationale is in [`docs/adr/0018`](../docs/adr/0018-nightly-refresh-ledger-and-run-history.md);
the **Refresh** and **Final** terms are defined in [`CONTEXT.md`](../CONTEXT.md).

---

## What's in this folder

| Path | Tracked in git? | What it is |
|---|---|---|
| `core.py` | ✅ yes | The job itself — all the logic. |
| `__main__.py` / `__init__.py` | ✅ yes | Make `python -m refresh` runnable. |
| `README.md` | ✅ yes | This file. |
| `refresh.db` | ❌ ignored | **Run history** — one row per night + per-endpoint call breakdown. Created on first run. |
| `refresh_ledger.json` | ❌ ignored | The **ledger**: every fixture already collected while Final. Created on first run. |
| `logs/refresh-YYYY-MM-DD.txt` | ❌ ignored | One human-readable log per run. |

The three runtime files are `.gitignore`d — they're local operational state, not source.
They live **here, beside the code**, deliberately kept out of `data/`: the nightly
`parse.build()` drops and rebuilds everything under `data/`, so run history stored there
would be wiped. Keeping it in `refresh/` is what lets it survive the rebuild.

---

## Running it

From the repo root (`/home/azureuser/alt_data`):

```bash
uv run python -m refresh                 # full run: collect → rebuild football.db → re-scope → log
uv run python -m refresh --scope-only    # collect → re-scope only; skip the full football.db rebuild
uv run python -m refresh --no-rebuild    # collect + log only; skip the DB rebuild entirely
```

That's it — no arguments. It always targets **every** Competition's current season.

The full `football.db` rebuild dominates a run: collecting the frontier is seconds (the
plan is un-throttled), but rebuilding the ~370 MB store is **~13 min**. The two skip flags
(mutually exclusive) trade that off:

- `--scope-only` — the **interactive fast path**. Collects the frontier and re-scopes every
  existing `data/<slug>.db` straight from cache (~seconds each), but skips `parse.build()`.
  Use it when you're at the keyboard and only read a scoped DB (e.g. `world-cup.db`).
  `football.db` is left **stale** until the next full run, which self-heals it (the new
  Finals are already ledgered and cached). See ADR 0018.
- `--no-rebuild` — collect + log only, skip both rebuild and re-scope. For inspecting the
  log/ledger without touching any DB.

**Exit code:** `0` on a clean night, non-zero if any Competition errored or the API quota
ran out mid-run (so cron/`MAILTO` notices). A partial run still rebuilds and logs what it
got — the cache is always internally consistent, so there's no half-collected state to
protect against.

---

## Installing it as a nightly cron job

`uv` usually isn't on cron's `PATH`, so call the venv's Python directly. `flock -n` skips
a run if the previous one is still going (or if you kick one off by hand). Leaving
**stderr unredirected** means a clean night is silent but a failure emails you via `MAILTO`.

Run `crontab -e` and add:

```cron
MAILTO=me@gmarr.com
0 4 * * * cd /home/azureuser/alt_data && flock -n /tmp/football-refresh.lock .venv/bin/python -m refresh >> refresh/logs/cron.out 2>>refresh/logs/cron.err
```

- **04:00 server time** — late enough that the day's matches have gone Final.
- Test the exact command first, without the rebuild:
  ```bash
  cd /home/azureuser/alt_data && flock -n /tmp/football-refresh.lock .venv/bin/python -m refresh --no-rebuild
  ```
- Confirm it's installed with `crontab -l`.

> **First real run is heavy.** On an empty ledger, *every* current-season Final across all
> ~20 Competitions is "new", so it heals every legacy stale-empty and pulls bios/careers
> for anyone still missing — La Liga alone was ~360 live calls; expect a few thousand total,
> and it will be slow. Every night after that is cheap (only genuinely new matches). Either
> let the first cron run absorb it overnight, or run it by hand once first so you can watch it.

---

## What it does each night (the model)

The raw client is **cache-first**: a fixture, once fetched, is fetched once ever. That's
correct for the immutable past but actively *hides* new matches. Refresh deliberately
re-fetches only the two things cache-first would otherwise freeze, per Competition:

1. **The `/leagues` record** (force-refetched) — refreshes provider metadata *and* exposes
   the provider's latest available season (this is what powers the new-season warning below).
2. **The current season's fixture list** (force-refetched) — otherwise a match played since
   the list was first cached would never surface.

From that fresh list it collects per-fixture data (squad/goals, events, team stats —
Coverage-gated) for **Final** fixtures — status `FT` / `AET` / `PEN` — that aren't already
in the **ledger**. Non-Final fixtures (scheduled, live, `PST`, `CANC`, …) are skipped and
revisited for free on later nights until they become Final. A fixture the old backfill
cached **empty** while it was still scheduled gets **healed** (force-refetched) on the first
night it's Final. It then runs the full enrichment chain (bios → careers → Team Profiles)
for the players and teams those new Finals surfaced — cache-first, so already-seen ones cost
nothing. Finally it rebuilds `football.db`, re-scopes existing `data/<slug>.db` files, writes
the log, and records the run.

---

## The runtime files

### `refresh_ledger.json` — what's been collected
A flat `fixture_id → {status, collected}` map spanning every Competition. A fixture is
written **only after all its per-fixture stages succeed**, so an interrupted run re-does a
half-collected match rather than skipping it. This is the gate that makes Refresh
incremental *and* defeats the stale-empty trap.

- **Safe to delete?** Yes, but don't casually. Deleting it makes the next run re-collect
  every current-season Final from scratch (heavy, but harmless — cache-first dedupes).
  It's the fast path, not a source of truth; the raw cache is.

### `refresh.db` — run history
SQLite, two tables. Survives the nightly `football.db` rebuild because it's outside `data/`.

- `refresh_run` — one row per night: `date, started, finished, duration_s, competitions,
  fixtures_collected, teams_updated, live_calls, cache_hits, outcome`.
- `refresh_call` — `run_id, competition, endpoint, calls` — the per-Competition,
  per-endpoint live-call breakdown for each run.

Inspect it any time:
```bash
sqlite3 refresh/refresh.db "SELECT date, competitions, fixtures_collected, live_calls, outcome FROM refresh_run ORDER BY id DESC LIMIT 14;"
sqlite3 refresh/refresh.db "SELECT competition, endpoint, calls FROM refresh_call WHERE run_id=(SELECT max(id) FROM refresh_run) ORDER BY calls DESC;"
```
`outcome` is `ok` (clean), `interrupted` (hit the API quota — resume just happens next
night), or `failed` (a Competition threw; see the log / cron email).

### `logs/refresh-YYYY-MM-DD.txt` — the nightly report
Summary header (counts, live/cache totals, duration, outcome), any new-season warnings,
then per Competition: **Updated** teams (each new Final with opponent, date, score) and
**Unchanged** teams. A club playing several Competitions is reported per Competition.

---

## ⚠ "NEW SEASON AVAILABLE" — what it means and what to do

Sometime around the start of a season you'll see this at the top of the log (and the run
summary):

```
⚠ NEW SEASON AVAILABLE: La Liga 2026 — add to config (currently pinned to 2025)
```

**What it means.** Refresh targets `max(seasons)` from each Competition's config. The parser
only models seasons that are in config. So when the provider opens a season your config
doesn't list yet, that season would be **collected into cache but never appear in
`football.db`** — a silent gap. Rollover is deliberately **not automatic** (a cron job
should not rewrite your source and start pulling a possibly preseason/friendly-only season
with no human in the loop), so Refresh warns instead of acting.

**Nothing is broken and nothing is lost** while the warning stands — Refresh keeps
correctly refreshing the *old* current season (2025 here). You just aren't collecting the
new one yet. Act when you're ready for the new season to start flowing in.

### What you do

Every Competition — league *and* cup — lives in the single competitions file
[`football/competitions.json`](../football/competitions.json) (ADR 0019); there is no longer
a built-in/registered split. Find the Competition by `league_id` and append the new year to
its `"seasons"` list (it's an explicit list of integers, so just add the value):

```json
// before — max is 2025
{ "league_id": 140, "name": "La Liga", "seasons": [2015, ..., 2025], ... }
// after — now includes 2026
{ "league_id": 140, "name": "La Liga", "seasons": [2015, ..., 2025, 2026], ... }
```

Then collect it. Either is fine:

```bash
uv run python -m refresh                        # next run picks 2026 as current, backfills its Finals so far, rebuilds
uv run python -m football.orchestrate 140       # heavier one-shot backfill of the whole new season, then rebuild
uv run python -m football.cups <id>             # for a cup, use the cups collector instead
```

Use `orchestrate` (or `cups`) if you want the new season's already-played matches pulled in
one go right now; otherwise the next nightly Refresh does it incrementally. Either way the
warning stops once `max(seasons)` matches the provider.

> **Heads-up on season numbering.** The season integer is the *provider's* own. Straddling
> leagues (La Liga, Premier League, …) label `2025` as "2025/26"; calendar-year leagues
> (Brasileirão, Liga MX Femenil, …) label `2026` as "2026". The warning prints the provider's
> integer — use exactly that value in the config.

---

## Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| Log says `outcome: interrupted` | API quota hit mid-run. Ledger saved partial progress; the rest collects next night. No action needed unless it recurs — then raise the plan or lower the schedule frequency. |
| Log says `outcome: failed` | One Competition threw (see cron email / the `[error]` line). The run still rebuilt and logged the rest. Investigate that Competition. |
| A new match isn't showing up | Only **Final** (`FT`/`AET`/`PEN`) fixtures are collected. A `PST`/`CANC`/live match is intentionally skipped until it's Final. |
| Cron didn't run / no email | Check `crontab -l`, that `.venv/bin/python` exists, and `refresh/logs/cron.err`. Remember `flock -n` silently skips if a previous run is still holding the lock. |
| Want to force a full re-collect | Delete `refresh/refresh_ledger.json` and run `python -m refresh`. Heavy but safe. |
