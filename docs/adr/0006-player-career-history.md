# Collect full player career history (players/teams)

We collect per-fixture Squad Entries scoped to our Competitions (La Liga, Liga MX),
which answers "how did this player perform in these leagues". It cannot answer
"which teams has this player ever played for" — a Barcelona or Real Madrid player's
spells at clubs and national teams outside our scope are simply absent. The
provider's `players/teams?player={id}` endpoint returns exactly that: every team a
player has appeared for across their whole career, each with the seasons the
provider has them there (e.g. Cristiano Ronaldo → Sporting CP, Manchester United,
Real Madrid, Juventus, Al-Nassr, Portugal).

**Decisions:**

- **Add a `PlayerTeam` table — the "Career Stint" (CONTEXT.md).** One row per
  `(player_id, team_id, season)`, mirroring the normalized, one-row-per-fact shape
  of `SquadEntry`. "All teams a player played for" is a `SELECT DISTINCT team_id`.
- **`team_id` is a global provider id, NOT a foreign key to `Team`.** Career
  history spans clubs and national teams outside our collected Competitions, which
  never enter the `Team` table (populated only from collected fixtures). We
  denormalize `team_name` onto the row instead — the same choice `Fixture` makes
  for its home/away team names.
- **Fetch one `players/teams` request per player already in our data.** The player
  set is the ~2.6k players seen in collected fixtures; collection is cache-first
  and resumable, so it runs as a final step of `collect()` and costs zero on
  re-runs. Parsing skips players whose history isn't cached yet (the same
  `QuotaExceeded → minimal/skip` pattern used for bios), so the backfill can
  complete over multiple days under the 7,500/day cap.

## Considered Options

- **Store each `(player, team)` with a JSON `seasons` list** instead of one row per
  season. Rejected: opaque to SQL — season filtering and cross-competition joins
  would need JSON extraction. One row per season keeps the store queryable like the
  rest of the schema.
- **FK `team_id` to `Team` and backfill every historical club into `Team`.**
  Rejected: pollutes the `Team` dimension (scoped to competitions we actually
  collect) with thousands of out-of-scope clubs and national teams we have no
  fixtures for. Denormalizing `team_name` keeps `Team` meaningful.
- **Only collect history on demand for specific players.** Rejected: inconsistent
  with the cache-first, whole-scope collection model; collecting for all known
  players makes career questions uniformly answerable and re-runs are free.
