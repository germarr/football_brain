# Cache raw API responses on disk; build SQLite from the cache

API-Football's free plan caps us at **100 requests/day**, making refetching
costly and iteration painful. Every raw response is cached to disk under
`data/raw/<endpoint>/`, keyed by endpoint + params; the fetcher reads cache
before hitting the network, so each fixture is pulled **once, ever**. Re-running
parsing/modeling code then costs **zero** API calls.

The typed SQLite store (`Player` / `Fixture` / `SquadEntry`, via `sqlmodel`) is
built *from* the raw cache and is therefore disposable — it can be dropped and
rebuilt with no network access.

## Addendum — `data/raw` is a symlink to another volume

Recorded 2026-08-04, long after the fact, because it was written down nowhere and the
reorganization of ADR 0031 nearly stepped on it.

`data/raw` is **not a directory**. It is a symlink to `/stuffdata/alt_data/raw`, a
separate mount, because the cache has grown to roughly 8 GB while the root filesystem is
29 GB and currently 83% full. `config.RAW_DIR = ROOT / "data" / "raw"` resolves through
it transparently, so nothing in the code knows or needs to.

Two things follow, and both are the reason this is worth a paragraph:

- **The failure is silent and expensive.** If the symlink is broken, moved, or absent —
  by tidying `data/`, by cloning to a machine without `/stuffdata`, or by checking the
  repo out a second time — the cache-first fetcher simply misses every key and re-fetches.
  Nothing errors. The bill is the whole cache, against a paid plan.
- **The repo is not portable, and no amount of version control makes it so.** A second
  working tree has no cache, no `.env` and no `.venv`. This is why the reorganization was
  done in this checkout rather than a git worktree (ADR 0032), and why the preflight of
  ADR 0033 asserts `config.RAW_DIR` actually resolves before any run begins.

The "disposable, rebuild with no network access" claim above holds for the **SQLite
store**. It has never held for the raw cache, which is the one thing here that money and
time bought and no re-run reconstructs.

## Considered Options

- **Parse straight into the DB (no raw layer).** Rejected: every schema change or
  parser bug fix would re-spend the daily quota.
- **Postgres / parquet for the modeled store.** Rejected for now: single-team,
  single-season scope fits SQLite trivially; revisit if scope grows to full-league
  or multi-season.
