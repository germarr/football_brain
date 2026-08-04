# `live/` — the Live Poll and its provisional Live Mirror

Watch an **in-progress** match's timeline update, minute by minute, without
disturbing the rest of the pipeline. Every other collector in this project
(`football.onboard.orchestrate`, `football.onboard.cups`, the `football.collect.*` backfills, and the nightly
`refresh`) is **cache-first**: a fixture's per-match data is fetched **once ever**
and then frozen. `live/` is the deliberate **opposite** — it hits the provider
**live every cycle** and *overwrites* what it fetched last time.

Design rationale is in [`docs/adr/0020`](../docs/adr/0020-live-poll-provisional-mirror.md);
the **Live Poll**, **Live Mirror**, **provisional** and **Final** terms are defined in
[`CONTEXT.md`](../CONTEXT.md).

---

## The problem it solves

The main store already shows a live **score** for an in-progress match — that number
rides on the `fixtures?league&season` list payload, which the nightly **Refresh**
force-refreshes (ADR 0018). But a fixture's **events, squad and team stats do not
update until the match is Final.** They are cache-first, per-fixture calls: fetched
once, and if they were fetched while the match was still unplayed they cached **empty**
and are only *healed* by a later Refresh — which by design collects a fixture's
per-match data exactly once it is **Final** (`FT`/`AET`/`PEN`).

So during a live match the store has a moving score but an **empty timeline**. A
throwaway spike ([`spike_events.py`](spike_events.py)) confirmed the provider *does*
serve in-play data — `fixtures/events?fixture=<id>` returns the timeline live, revised
as the match goes (we watched a second yellow become a red). The Live Poll is how we
consume that stream into a readable DB **without** touching the cache-first collector or
the authoritative Final record.

---

## What's in this folder

| Path | Tracked in git? | What it is |
|---|---|---|
| `poll.py` | ✅ yes | **The Live Poll itself** — the repeated fetch-and-overwrite loop. This is the durable code. |
| `spike_events.py` | ✅ yes | A **throwaway probe** kept only as documentation of the live API's shape (see below). Not part of the pipeline. |
| `__init__.py` | ✅ yes | Makes `python -m live.poll` runnable. |
| `README.md` | ✅ yes | This file. |
| `live.db` | ❌ ignored | **The Live Mirror** — the provisional in-play DB. Created on first poll, overwritten every cycle, regenerable by re-polling. `.gitignore`d as local runtime state. |

Like `refresh/refresh.db`, `live.db` lives **here, beside the code**, deliberately kept
out of `data/`: the nightly `parse.build()` drops and rebuilds everything under `data/`,
so a DB stored there would be wiped. Keeping it in `live/` is what lets it survive — and
stay independent of — the main rebuild.

---

## Running it

From the repo root (`/home/azureuser/alt_data`):

```bash
uv run python -m live.poll 1582681                    # watch one fixture, 60s cycle, until Final
uv run python -m live.poll 1582681 1582682 --interval 30   # watch several, faster cycle
uv run python -m live.poll 1582681 --once             # one cycle then exit (testing)
```

It takes **explicit fixture ids** — the matches you want to watch. The poll runs an
**unbounded loop** until every watched fixture reaches a terminal status, then exits on
its own (`All watched fixtures finished. Live Poll exiting.`).

You can also trigger it from the **Operator Console** (`console`): the *Live* group
has a **Live-poll a match** card whose form is pre-filled with this week's fixtures
(registered in [`football/commands.py`](../football/commands.py) as `module="live.poll"`).
The dashboard spawns exactly the same `python -m live.poll …` command as a background job
with a Stop button.

- `--interval N` — seconds between cycles (default **60**). Events land every minute or
  two, so 60s feels live at trivial cost against the 150k/day quota.
- `--once` — run a single cycle and exit, for testing the pipeline without an open-ended loop.

Because the poll spends live API quota, run it only while a match you care about is
actually in play. It is **not** wired to cron and there is no auto-discovery of the live
slate — an operator names the fixtures.

---

## What each poll cycle does (the model)

For every fixture still being watched, one cycle does exactly this — see
[`_poll_once`](poll.py):

