# Package by context, declare role in the registry

---
Status: accepted — amends ADR 0021 (operator command registry and its groups) and
ADR 0028 (which named `football/venues.json` by path). Builds on ADR 0011
(one package, one store), ADR 0019 (committed Competition registry) and ADR 0023
(Console / Viewer split).
---

`football/` had become a flat sixteen-module drawer holding four unrelated jobs —
fetching, registering, parsing, publishing — beside two committed JSON registries and
three subdirectories, one of which (`ui/`) is a whole separate application. The stated
goal was to separate "scripts that populate the frontend" from "data pipelines" from
"onboarding scripts".

That framing does not survive contact with the tree, and the reason is the crux of this
ADR. The repo already has **two** organising axes and both are documented:

- **context** — the folder tree today (`football/`, `commentary/`, `live/`, `refresh/`,
  `web/`), where each package owns exactly one store (ADR 0011/0020/0023/0026/0027);
- **role** — `GROUPS` in `football/commands.py` (ADR 0021), the operator-facing
  vocabulary the Console renders.

Role **cross-cuts** context. `commentary/` alone collects (`espn.py`), builds (`join`,
`classify`, `store`) and is republished wholesale into Postgres. Making role the folder
tree would split that one package across three directories and break the one-package-one-store
invariant that five ADRs lean on. Only one axis can be the tree.

**Decisions:**

- **Context stays the tree; role is declared only in the registry.** Packages remain
  per-context and keep owning one store apiece. `football/commands.py` plus a README
  role table become the single place a script's role is stated — so the third vocabulary
  the reorganization asked for is expressed as *data*, not as directories. A script's role
  can now be changed by editing one line, and no file has to be in two places at once.

- **`football/` splits into a kernel plus four role-named subpackages.** The kernel
  (`client`, `config`, `models`, `fetch`, `paths`, `commands`) is what everything imports;
  the subpackages are leaves nothing imports across.

  | was | becomes |
  |---|---|
  | `football/orchestrate.py` | `football/onboard/orchestrate.py` |
  | `football/cups.py` | `football/onboard/cups.py` |
  | `football/collect_events.py` | `football/collect/events.py` |
  | `football/collect_stats.py` | `football/collect/stats.py` |
  | `football/teams.py` | `football/collect/teams.py` |
  | `football/parse.py` | `football/build/parse.py` |
  | `football/scope.py` | `football/build/scope.py` |
  | `football/venues.py` | `football/build/venues.py` |
  | `football/publish_pg.py` | `football/publish/pg.py` |
  | `football/refresh_pg.py` | `football/publish/delta.py` |
  | `football/ui/` | `console/` |
  | `football/notebooks/` | `notebooks/` |
  | `football/context/` | `docs/reference/` |
  | `football/competitions.json` | `football/registry/competitions.json` |
  | `football/venues.json` | `football/registry/venues.json` |

  `commentary/`, `live/`, `refresh/`, `web/` and `football_blog/` are untouched — they
  are already one-context, one-store packages, and this ADR does not disturb what is
  already coherent.

- **`collect.py` is split: the kernel becomes `fetch.py`, its CLI is retired.** One file
  was doing two jobs — nine modules import it for cache-first fetch helpers (`parse`,
  `teams`, `scope`, `orchestrate`, `publish_pg`, `venues`, `collect_events`,
  `collect_stats`, `refresh.core`), while its *entrypoint* is a full-sweep backfill
  superseded by `orchestrate`. The importable half becomes `football/fetch.py` in the
  kernel; the `Full backfill` command is dropped from the registry. The name `collect`
  is then free to become the subpackage above without a module/package collision.

