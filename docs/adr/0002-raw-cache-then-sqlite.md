# Cache raw API responses on disk; build SQLite from the cache

API-Football's free plan caps us at **100 requests/day**, making refetching
costly and iteration painful. Every raw response is cached to disk under
`data/raw/<endpoint>/`, keyed by endpoint + params; the fetcher reads cache
before hitting the network, so each fixture is pulled **once, ever**. Re-running
parsing/modeling code then costs **zero** API calls.

The typed SQLite store (`Player` / `Fixture` / `SquadEntry`, via `sqlmodel`) is
built *from* the raw cache and is therefore disposable — it can be dropped and
rebuilt with no network access.

## Considered Options

- **Parse straight into the DB (no raw layer).** Rejected: every schema change or
  parser bug fix would re-spend the daily quota.
- **Postgres / parquet for the modeled store.** Rejected for now: single-team,
  single-season scope fits SQLite trivially; revisit if scope grows to full-league
  or multi-season.
