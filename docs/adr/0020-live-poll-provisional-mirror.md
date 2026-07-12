# Live Poll into a provisional Live Mirror (`live/live.db`)

*Relates to ADR 0007 (match-event timeline), ADR 0018 (nightly Refresh), and the
cache-first client of ADR 0002.*

The store shows a live **score** for an in-progress match — that number rides on
the `fixtures?league&season` list payload, which the nightly Refresh force-refreshes
— but its **events, squad and team stats do not update until the match is Final**.
Those are cache-first, per-fixture calls: fetched once ever, and if fetched while the
match was still unplayed they cache empty and are only *healed* by a later Refresh,
which by design collects a Fixture's per-match data exactly once it is **Final** (ADR
0018, CONTEXT.md: Final). So an in-play match has a score but an empty timeline until
the nightly Refresh runs after it ends.

A spike (`live/spike_events.py`) confirmed the provider *does* serve in-play data:
`fixtures/events?fixture=<id>` returns the timeline live, revised as the match goes
(we watched a second yellow become a red). We want to watch an in-progress match —
its timeline, live — without disturbing the cache-first collector or the authoritative
Final record.

**Decisions:**

- **A provisional Mirror, not a snapshot archive.** `live/live.db` holds the
  *current best-known* state of the watched fixtures, **overwritten every poll**. It
  is explicitly provisional — an in-play Event is revisable until Final — and is
  superseded by `world-cup.db` once the Refresh collects the Final Fixture. We did
  *not* build an append-only per-poll archive (reconstructing "the timeline at minute
  60"); that is a larger, separate ambition.

- **Scope v1 = events + fixture header only.** Each poll fetches `fixtures/events`
  (timeline) and the `fixtures` row (live score, `status.short`, elapsed). Lineups,
  team statistics and per-player stats are deliberately out of scope for now.

- **Watches explicit fixture ids.** The poll is handed the ids to watch. Auto-discovery
  of the live slate via `fixtures?live=all` filtered to our Competitions is the natural
  next step but is not built here.

- **60s interval; stop at Final/terminal.** Events land every minute or two, so a 60s
  cycle feels live at trivial cost against the 150k/day cap. A fixture is polled until
  it reaches a Final status (`FT`/`AET`/`PEN`) or another terminal one (`PST`/`CANC`/
  `ABD`/`AWD`/`WO`); the loop exits when the watch set is empty.

- **The Mirror mirrors the main store, plus a freshness marker.** `live.db` reuses
  `football.models` and `parse.py`'s `_parse_fixture`/`_parse_event` (empty venue map →
  null `venue_id`), so its `event`/`fixture` columns match `world-cup.db` exactly — a
  reader built for the main store points at it unchanged. A small `livepoll` table adds
  `polled_at`/`status`. To keep the timeline legible without widening scope, a minimal
  `player(id, name)` is upserted from the **names already in the events payload** — no
  extra API call.

- **At Final, keep the last snapshot; the poll just stops.** No prune and no coupling
  to the Refresh: the just-finished match stays readable in the Mirror during the window
  before the nightly Refresh writes the authoritative copy. Re-polling an id overwrites
  it; a `--once`/manual clear covers cleanup.

- **A standalone side store under `live/`.** Like `refresh/refresh.db`, `live/live.db`
  is never touched by the main `football.db`/scoped rebuild. New vocabulary — **Live
  Poll**, **Live Mirror**, **provisional** — lands in the single `CONTEXT.md` (no
  `CONTEXT-MAP.md` split; `live/` is not a large enough bounded context).

**Consequences:**

- `football/notebooks/match_story.py` drops straight onto `live.db` by changing one
  `db_path`: the headline and timeline light up for an in-progress match. Squad, match
  stats and Man-of-the-Match sections have no data in the Mirror and now **degrade
  gracefully** — the tournament-run cell gained an empty-squad guard it lacked (it did
  `.iloc[0]` on an empty frame), a robustness fix that also helps any squad-less DB.

- The Mirror can disagree with the eventual authoritative record (a VAR reversal
  between the last poll and Final). That is the accepted nature of provisional data;
  the authoritative timeline remains the Refresh into `world-cup.db`.

- Not wired to cron or auto-discovery: an operator passes fixture ids. `live/spike_events.py`
  is a throwaway probe, kept only as documentation of the live API shape.

- Deferred, each its own step: auto-discovery via `fixtures?live=all`; lineups/team/
  player stats in the Mirror; and an append-only snapshot archive if in-play *drift*
  (xG, momentum) ever becomes the question.