- **`GROUPS` becomes `Onboard, Backfill, Build, Refresh, Publish, Control`.** ADR 0021's
  `Collect` conflated two acts that fail differently, so it splits (see **Onboarding** in
  CONTEXT.md): `Onboard` admits an entity to a **Registry** so every later recurring job
  covers it; `Backfill` bulk-fetches Seasons into the raw cache, resumable and quota-bound,
  admitting nothing. `Control` is the Operator Console — and it is the one group with **no
  registry entry**, because the Console is what *renders* the registry and cannot invoke
  itself. It is named anyway so the role table is complete and the Console is not mistaken
  for a thing that populates something.

- **Registries get one home and one resolution anchor.** Both JSON registries move to
  `football/registry/`, and every path derives from `football/paths.py` rather than each
  module's own `__file__`. This is not tidying — it is the only reason the move is safe.
  `venues.py` resolved its registry as `Path(__file__).resolve().parent / "venues.json"`
  and `_read()` returns `[]` when the file is absent, so moving the module without the data
  would have silently orphaned the committed 516 KB registry, re-fetched ~8k venues against
  a paid API, written a fresh registry at the new path, and left the nightly
  `git add football/venues.json` with nothing to commit. No error anywhere. Anchoring every
  registry path in one module decouples data layout from code layout permanently, so the
  next reorganization cannot re-open this failure.

- **Retirement is deletion, not an archive folder.** Git already holds the history, so an
  `archive/` directory only moves clutter. Deleted: `main.py` (a hello-world stub referenced
  nowhere), `football/initial-assesment.md` (a pre-project chat transcript about where to buy
  football data — not a decision record), `commentary/sample-760514.json` (unreferenced).
  Untracked from git but left on disk: `football/__marimo__/session/explore.py.json`, committed
  before `.gitignore` learned `__marimo__/`. Explicitly **kept**: `live/spike_events.py`, which
  `live/README.md` documents as "kept only as documentation of the live API's shape", and
  `commentary/synthesis-760514.json`, cited by ADR 0026 as a shape example.

## Consequences

- **Four places hold module paths, and one of them is outside git.** `commands.py` (eleven
  `module=` strings the Console turns into `python -m` argv), `web/app.py:94` (`[sys.executable,
  "-m", "live.poll", ...]`), fourteen of thirty-one ADRs in prose, and the installed crontab.
  The crontab is the dangerous one and is dealt with separately in ADR 0032; without that,
  this move would break the nightly job on the night it lands.

- **A stale `commands.py` module string fails silently in the worst way.** It is a *string*,
  so nothing resolves it until an operator clicks the button in the Console and gets a
  `No module named` traceback. This is one of the invariants the suite in ADR 0033 must cover.

- **Fourteen ADRs now cite module paths that no longer exist.** They are not rewritten —
  an ADR records what was decided when it was decided, and rewriting history to match the
  present destroys the record's value. This ADR is the forwarding table; the path map above
  is deliberately complete so any older path can be resolved against it.

- **`from . import collect` becomes `from . import fetch` in nine modules**, and
  `football.collect` changes meaning from a module to a package. Any external muscle memory
  for `python -m football.collect` breaks loudly, which is the acceptable direction.

## Considered Options

- **Role as the folder tree** (`onboarding/`, `pipelines/`, `frontend/`) — the literal
  reading of the request. Rejected because role cross-cuts context: `commentary/` would be
  split three ways, no package would own one store, and the ADRs that depend on that
  invariant would all become wrong at once.

- **Registries beside the module that reads them** (`build/venues.json` next to
  `build/venues.py`) — preserves the "a committed JSON array beside this module" idiom both
  docstrings state. Rejected because it splits the two registries across two directories and
  leaves resolution `__file__`-relative, so the *next* code move re-opens the silent-orphan
  failure.

- **Leave the registries at `football/` and only change resolution** — smallest diff, no
  516 KB file showing as a rename. Rejected for leaving two registries loose in a package
  root that is otherwise all subpackages, which is the incoherence this ADR exists to remove.

- **`archive/` instead of deleting** — rejected; git is the archive, and a directory of
  things nobody will read is exactly the clutter being removed.
