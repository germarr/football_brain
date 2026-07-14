# Split the operator dashboard into a Console + a Viewer over a published serving DB

*Relates to ADR 0021 (operator dashboard + command registry), ADR 0022 (Match
Tracker page), ADR 0020 (Live Poll / Live Mirror), ADR 0018 (nightly Refresh), and
ADR 0011 (per-competition database / scoping). Second entry of the "Football 2.0"
line of work.*

ADR 0021 built one FastAPI app that did three unrelated jobs at once: it **fired the
pipeline** (Collect/Build/Refresh/Live subprocesses that rebuild `data/football.db`
and spend API quota), and it **rendered reader panels** (tracked leagues/cups, this
week's fixtures) by reading `football.db` **live**, and ADR 0022 added a per-match
**Match Tracker** on top. Two worlds — a pipeline control plane and a reader surface —
share one process, one port, and one database dependency.

That coupling has real costs. `football.db` is the historic **source of truth**: a
388 MB store that `parse.build()` **drops and rebuilds wholesale** (~13 min) on every
collect/refresh. A reader surface pointed straight at it degrades for that whole
window (the app already carries graceful-empty fallbacks for exactly this), competes
for the file while it is being rewritten, and — the deeper problem — makes the store
that should change **once a day, deliberately** into a live read-dependency of a web
UI. We want `football.db` written by the pipeline and read by nothing user-facing;
and we want the reader surface fast and always-available, independent of whatever the
pipeline is doing.

**Decisions:**

- **Two apps, split by concern.** The single app becomes two, each its own package,
  process, and port, both bound to `127.0.0.1`:
  - the **Operator Console** (`football/ui/`, the ADR 0021 app *stripped back*) — its
    only job is to run the scripts that build `football.db`;
  - the **Viewer** (new `web/` package) — the reader surface: tracked leagues/cups,
    games of the week, and the per-match view. It reads **only** its own stores, never
    `football.db`.
  We rejected keeping one process with two routers (`/console`, `/`) as separation in
  name only — the point is that the two worlds no longer share a process or a database
  handle.

- **A new, dedicated serving database `web/serve.db`.** The Viewer reads a
  purpose-built store, not `football.db`. We explicitly rejected reusing either
  existing side store: `refresh/refresh.db` is *run history* (no domain data), and
  `live/live.db` is the **provisional** Live Mirror (a per-poll-overwritten snapshot of
  a handful of watched fixtures, ADR 0020) — wrong shape and wrong lifecycle to be the
  durable serving copy of every tracked league and the full week. `serve.db` lives
  beside them under `web/`, `.gitignore`d like the others (local operational state,
  re-derivable from `football.db`, not source).

- **`serve.db` is schema-identical to `football.db`, a windowed slice.** It reuses the
  `football.models` tables verbatim — the same choice ADR 0020 made for `live.db`, and
  for the same payoff: the Viewer's per-match reader is the ADR 0022 code with **one
  line changed** (the authoritative fallback is `serve.db`, not `football.db`), and the
  `live.db` → `serve.db` precedence keeps its exact shape (Mirror wins while a
  `livepoll` row exists, else the authoritative serving copy). We rejected a
  denormalised render-ready schema: the only join it would remove is against
  `competition` (39 rows — effectively free), so it buys nothing and forks the reader
  from the Match Tracker's. The **speed** win comes from `serve.db` being *small and
  never mid-rebuild*, not from denormalisation. Its contents:
  - **every `competition`** (all ~39 tracked rows) and **every `player` + `playerteam`**
    (career history) — these are the *small* tables in `football.db` (the 388 MB is
    almost all `squadentry`/`event` history), so cloning them whole is cheap and makes
    any player clickable;
  - **`fixture` + `event` for a rolling window only** — roughly **−3 to +10 days**
    (recent results + the upcoming slate). The Viewer filters that superset down to
    "this week" at request time; the ±buffer gives both edges data. Full history stays
    only in `football.db`.

- **A `web.publish` step bridges the two worlds; nothing else reads `football.db`.**
  Populating `serve.db` is a **zero-API DB→DB copy**: open `football.db` read-only,
  `INSERT` the windowed slice into a fresh `serve.db.tmp`, then **atomically rename** it
  over `serve.db` so a reader never sees a half-written store. This is distinct from
  `scope.py` (which re-parses a competition from the *raw cache*, ADR 0011) — publish
  never re-parses and never hits the API; it clones from the already-built authoritative
  store. It lives in the `web/` package (`python -m web.publish`), so the serving world
  owns its own build and `refresh/` stays purely about `football.db`. The result:
  `football.db` has exactly **two** consumers — the pipeline that *writes* it, and
  `web.publish` that *reads* it — and **no web UI reads it live**.

- **The nightly job gains a publish tail, and is actually installed.** The recurring
  chain is `refresh` (rebuild `football.db`) → re-scope any `data/<slug>.db` (ADR 0018)
  → **`web.publish`** (refresh `serve.db`). So `serve.db` is exactly as current as
  `football.db`, once a day. The ADR 0018 cron was documented but **never installed**
  (the crontab held only unrelated jobs); installing `refresh → web.publish` at 04:00 is
  part of this change, not a pre-existing given.

- **The Console shrinks to triggers + logs; the Live group leaves it.** Command groups:
  **Collect / Build / Refresh** (the football.db builders) **+ Publish** (`web.publish`,
  the explicit handoff). The **Live group is removed** — `live.poll` launching is not a
  football.db concern and now lives only on the Viewer's match page (below). The
  Console's form options come from **`competitions.json`** (config, ADR 0019), not
  `football.db`, so the Console needs **zero** `football.db` reads; the ADR 0021
  leagues/week panels move out of it entirely. The `.build.lock` flock guard stays.

- **The Viewer keeps the live features unchanged, and stays localhost-only.** Per-match
  live tracking (ADR 0022) moves to the Viewer intact: it reads `live.db` (provisional)
  over `serve.db` (authoritative-for-the-Viewer), and **launches / stops `live.poll`**
  from the match page. `live.db` counts as one of the Viewer's *own* stores, so "reads
  only the new database" holds — it still never touches `football.db`. Because
  `live.poll` spends API quota, the Viewer remains bound to `127.0.0.1`, same as ADR
  0021. `live.poll` is the **sole** subprocess the Viewer may spawn; every
  football.db-touching trigger is Console-only. A truly public, read-only Viewer would
  mean gating/removing the live-launch controls — deferred.

- **No new domain vocabulary; `CONTEXT.md` untouched.** "Console", "Viewer", "serving
  database", "publish" are implementation, not domain language — consistent with ADR
  0020/0021/0022 keeping UI terms out of the glossary. The domain nouns (Competition,
  Season, Fixture, Event, Player) are unchanged; this ADR reshapes *where they are
  read*, not what they mean.

**Consequences:**

- The Match Tracker routes (`/fixture/{id}`, `/state`, `/live`, `/clear`) and the
  reader helpers (`_read_fixture`, `_event_text`, `_fmt_clock`, `_event_icon`, the
  precedence logic) **move from `football/ui/app.py` into `web/`**. `football/ui/app.py`
  loses all `/fixture/*` routes and the live-DB code, keeping only trigger spawning,
  job logs/SSE, and the build-lock guard. `commands.py` drops the Live group and gains a
  Publish entry.

- Two things now run instead of one — the Console (e.g. `:8000`) and the Viewer (e.g.
  `:8001`), plus the nightly cron. Operationally heavier, but each is single-purpose and
  the Viewer is available even while a build or refresh is in flight.

- `serve.db` can lag reality by up to a day (it is a daily publish), and its window is
  fixed at publish time — "games of the week" is computed from the −3..+10 superset
  captured at 04:00, which is stable for the day. An in-play score/timeline for a match
  the operator is actively watching still comes live via `live.poll` → `live.db`, exactly
  as before; a match **not** being polled shows its last-published state until the next
  publish.

- The Viewer's per-match view can still show **provisional** data that later disagrees
  with the authoritative record (ADR 0020/0022) — unchanged. The one new staleness risk
  is symmetric to ADR 0022's: a `live.db` row can shadow the newly-published `serve.db`
  copy after a Final; the existing **Clear** control still reverts it.

- Deferred, each its own step: a public read-only Viewer (gate/remove the live-launch
  controls, then it can leave localhost); per-player pages exploiting the cloned
  `player`/`playerteam` (the data is now in `serve.db`, the pages are not built);
  publishing `serve.db` on a different cadence than `football.db` if their freshness
  should ever diverge; and auto-clearing a `live.db` row once publish has written the
  Final (retiring the manual Clear), the same deferral ADR 0022 named.
