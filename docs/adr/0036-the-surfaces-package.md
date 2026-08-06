# The three local apps move into `surfaces/`, and `web/` becomes `serving/`

---
Status: accepted — **narrows ADR 0031** (context is the tree), **reverses ADR 0034's
tree placement** of the Desk, and **simplifies ADR 0035**, whose template-lending
machinery this makes unnecessary. Builds on ADR 0023 (the serving store) and ADR 0033
(test what fails silently).
---

ADR 0035 put the Viewer, the Desk and the Console on one port, under one header, in one
process. Their code stayed in three packages: `web/`, `football_blog/desk/`, `console/`.
Editing the header meant three directories; answering "where does this app's template
live" meant remembering which context owned it.

ADR 0031 would ordinarily settle this and settle it the other way. It made context the
folder tree and rejected role as the tree in as many words — its Considered Options list
names `frontend/` as the literal reading of that request and refuses it, because role
cross-cuts context and only one axis can be the tree.

**That reasoning is not being overturned; it is being applied to a fact it predates.**
0031's argument turns on packages that own stores: splitting `commentary/` by role would
break one-package-one-store, which five ADRs lean on. The three surfaces own **no store
between them**. The Viewer reads `serve.db` and `live.db` and owns neither; the Console,
since ADR 0023 stripped its reads, opens nothing at all; the Desk reads the Editorial and
Published Stores, both owned elsewhere. Grouping them moves no store and breaks no
invariant. And since ADR 0035 they are one process — a deployment unit that 0031's tree
has no way to express, because when 0031 was written they were three.

**Decisions:**

- **`surfaces/` stops being a shim and becomes the package it was standing in for.**

  | was | becomes |
  |---|---|
  | `web/app.py`, `web/templates/` | `surfaces/viewer/` |
  | `football_blog/desk/` | `surfaces/desk/` |
  | `console/` | `surfaces/console/` |
  | `web/publish.py`, `web/serve.db` | `serving/` |
  | `surfaces/app.py`, `surfaces/templates/` | unchanged — the composition root |

  The subpackages take the glossary's words, which are already the nav's labels. Each
  keeps its own `__main__.py`, so the debug entrypoints become `python -m surfaces.viewer`
  and friends; `python -m surfaces` remains the front door.

- **This reverses ADR 0034's placement of the Desk, and the reversal turns on visibility.**
  0034 kept the Desk inside `football_blog/` because it imports `loader`, `prompts`,
  `pocketbase`, `candidates` and `espn_lookup`, and rooting it *"would create a package
  with no store of its own reaching deep into another context's internals — the exact
  coupling ADR 0031's tree exists to make visible."*
  The coupling is real and does not change: those five modules cannot follow, because each
  is load-bearing for `pipeline`, `onboard`, `draft` or the command registry. What changes
  is that it stops being camouflaged. `from ..candidates import list_board` reads as an
  ordinary intra-package import. `from football_blog.candidates import list_board`, sitting
  in `surfaces/desk/app.py`, states plainly that this app reaches into another context —
  which is what 0031 said the tree is *for*. Making the coupling visible was the goal;
  hiding it behind a relative import was the accident.

- **`web/` becomes `serving/`, because a package called `web` with no web app in it is the
  confusion this ADR exists to remove.** What is left is coherent: `serving/publish.py`
  builds `serving/serve.db`, and the package still **owns** that store, which is the
  invariant ADR 0023 was explicit about. Folding `publish.py` into `football/publish/`
  beside `pg.py` and `delta.py` was considered and rejected for the opposite reason: it
  would leave `serve.db` written by one package and owned by none.

- **ADR 0035's template lending is deleted, and with it one of its two silent failures.**
  That machinery — `surfaces/app.py` reaching into each app's Jinja loader, every header
  including `_nav.html` with `ignore missing` — existed only because the three packages
  were strangers that could not name each other. As siblings they can: each appends
  `surfaces/templates/` to its own loader at import, unconditionally, and includes the nav
  unconditionally. A renamed `_nav.html` now raises `TemplateNotFound` instead of removing
  the nav from every page with no error anywhere.
  A single shared Jinja environment was rejected on evidence: there are three `index.html`
  files, and one loader would resolve two of them to the wrong app.

- **This is a relocation and nothing else.** The four duplicated palettes — 431 lines of
  inline `<style>` across four templates, differing only in `--accent` — are *not* unified
  here, though being siblings finally makes it cheap. A move that also changes rendering
  makes every visual regression ambiguous about its own cause, and this one already
  relocates a 39 MB store, edits the cron and rewrites `.gitignore`. The stylesheet is a
  separate commit whose diff is worth looking at on its own.

**Consequences:**

- **The `.gitignore` is the dangerous part, and it fails by *adding*.** Its ignores are
  path-anchored — `web/serve.db`, `web/logs/`, `console/logs/`,
  `football_blog/desk/logs/`. `git check-ignore` against every proposed path returns
  *tracked*, so this move done naively commits a 39 MB database and every job log. This is
  ADR 0031's failure running backwards: there, a stale ignore silently *dropped*
  `football/build/__init__.py`. The ignores move in the same commit, verified with
  `git check-ignore` before staging rather than after.

- **Two path holders, with unequal protection.** `commands.py`'s `module="web.publish"`
  becomes `serving.publish` and is covered — `tests/test_commands.py` resolves every
  registry string (ADR 0033), so a stale one fails the suite instead of waiting to be
  clicked. `scripts/nightly.sh` is **not** covered: it names both `-m web.publish` and
  `web/logs/publish-cron.out`. ADR 0031 called the crontab "the dangerous one" and ADR 0032
  moved the sequence into git so a change like this is reviewable; that review is the whole
  mitigation, and it is a person reading a file.

- **`serve.db` is not migrated, it is re-derived.** It is gitignored and rebuilt by the
  04:00 publish, so the old 39 MB file is deleted rather than moved.

- **A standalone surface now renders a nav whose other two links 404.** The Viewer is
  mounted at `/`, so `root_path` is `""` both composed and standalone and it cannot tell
  the difference. A `COMPOSED` flag set by the composition root was rejected: it
  reintroduces exactly the must-be-set-correctly state that `ignore missing` already
  proved fragile, to fix a confused second in a debug entrypoint.

- **Older ADRs now cite paths that no longer exist, and are not rewritten.** ADR 0023
  (`web/`), 0034 (`football_blog/desk/`) and 0035 (all three) describe the tree as it was
  when each was decided. That is what an ADR is for; the map above is the forwarding table,
  the same stance ADR 0031 took for its fourteen.

- **CONTEXT.md is untouched, for the third change running.** The Desk's entry names what it
  does and never where it lives, so nothing there goes stale. "Surface", "package" and
  "mount" remain implementation, which is the line ADR 0023 drew.

- **What fails silently here (ADR 0033):** the `.gitignore` above, which announces itself
  only as an enormous diff; and `nightly.sh`, which announces itself at 04:00 the following
  morning by publishing nothing, into a log path that no longer exists.
