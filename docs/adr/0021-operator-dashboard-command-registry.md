# Operator dashboard: a command registry and subprocess triggers

*Relates to ADR 0009 (league orchestrator), ADR 0010 (cups), ADR 0018 (nightly
Refresh), and ADR 0020 (Live Poll). First entry of the "Football 2.0" line of work.*

The project had grown to ~10 terminal entrypoints — `football.orchestrate`,
`football.cups`, `football.collect`, `football.collect_events`,
`football.collect_stats`, `football.parse`, `football.scope`, `football.teams`,
`refresh`, `live.poll` — plus a shared library core (`client`, `config`, `models`,
`parse`) that has no CLI at all, and several marimo notebooks. The roles (collect,
build, refresh/cron, live) were clear in the author's head but invisible in the
layout: everything was a flat set of `python -m` modules with the operational intent
undocumented. We wanted (a) that structure made legible, and (b) a local UI to fire
these jobs and see, at a glance, which competitions are tracked and what plays this
week — without giving up the terminal.

The tension: the package is **tightly coupled** (`orchestrate._collect`,
`collect.fetch_*`, `parse.build()` are imported across cups/refresh/teams/scope), and
several jobs are **long-running** — `refresh` sweeps ~39 competitions against the API
quota, the backfills run for a day, and `live.poll` is an infinite loop by design.
A physical reorg into role folders would break every import, every ADR path
reference, the README, and the cron command; running a day-long or infinite job
inside a web request would hang or crash the server.

**Decisions:**

- **Organize by registry, not by moving files.** No module moves. A new
  `football/commands.py` is the single documented source of truth: each command's
  `name`, `group` (Collect / Build / Refresh / Live), a human `description` of what it
  accomplishes, the exact `python -m …` argv, and its typed `params`. The
  "clear structure" the reorg was meant to deliver lives here — as documented intent —
  rather than in a folder tree. We explicitly rejected both the physical role-folder
  reorg and a `[project.scripts]` console-script layer: the coupling and the path/cron
  churn made the payoff not worth it for a one-person tool, and the registry gives the
  UI a richer surface (descriptions, param specs) than a bare list of script names.

- **Triggers are background subprocesses of the existing commands.** The UI spawns the
  same `python -m …` argv the registry records, as a child process, streams its log,
  reports running/done/failed, and can stop it. The terminal workflow is unchanged
  byte-for-byte — the UI is a *second* front door to the same commands, never a
  reimplementation. We rejected in-process calls (a day-long backfill or the infinite
  Live Poll would block the server; a `QuotaExceeded` would take the worker down) and a
  Celery/RQ+Redis queue (a broker and a worker daemon are overkill for a local,
  single-user dashboard).

- **FastAPI + Jinja, bound to `127.0.0.1`.** The app can spawn processes and spend paid
  API quota, so it is never network-exposed. `fastapi` was already a dependency.

- **One DB-builder at a time; Live Poll exempt.** Every collect/build/refresh command
  drops and rebuilds `football.db`, so the UI refuses to start a second builder while
  one holds the existing `data/*.build.lock`. `live.poll` writes the separate
  `live/live.db` (ADR 0020) and is therefore always allowed to run alongside.

- **Read-only panels straight from the store, no new collection.** The tracked
  leagues/cups panel reads the `competition` table (name/type/country/flag/continent,
  ADR 0015/0016), grouped by continent. The "this week" table reads `fixture` joined to
  `competition` for canonical names, filtered to a rolling 7-day window and converted
  from stored **UTC** to `America/New_York` — the client passes no `timezone` param, so
  provider datetimes are UTC. Both reflect the last **Refresh**, not live state, and the
  UI says so.

- **No new domain vocabulary.** "Dashboard", "trigger", "command registry" are
  implementation, not domain language, so `CONTEXT.md` is untouched — it stays a pure
  domain glossary.

- **Amendment (2026-07-14): the registry is the card's documentation, not just its argv.**
  Each `Command` now also carries a concrete `example` (a "run it now" scenario written in
  the `CONTEXT.md` glossary's own terms — Squad Entry vs Event vs Team Match Stat), a `scope`
  one-liner (what it touches: "one league · every layer" vs "all competitions · events only"),
  and every advanced `Param` carries effect-framed `help` (what changes if you use it). The
  card renders, top to bottom: a `scope` cue tag, the exact `python -m <module>` command
  (copyable), an always-visible description, the `example` callout, then the form with a real
  disclosure for advanced options. This is what disambiguates the look-alike backfills
  (`collect` vs `collect_events` vs `collect_stats`) at the point of use — the orchestrators
  go *wide* (one competition, every layer) while the backfills go *tall* (one layer, every
  competition), and `scope` makes that grid legible. It keeps the registry — not the template — the single
  documented surface: adding a command still means one entry here, now with its example too.

**Consequences:**

- `commands.py` duplicates the entrypoint list that already exists implicitly across
  the modules' `__main__` blocks. That duplication is deliberate — it is the price of
  documenting role + description + params in one place — but a new command must be
  registered there to appear in the UI (the terminal needs no such step).

- The dashboard is a convenience layer with no persistence of its own beyond run logs;
  it holds no state the terminal path doesn't. Deleting `football/ui/` and
  `commands.py` removes the UI and changes nothing about how the pipeline runs.

- Deferred, each its own step: auto-discovering the live slate to pre-fill the Live
  Poll form (`fixtures?live=all`, cf. ADR 0020); scheduling/enabling the cron Refresh
  from the UI; and promoting the registry to real role-folders or `[project.scripts]`
  if the flat layout ever stops paying its way.
