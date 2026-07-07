# La Liga history starts at 2015/16 — the per-player-stats floor

We backfill La Liga to 2015/16 (`season=2015`) and no earlier, even though the
provider *catalogs* La Liga fixtures back to 2010/11.

The pipeline depends on **per-player match statistics** (`fixtures/players`:
goals, assists, minutes, who played). That data does not exist for older seasons.
Verified empirically, not from the (unreliable) coverage flags:

- 2013/14 and 2014/15: `fixtures/players` returns **0 teams with data**.
- 2015/16: returns full per-player data (2 teams per fixture).

So 2015/16 is the earliest season that supports the goals/assists-per-player
model. Earlier seasons would yield fixtures, final scores, and lineups only —
not the per-player output that is the point of the project — so they are
excluded rather than stored as a degraded, half-populated dataset.

Liga MX is left at 2024/25 onward (its own coverage was not investigated further;
extend `config.COMPETITIONS` if needed).
