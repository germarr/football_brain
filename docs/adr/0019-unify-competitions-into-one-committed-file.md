# Unify every Competition into one committed file, retiring the built-in/registry split

*Amends ADR 0009 (league registry) and ADR 0010 (cups in that registry).*

ADR 0009 kept the original seven leagues hand-listed in `config.COMPETITIONS`
(`_BUILTIN_COMPETITIONS`) and layered a JSON **registry** (`data/competitions.json`)
on top, merged at import by `_merge_competitions()`; ADR 0010 routed cups into that
same registry. The result is two sources of truth for one concept and a spray of
special-casing to reconcile them: `_BUILTIN_CALENDAR_YEAR`, `_load_registered`,
"a built-in always wins over a registry duplicate," and `_register`'s guard that
refuses to touch a built-in. It has also quietly rotted in a worse way — the registry
lives under `data/`, which is **git-ignored**, so **32 of the current 39 Competition
definitions (every cup among them) are not in version control at all**. A `rm -rf
data/` or a fresh checkout loses them. We want one place that defines every league and
cup, under git, with no built-in-vs-registry distinction anywhere in the code.

**Decisions:**

- **One committed source: `football/competitions.json`.** Every Competition — the
  former seven built-ins and the 32 registered ones — lives in a single JSON array
  beside `config.py`, the only module that reads or writes it. `_BUILTIN_COMPETITIONS`,
  `_BUILTIN_CALENDAR_YEAR`, `_load_registered`, and `_merge_competitions` are deleted
  and replaced by one `_load_competitions()`. The file moves **out of git-ignored
  `data/`** into the package so all 39 definitions are finally version-controlled; a
  one-time migration folds the seven built-ins in with their existing curated season
  ranges and calendar-year flags.

- **Loading fails loud on a missing or malformed file.** The old `_load_registered`
  swallowed a JSON error and returned `[]`, which was safe only because the built-in
  seven were a hardcoded floor. As the *sole* source there is no floor: a silent empty
  list would make `parse.build()` — a drop-and-rebuild over every config target —
  quietly rebuild `football.db` from **nothing**, destroying the modeled store on a
  single stray comma. So `_load_competitions()` raises immediately (naming the file and
  the parse error) on absence or malformed JSON, aborting the run before anything
  downstream sees an empty scope.

- **`orchestrate` and `cups` upsert uniformly — no built-in guard.** `_register`
  collapses to one path: drop any existing entry with this `league_id`, append the new
  one, write, `reload_competitions()`. Re-running `orchestrate <id>` on *any*
  Competition now rewrites its stored definition, exactly as the 32 registry entries
  already behaved; the former seven are no longer special. Curated ranges are preserved
  by the migration and, because `_seasons()` is coverage-driven (ADR 0009), a bare
  re-run largely regenerates the same floor; `--from`/`--to` set a range deliberately.

- **A uniform, tolerant record shape.** Each entry is
  `{league_id, name, seasons, calendar_year, type}`. `_register` writes all five; a
  hand-added entry may omit `type` (defaults `"league"`) and `calendar_year` (defaults
  `false`). `calendar_year` is now a per-entry field for every Competition, replacing
  the `_BUILTIN_CALENDAR_YEAR` set — the last piece of built-in-only state.

**Considered Options:**

- **One Python list in `config.py`, drop auto-registration.** The simplest possible
  "one list" — fully in source, no JSON, no reload. Rejected: it neuters the
  auto-discover-and-self-register workflow ADR 0009 exists to provide, and — as ADR
  0009 itself argued — programmatically appending to hand-curated, commented Python
  source is brittle and easy to malform. Keeping registration as *data* preserves the
  orchestrator's one-command UX.

- **Keep the built-in/registry split.** Rejected: it is exactly the two-sources,
  special-cased distinction this ADR removes, and it leaves most definitions untracked.

- **Un-ignore `data/competitions.json` in place** (a `.gitignore` negation). Smallest
  diff, but leaves the source-of-truth config sitting inside the regenerable cache
  directory — conceptually wrong and one `rm -rf data/` from gone. Rejected in favor of
  moving it into the package.

- **Keep swallowing load errors → empty list.** Rejected: harmless when the registry
  merely supplemented a hardcoded base, catastrophic as the sole source (a typo silently
  zeroes every Competition and the next rebuild wipes `football.db`).
