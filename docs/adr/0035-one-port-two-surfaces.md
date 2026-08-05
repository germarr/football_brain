# One port, two surfaces: composing the Viewer and the Desk without merging them

---
Status: accepted — **narrows ADR 0023**, which rejected "one process, two routers", and
**preserves ADR 0034**, whose tree argument this change would otherwise void. Builds on
ADR 0031 (context is the tree, role is declared in the registry) and ADR 0021 (the UI is
a second front door).
---

Three local surfaces answer on three ports: the Console on `:8000`, the Viewer on
`:8001`, the Desk on `:8002`. Two of them are looked at in the same sitting — you scan
the week's fixtures, notice a match finished, and go to draft it — and the handoff
between them is a second tab and a copied Fixture id. The Console is not in that loop;
it builds stores and is visited on purpose.

The friction is small and constant, and it has a second symptom: with three
single-purpose processes bound to localhost, *"is that one still up?"* becomes a
question you have to run `ss -ltnp` to answer.

So the Viewer and the Desk now answer on one port, under one header, at `/` and
`/desk`. What follows is mostly about why that is not the thing ADR 0023 refused.

**Decisions:**

- **This is composition, not the merge ADR 0023 rejected, and the difference is that
  there is no shared store.** 0023 said: *"We rejected keeping one process with two
  routers (`/console`, `/`) as separation in name only — the point is that the two worlds
  no longer share a process or a database handle."* The operative clause is the second
  one. That ADR's whole case was `football.db`: a 388 MB store that `parse.build()` drops
  and rebuilds wholesale, leaving a reader surface degraded for the ~13 minutes it is
  mid-rewrite. Sharing a process mattered because it meant sharing that handle.
  The Viewer and the Desk share nothing. The Viewer reads `serve.db` and `live.db`; the
  Desk reads the Editorial Store and the Published Store. No file, no connection, no
  rebuild window. Neither can degrade the other by being busy. What 0023 refused was
  separation in name only *where the coupling was the database*; here there is no
  database to couple, and the routers are all that would be shared.
  The Console stays out. It is the one surface that still writes `football.db`, and 0023's
  argument applies to it unchanged.

- **A fourth package, `surfaces/`, that neither existing package knows exists.** One port
  means one process means one import — and the naive direction is fatal. Had `web/` mounted
  the Desk, the package whose entire identity in ADR 0023 is *"reads only its own stores"*
  would import `football_blog`, and with it PocketBase and the Postgres loader. That is
  precisely what ADR 0034 forbade: *"Teaching either about the Editorial Store would spread
  a dependency on the one store with no rebuild path across three packages."* Mounting is
  teaching.
  So the import points the other way: `surfaces/` imports both apps, mounts them, and owns
  the header. `web` and `football_blog.desk` are unchanged in what they depend on, and
  0034's reasoning survives contact with this change rather than being quietly voided.
  `surfaces/` sits at the root beside `console/` and `web/` for the reason the README
  already gives for the Console: it reads no store and belongs to no context. It is named
  for the repo's own noun — *"a third local surface"*, *"three local surfaces that look
  like one tool"* — and deliberately not for an application, because CONTEXT.md says the
  Desk *"is the third of three local applications"* and there are still three. `shell/`
  was rejected: in a repo whose Console exists to spawn argv, that name reads as the thing
  that runs shell commands.

- **The Viewer keeps `/` and keeps `:8001`; the Desk moves to `/desk`.** Asymmetric on
  purpose. Every Viewer URL survives in path *and* port — `/fixture/1550918`,
  `/league/262`, `/week`, `/refresh-live` — so no bookmark and no deep link breaks, and
  the Viewer's templates need no path edits at all. Only the Desk's absolute paths take
  a prefix. Symmetric prefixes (`/week` + `/desk`, `/` redirecting) were rejected for
  moving every Match Tracker link to buy tidiness.
  The asymmetry is also true: the Viewer is what you leave open, the Desk is where you go
  to do a job. The four route collisions the two apps had — `POST /run` (the Viewer
  requires `key='live_poll'`, the Desk takes `fixture_id`), `POST /jobs/{id}/stop`,
  `GET /api/health`, `GET /jobs/{id}` — dissolve under the prefix rather than needing to
  be reconciled.

