# Collect the full La Liga across seasons, on the Pro plan

The account moved from the Free plan to **Pro (7,500 requests/day**, and a far
higher per-minute cap). This removes the two constraints that shaped the original
scope:

1. The current season is now reachable — verified: `league=140 season=2025`
   returns 380 finished fixtures (2025-08-15 → 2026-05-24). Supersedes ADR-0001.
2. The daily budget comfortably covers the whole league. Collecting all 20 teams
   for both 2024/25 and 2025/26 is ~760 fixtures + ~760 fixture-player calls +
   ~1,200 player bios ≈ 2,000 requests — well under 7,500/day.

**Decisions:**

- **Scope is the whole league, not one team.** We store every player of both
  teams in every fixture; no team filter at collection time. FC Barcelona becomes
  merely the *default view* in the notebook, not a data boundary.
- **`season` is a list, not a constant.** `config.SEASONS = [2024, 2025]`.
  Collection and parsing iterate it, so adding a season is a one-line change.
  Every stored row already carries its `season`, so the store is multi-season by
  construction.
- **A `Team` dimension table is introduced.** With 20 teams referenced across
  ~30k squad entries, a normalized `Team(id, name)` is worth the join. It is
  derived from fixture payloads — **no extra API calls**.

## Considered Options

- **Keep per-team collection, loop teams.** Rejected: fetching fixtures per team
  double-counts every match (both teams request the same fixture) and complicates
  dedup. One league-wide `fixtures?league&season` call per season is cleaner.
- **Fetch bios per end-of-season squad (`/players?team`).** Cheaper (~40 calls)
  but misses mid-season transfers who appeared then left. We fetch bios per unique
  player id seen in any fixture — completeness over a few hundred saved calls,
  which the 7,500/day budget makes a non-issue.
