# Test what fails silently; let loud failures stay loud

---
Status: accepted — blocks ADR 0031 (the package reorganization) and supplies the
preflight ADR 0032's nightly script runs first. Builds on ADR 0011, whose scoped-store
work already opened the one seam this suite needs.
---

Thirty-one ADRs in, this project has **no tests at all** — no `test_*.py`, no
`conftest.py`, no pytest configuration. That was survivable while changes were additive
and verified by running the thing. ADR 0031 is neither: it moves roughly thirty modules,
rewrites eleven `module=` strings that are only strings, and relocates two committed
registries, with a paid API and an unattended nightly job downstream.

So the suite is written **before** the move, not after it and not never. Its purpose is
narrow and worth stating plainly: it is a **refactoring harness**, not a quality
programme. It exists so the tree can be proved green, moved, and proved green again.

That purpose supplies a stopping rule, which matters more than the tooling — without one,
"add tests first" expands without limit and the reorganization never starts.

**Decisions:**

- **The rule: if it fails loudly, do not test it; if it fails silently, it must be
  tested.** Every dangerous thing found while planning ADR 0031 is dangerous *because it
  is quiet*. `venues.py` at a wrong path imports cleanly and returns `{}`. A stale
  `commands.py` module string is inert until an operator clicks a button. `git add` on a
  moved file reports success and commits nothing. A missing `data/raw` symlink turns a
  cache-first fetcher into a full re-fetch against a paid API. None of these raise.
  Meanwhile import errors, crashes and tracebacks are already caught the first time
  anything runs, and testing them buys nothing the move does not already provide.

- **Four guards, and the suite is done.** `test_registry.py` — both registries resolve
  through `football/paths.py`, exist, and are non-empty. `test_commands.py` — every
  `module=` string in `commands.py` resolves via `importlib.util.find_spec`, so a stale
  argv target fails in CI rather than under an operator's cursor. `test_parse.py` — a
  build from the committed fixture cache produces the expected tables and non-zero row
  counts, which is what a mis-resolved `RAW_DIR` would quietly hollow out.
  `test_fixture_link.py` — the 15-minute kickoff tolerance and the team-name anchor of
  ADR 0030, including that `--force-link` cannot waive the anchor. When those are green,
  the reorganization starts.

- **Fixtures are a curated ~1 MB slice of the real cache, committed under
  `tests/fixtures/raw/`.** One competition-season: its `leagues` record, its `fixtures`
  file, and ten fixtures' `fixtures_events`, `fixtures_players` and `fixtures_statistics`
  (~6.5 K + 47.7 K + 3.0 K each, against a live cache of roughly 8 GB). Tests point
  `config.RAW_DIR` at it by monkeypatch. Real provider shapes, hermetic, and independent
  of the `data/raw` symlink being present.

- **The suite is proved green on the *pre-move* tree first.** A refactoring harness that
  first goes green after the refactor has proved nothing. The tests are written and
  committed on `footballV3`, ahead of branching, so the baseline is a real observation
  rather than an assumption — and the move then updates them as part of its own diff.

- **`scripts/nightly.sh` runs the same guards as its preflight** (ADR 0032). The registry,
  `RAW_DIR` and `commands.py` assertions are exactly the invariants whose violation is
  otherwise invisible overnight, so they are worth re-asserting at 04:00 and not only in a
  test run. This is why the guards are written as importable checks rather than as pytest
  bodies alone.

## Consequences

- **Most of the codebase stays deliberately untested, and that is the decision, not an
  omission.** `taxonomy.py`, `classify.py`, the parse internals, the publish paths: none is
  covered. A future reader finding no `test_taxonomy.py` should not read it as an oversight —
  `taxonomy.py` cannot break by being moved, and when it breaks it breaks loudly.

- **The rule is a floor, not a ceiling.** Nothing here argues against covering more later.
  It sets what must be true before ADR 0031 may begin, and pytest's presence makes any
  later addition cheap.

- **The fixture slice can drift from provider reality.** It is a real capture, so it is
  accurate the day it is taken, but a provider response-shape change would leave the tests
  passing against a stale shape. Accepted: the alternative — testing against the live cache —
  skips when the cache is absent, and a suite that goes green by *skipping* is the very
  failure mode this ADR exists to eliminate.

- **`parse.build(db_path=…)` needed no new seam.** ADR 0011's scoped-store work already made
  the output path injectable, so `test_parse.py` writes to a temp DB without any production
  code changing to accommodate it. `config.RAW_DIR` stays a module constant and is
  monkeypatched; making it injectable would be a refactor performed to serve tests, which is
  the wrong direction while the tree is about to move anyway.

## Addendum — two claims above were wrong, found while implementing

Written the same day, after building the suite. Left in place rather than edited away,
because both errors are informative.

- **A mis-resolved `RAW_DIR` does *not* quietly hollow the store out.** The decision
  above justified `test_parse.py` on that basis. It is false: the parser's client is
  `CachedClient(max_live_requests=0)`, so the first cache miss raises `QuotaExceeded`
  rather than fetching, and the build dies at `_build_venues` before writing a row.
  A wrong cache path is one of the few dangers here that is already loud. What
  `test_parse.py` actually guards is narrower and still worth having: that the build
  pipeline end-to-end still populates every table after the move, and — the genuinely
  silent case — that a `register=False` build yields **zero venues with every
  `venue_id` null**, succeeding and reporting success while producing a store that is
  quietly wrong in one table. Both are pinned.

- **The fixture slice is ~2.2 MB, not ~1 MB.** The estimate counted only the
  per-fixture endpoints and forgot that a fixture drags in every player who appeared:
  bios (`players`) and career directories (`players_teams`) are ~80% of the weight.
  Measured: 6 fixtures → 4.5 MB / 580 files; 3 fixtures → 2.2 MB / 285 files; 2
  fixtures → 1.5 MB / 186 files. Settled on **3 fixtures of La Liga 2024** — 6 teams,
  131 players, 3 venues — which populates every table while keeping the slice small.
  `scripts/carve_test_fixtures.py` re-cuts it.

## Considered Options

- **Unit-cover the pure logic** (`venues`, `fixture_link`, `taxonomy`, `classify`, `scope`,
  `parse`). Broader and more conventionally respectable, but aimed at the wrong target: pure
  functions are what the move *cannot* break, and this option still misses `commands.py`
  string staleness entirely.

- **One end-to-end golden run** — commit a cache slice, run onboard → parse → publish, diff
  the result against a golden snapshot. Highest confidence that behaviour survived end to
  end, and the natural thing to want. Rejected for now as the *blocking* gate: a snapshot
  churns on every legitimate schema change, and the move must not be held behind a test that
  is expensive to keep true. It remains the obvious later addition.

- **Layered — silent-failure guards now, breadth after the move.** Nearly the same first step
  and a reasonable plan; not chosen as the framing because it defers the stopping rule rather
  than setting one, and an explicit "done" is what keeps this from displacing the work it
  exists to protect.

- **No tests; verify by hand and by preflight alone.** Cheapest, and what the project has
  done for thirty-one ADRs. Rejected here specifically: hand-verification of a thirty-module
  move costs API quota and hours, proves nothing about the next change, and cannot establish
  the green *baseline* that makes "the move preserved behaviour" a claim rather than a hope.