- **The header is injected into each app's Jinja search path, and included
  `ignore missing`.** A shared header rendered by both apps is, done naively, `web`
  importing the shell — the dependency just refused, arriving through the template loader.
  Instead `surfaces/` prepends its own template directory to each mounted app's loader at
  composition time, and each app's existing `<header>` gains
  `{% include "_nav.html" ignore missing %}`. Nothing is imported in either direction, and
  `ignore missing` is what keeps `python -m web` booting standalone with its header intact
  and no nav.
  The nav's active pill takes `var(--accent)`, so it is Viewer blue on one side and
  editorial teal on the other. That preserves something the Desk's `_base.html` set out to
  do — *"the accent differs … so a glance at a tab says which app you are in"* — which one
  tab would otherwise have silently destroyed. The affordance moves from the tab to the
  pill; it does not disappear.

- **The nav says "Viewer" and "Desk", and carries no state.** "This Week" was the obvious
  friendlier label and is wrong twice: it names the Viewer's first `<section>` rather than
  the app, and under this layout it would be the active item on `/league/262` and on every
  Match Tracker page. It would also be a *third* name for a surface CONTEXT.md, ADR 0023
  and its own header badge all call the Viewer.
  A Candidate count on the Desk item — `Desk (3)` — was rejected outright, not deferred.
  `_nav.html` is included by every Viewer page, so the count would put a PocketBase
  round-trip and a Published Store query on the render path of every match page, making the
  Viewer unavailable whenever the Editorial Store is. A surface whose ADR 0023 premise is
  that it stays *"available even while a build or refresh is in flight"* does not acquire a
  new upstream to save one click. If the itch returns, the answer is ADR 0034's own ESPN
  pattern — fetched after paint, never rendered with the page — and never a render-path read.

**Consequences:**

- **`python -m surfaces` on `:8001` is the front door; the two standalone launchers stay as
  debug entrypoints.** `python -m web` must keep booting alone or ADR 0023's deferred
  *"truly public, read-only Viewer"* becomes unreachable — that deferral requires a Viewer
  process with no Editorial Store in it, and `surfaces/` is not one. The cost is two ways to
  reach the Desk (`:8002` standalone, `:8001/desk`) and a stale `:8002` left running is a
  live confusion, one already hit in practice. Their `__main__` docstrings say debug-only;
  that is the whole mitigation, and it is weak.

- **`commands.py` gains its first `Control` entry, pointing at `surfaces` — and ADR 0034's
  version of this promise was never built.** 0034's consequences say *"Refresh Candidates
  likewise enters `commands.py` under **Refresh**, and the Desk itself enters under
  **Control**"*, and the README role table prints the Control row as though it were done.
  Neither entry exists: `GROUPS` declares `"Control"` and no command uses it. The registry
  entry added here launches the composed surface, which is now the true front door; the
  missing `candidates` entry under **Refresh** is 0034's debt and is tracked there.

- **Two `JOBS` registries and two `logs/` directories now live in one process, by design.**
  The Viewer's `live.poll` jobs and the Desk's pipeline jobs are not unified. Unifying them
  would be the merge this ADR declined; sharing a process is not a reason to share state.
  A blocking Editorial Store call cannot stall the Viewer: both apps' handlers are sync
  `def`, so uvicorn runs them in the threadpool.

- **CONTEXT.md is unchanged, and that is the correct outcome.** No domain vocabulary moves.
  "Viewer", "Desk", "surface" and "mount" are implementation, which is the line ADR 0023
  drew for exactly this class of change; and the Desk's glossary entry stays true, because
  there are still three local applications — two of them now answering on one port.

- **Two things here fail silently, which is what the tests target (ADR 0033).**
  `ignore missing` is a deliberate silent failure: rename or move `_nav.html`, or break the
  loader injection, and every page renders a header with no nav and no error anywhere — the
  merge is simply gone. And a Desk template that keeps an unprefixed absolute path works
  perfectly under `python -m football_blog.desk` and 404s only when mounted, so the
  standalone entrypoint that exists for debugging is the one place the bug hides.