1. **Fetch the fixture header** — `GET /fixtures?id=<id>` for the live score, `status.short`
   and elapsed minute. This is a **direct `requests` call** (`_fetch`) that **bypasses the
   cache-first client entirely** — no cache read, no cache write. That is the whole point:
   the collector must never see this live, revisable data.
2. **Overwrite the `fixture` row** — the header is parsed with `parse._parse_fixture` (the
   *same* helper the main store uses) and `session.merge`d. An empty venue map is passed, so
   `venue_id` is left `null`.
3. **Fetch and wholesale-replace the events** — `GET /fixtures/events?fixture=<id>` returns
   the **full timeline array** each time, so the poll `DELETE`s all of that fixture's `event`
   rows and re-inserts the fresh set (shootout kicks filtered via `parse._is_shootout_kick`,
   each parsed with `parse._parse_event`). This is why an in-play event is **revisable** — a
   yellow can become a red, a goal can be VAR-cancelled, and the next cycle simply reflects it.
4. **Upsert a minimal `player(id, name)`** — pulled from the **names already inside the events
   payload** (scorers and assisters), so the timeline reads with names at **zero extra API
   calls**. Only `id` and `name` are set; every biographical column stays `null`.
5. **Stamp a `livepoll` freshness row** — `fixture_id`, `polled_at` (UTC now), and the current
   `status`, so a reader can tell *how stale* the snapshot is and *what's live right now*.

Everything above is committed per fixture, per cycle. A fixture is **dropped from the watch
set** once its status is **Final** (`FT`/`AET`/`PEN`) or otherwise **terminal** (`PST`/`CANC`/
`ABD`/`AWD`/`WO` — abandoned/postponed matches never go Final). When the set empties, the loop
exits.

> **Scope v1 = events + fixture header only.** Lineups, team statistics and per-player match
> stats are deliberately out of scope (ADR 0020) — one events call + one fixtures call per
> fixture per cycle, nothing more.

---

## What `live.db` hosts (the Live Mirror)

`live.db` is created with `SQLModel.metadata.create_all`, which materialises the **entire**
`football.models` schema — so structurally it is a **carbon copy of `world-cup.db` /
`football.db`**. But the poll only ever *writes* four of those tables:

| Table | Written by the poll? | Contents in the Mirror |
|---|---|---|
| `fixture` | ✅ | One row per watched fixture — live score, `status`, teams, competition. `venue_id` is `null`. |
| `event` | ✅ | The live timeline, **replaced wholesale every cycle**. Goals, cards, subs, VAR — minute, type, detail, player, assist. |
| `player` | ✅ (partial) | `id` + `name` only, harvested from the events payload. Bio columns (`nationality`, `birth_*`, `height_cm`, …) are `null`. |
| `livepoll` | ✅ | Freshness marker: `fixture_id` (PK), `polled_at`, `status`. **Mirror-only** — it does not exist in the main store. |
| `competition`, `team`, `venue`, `teamprofile`, `playerteam`, `squadentry`, `teammatchstat` | ❌ | **Present but empty.** Created by the shared schema, never populated by a poll. |

Two properties define the Mirror:

- **It is provisional.** It holds the *current best-known* state of the watched fixtures,
  overwritten every poll. It is **superseded** by `world-cup.db` / `football.db` once the
  nightly Refresh collects the now-Final fixture. Never read it as authoritative or complete.
- **It keeps the last snapshot at Final.** When a match ends the poll simply *stops* — it does
  **not** prune. The just-finished match stays readable in the Mirror during the window before
  the Refresh writes the authoritative copy. Re-polling an id overwrites it; there is no
  automatic cleanup beyond that.

The columns of `fixture` and `event` **match the main store exactly** because they come from
the same models and the same parse helpers — that identity is what makes the Mirror useful (next
section).

---

## How this connects to the rest of the football project

```
                          cache-first, once-ever                    live, every 60s
  API-Football  ──►  football.onboard.* / football.collect.*  ──►  data/football.db  (authoritative)
        │                        + nightly `refresh` (heals Finals)   data/<slug>.db
        │
        └──────────────────►  live.poll  ──►  live/live.db  (provisional, in-play only)
                                   ▲                 │
                       reuses football.models        └──►  read by notebooks/match_story.py
                       + parse._parse_fixture/_event        (set live=True)
```

- **Schema-compatible by construction.** `poll.py` imports `football.models` (`Event`,
  `Fixture`, `Player`) and `football.build.parse`'s `_parse_fixture` / `_parse_event` /
  `_is_shootout_kick`. So a reader built for the main store points at `live.db` **unchanged** —
  same table names, same columns, same parsing rules.
- **`match_story.py` drops straight onto it.** The [`match_story`](../notebooks/match_story.py)
  marimo notebook has a `live` toggle: set `live=True` and its `db_path` switches from
  `data/world-cup.db` to `live/live.db`. The **headline and timeline light up for an
  in-progress match**; the squad, team-stats and Man-of-the-Match sections have no data in the
  Mirror and **degrade gracefully** (ADR 0020 added the empty-squad guards that make this safe).
- **A side store, like `refresh/`.** `live.db` is never touched by `parse.build()` or the scoped
  rebuild — those only operate under `data/`. It is independent operational state.
- **The authoritative record is always the Refresh.** The store's live *score* rides the
  fixtures-list payload that Refresh force-refreshes; the Live Poll fills in the live *timeline*
  that would otherwise stay empty until Final. Once the nightly Refresh (ADR 0018) collects the
  Final fixture into `world-cup.db` / `football.db`, **that** is the source of truth. The Mirror
  can legitimately **disagree** with it (a VAR reversal between the last poll and Final) — that
  is the accepted nature of provisional data, not a bug.
- **Same provider and key.** `_fetch` uses `config.BASE_URL`, `config.KEY_HEADER` and
  `config.load_api_key()` — the identical credentials as every other collector, just without the
  caching layer.

---

## `spike_events.py` — the throwaway probe

Before designing `live.db` we needed to confirm the provider actually serves in-play data.
`spike_events.py` hits `/fixtures` and `/fixtures/events` for one fixture **live** and prints
the current status + timeline — no cache, no DB, just a print:

```bash
uv run python -m live.spike_events 1582681
```

It is **not part of the durable pipeline** — kept only as executable documentation of the live
API's shape. `poll.py` is what productionised what the spike proved.

---

## Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| Timeline is empty / not updating | Confirm the fixture is actually **in play** (a scheduled match has no events yet) and that you passed the right fixture id. The poll prints `status=… events=N` each cycle. |
| Poll exits immediately | The fixture was already **Final/terminal** when you started — the Mirror keeps its last snapshot and the watch set empties at once. Expected. |
| Squad / match-stats / MOTM sections are blank in `match_story` | By design — those are **out of a Live Poll's scope** (ADR 0020). Read the Mirror expecting only headline + timeline. |
| Mirror disagrees with `world-cup.db` after the match | Provisional data was revised between the last poll and Final (e.g. a VAR reversal). The **Refresh into the main store is authoritative**; the Mirror was a best-effort live view. |
| `live.db` has a leftover fixture from an old test | Harmless — it's overwritten when you re-poll that id. There is no auto-prune; delete `live/live.db` to start clean (it's regenerable). |
| Want the live *score* but not a timeline | The main store already tracks the live score via the Refresh-refreshed fixtures list; you only need a Live Poll for the live *events*. |

---

## Scope and what's deliberately deferred (ADR 0020)

- **No auto-discovery.** The poll is handed explicit fixture ids. Filtering the live slate via
  `fixtures?live=all` down to our Competitions is the natural next step, not built here.
- **No lineups / team / player stats** in the Mirror — events + header only, for now.
- **No append-only snapshot archive.** The Mirror is *current-state*, overwritten each poll; it
  cannot reconstruct "the timeline as it stood at minute 60". A per-poll archive is a larger,
  separate ambition, worthwhile only if in-play *drift* (xG, momentum) ever becomes the question.
- **Not wired to cron.** Unlike `refresh`, this is an at-the-keyboard tool you start for a match
  you're watching and that stops itself when the match ends.
