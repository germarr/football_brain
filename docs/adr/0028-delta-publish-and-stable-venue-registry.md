# Delta publish to the Postgres Published Store, on a stable venue-id registry

---
Status: accepted — amends ADR 0027 (Postgres Published Store), builds on ADR 0018
(Refresh ledger) and ADR 0019 (committed `competitions.json`).
---

ADR 0027 publishes the Postgres **Published Store** by re-parsing every published
Competition's whole history into a throwaway SQLite store and swapping it over `public`
wholesale, and it explicitly deferred anything incremental — *"Rejected as premature: the
full publish takes ~110s, well inside 'just re-run it'. **Revisit if the Competition set
grows enough that the re-parse dominates.**"* Two things triggered that revisit: `--only`
scoping made `refresh_pg` a **frequent** (many-times-a-day) path, and each such run still
re-parses and re-COPYs **~440k rows** (Liga MX + MLS: ~8.9k fixtures, ~123k events, ~287k
squad entries) only to add the handful of Finals a matchday produces — a ~600–1000× write
amplification that grows every season.

We keep the wholesale publish as the **manual reset**, and add a fast **delta publish** as
the default intraday path. The delta sends only Finals whose data actually changed, which
forces one foundational change — venue ids must stop being re-derived per build — and a
chain of consequences that follow from it.

**Decisions:**

- **`refresh_pg` becomes a delta publish by default; `publish_pg` stays wholesale.**
  `python -m football.refresh_pg [ids|--all]` now runs Refresh (`--only` the published
  Competitions, ADR 0018 amendment 2) then a **delta** publish; `--full` forces the old
  wholesale run. `python -m football.publish_pg [ids|--all]` is unchanged — the full
  wholesale replace, now the **reset / re-baseline** tool (first migration, registry loss,
  the retraction reset below). The everyday command is fast; the big hammer is explicit.

- **The delta sends only *new or re-healed* Finals, keyed on the Refresh ledger's
  `collected` date.** A Final is immutable under the cache-first contract (ADR 0018) — the
  *only* thing that mutates it is a Coverage heal, which re-stamps the ledger `collected` to
  the re-collection date (always fresh). So "changed since we last published it" is exactly
  "ledger `collected` is newer than what Postgres holds." The delta re-parses the published
  Competitions' **current Season** into a temp store (cheap, zero-API — the unit *parsed*),
  but applies only the fixtures whose `collected` beats their Postgres stamp (the unit
  *sent*). Immutable past Seasons are never parsed or touched. (A back-filled Coverage widen
  on a *past* Season — vanishingly rare, since heals live inside the 14-day
  `OPTIMISTIC_PROBE_DAYS` window — is not caught by the delta; a manual full publish heals it.)

- **"Already published" is a per-fixture stamp table in Postgres, advanced in the apply
  transaction.** A `publish_fixture(fixture_id, collected)` side table is the delta's
  watermark: `delta = ledger fixtures WHERE collected > coalesce(stamp, '')`. Chosen over a
  single global-date watermark (needs boundary-day re-work, and a max-date can skip a
  late-arriving heal) and over a local file (drifts from what actually committed). It is a
  ~9k-row shadow of the local ledger, but it is the *only* honest record of what reached the
  remote, it advances only for fixtures that committed (same transaction), and a full publish
  rebuilds it. It is a separate meta table, so the ten published football tables stay
  schema-identical to `football.db` (ADR 0027).

- **The apply is one transaction, per-fixture — the blue-green guarantee at fixture grain.**
  For each changed Final: delete its child rows (`event`, `squadentry`, `teammatchstat` by
  `fixture_id`) then the `fixture`, and insert the freshly parsed rows; upsert the **core
  dimensions** (`competition`, `team`, `player`, `venue`) so a debut player / promoted team /
  new ground the Final references exists first — all keyed on stable ids (`venue` now via the
  registry, the rest provider ids); advance its `publish_fixture` stamp. Delete-then-reinsert a
  *whole* fixture is sound because `Event.event_index` is a **per-fixture** surrogate (a row's
  position in that one fixture's response array) — stable within the fixture we replace,
  never across fixtures. A crash rolls the whole delta back, exactly as the wholesale swap does.

  The **career directory** (`teamprofile`, `playerteam`) is deliberately *not* delta-applied:
  it is FK-referenced by nothing (`PlayerTeam.team_id` is not a key), it is lag-tolerant by
  design (CONTEXT.md — it "fills progressively"), and for one current Season it is ~10× the
  delta's whole row volume (measured: ~12k `playerteam` vs ~1.3k for the four core dimensions).
  So a debut player reaches the store with their Fixtures immediately but their career rows only
  at the next wholesale publish — the accepted, documented lag.

- **Venue ids become globally stable, via a committed append-only registry — the crux.**
  ADR 0027's `Venue.id` is enumerated `1..N` in sorted `(name, city)` order *per build*, so
  one new alphabetically-early stadium shifts every id. An additive delta cannot re-enumerate
  — the fixtures already in Postgres point at the old numbering. So `parse._build_venues`
  stops enumerating and instead reads `(name, city) → id` from a committed
  **`football/venues.json`** (the `competitions.json` precedent, ADR 0019), assigning the same
  id in *every* store built from it — `football.db`, each scoped `data/<slug>.db`, the delta
  temp store, Postgres. New keys are appended `max(id)+1` in sorted order under a file lock;
  existing ids never move. It is seeded once by a sorted enumeration of all cached venues
  across all Competitions, and append-only thereafter.

  This deliberately **breaks the repo's "everything under `data/` is regenerable from the raw
  cache" invariant** (`.gitignore`): a sequential append-only map cannot be reproduced from
  the cache — rebuilt in sorted order, a later-added early-alphabet stadium gets a different
  id than it did when appended incrementally. So `venues.json` is, like `data/commentary.db`,
  a non-regenerable artifact — but a *milder* one: its loss is recoverable **to-equivalent**
  (rebuild going forward, one global re-baseline renumbers every store; the ids are
  surrogates nobody pins externally), where commentary's loss is forever. Committing it
  (rather than a gitignored side store) buys recovery **to-identical** via git history, at the
  cost of rare commit churn — acceptable because new stadiums appear a handful of times a
  *season*, not per run. The content-hash alternative (a `blake2b(name,city)` id, needing no
  state at all) was rejected to keep venue ids small sequential `INTEGER`s rather than turning
  `Venue.id`/`Fixture.venue_id` into non-sequential `BIGINT`s across every store.

- **The unattended delta is read-only against `venues.json`; only the nightly full build
  mints.** The intraday delta runs unattended and would otherwise have to write (and commit) a
  tracked file. Instead the nightly `refresh` — which rebuilds `football.db` over *all*
  Competitions and so sees every venue — is the sole minter; the nightly cron commits
  `venues.json` when it changed. If an intraday delta meets a not-yet-registered venue it
  publishes that fixture with **`venue_id = NULL`** (already a common state — the provider
  names no venue in ~32% of fixtures), and a **surgical nightly heal** fills it once the
  venue is registered: `UPDATE fixture SET venue_id = <registry> WHERE venue_id IS NULL AND
  the (name,city) now resolves`. We deliberately do **not** run a nightly full publish for
  this — the surgical `UPDATE` is enough, and keeps "re-send everything" out of the automated
  path entirely.

- **The commentary half stays a full, unscoped copy each delta.** `narrated_match` /
  `commentary_line` (ADR 0026) have no per-fixture watermark and are tiny (~200 KB), so the
  delta re-copies them whole inside its transaction — the same "commentary is always all of
  it" invariant ADR 0027 established, now the one full-replace inside an otherwise additive run.

## Consequences

- **Retracted rows are an accepted blind spot of the intraday path.** ADR 0027 chose
  wholesale replace partly because it "would never leave a row the provider has retracted." A
  purely-additive delta *does*: a provider-retracted Final would linger in Postgres. We accept
  this because retractions hit **scheduled** fixtures, essentially never **Finals** (the only
  rows we publish), and the escape hatch — a manual `publish_pg` — already exists for other
  reasons. Drift in a dimension attribute not attached to any new Final (a re-edited player
  bio) is the same class: rare, cosmetic, reset by a full publish.

- **Rollout is a one-time re-baseline.** (1) Seed `football/venues.json` from a sorted
  enumeration of all cached venues; (2) run one full `publish_pg` — it lays the registry venue
  ids (renumbering Postgres's venues once — they are surrogates) and builds/fills
  `publish_fixture`; (3) intraday deltas thereafter. The same full publish is the recovery
  path after a `venues.json` loss.

- **`football.db` and every scoped store also change venue numbering** on their next rebuild
  (they now read the registry). This is invisible downstream — venue ids were never equal
  *across* stores before and are not pinned by anything — but it is why the change is repo-wide
  rather than confined to `publish_pg`. The upside is a new, stronger invariant: the same
  stadium now carries the same id in every store.

## Considered Options

- **Delta unit = current Season, or a full re-parse diffed row-by-row.** The current-Season
  unit re-sends a whole season each run even when nothing changed; the diff still re-parses the
  whole decade and any new venue makes the diff see "everything changed." The ledger-keyed
  per-fixture delta sends the least and rides the mutation signal (`collected`) the Refresh
  already maintains.

- **Content-hash venue id (no registry).** `id = hash(name, city)` needs zero durable state
  and is reproducible from the cache alone — but makes ids non-sequential `BIGINT`s across
  every store. Rejected for the sequential-integer registry; the trade was regenerability for
  small readable ids.

- **Gitignored on-box registry (SQLite `UNIQUE(name,city)`, or JSON+flock).** Cleaner write
  path (no commit churn, DB-enforced atomicity) but recoverable only to-*equivalent*, never
  to-*identical*. Rejected in favour of a committed file for git-versioned identical recovery.

- **Nightly full publish as the reconciler.** Would heal NULL venues *and* drop retracted rows
  *and* re-baseline in one ~110s pass. Rejected to keep any "re-send everything" out of the
  automated path; the surgical venue heal covers the only issue that actually needs nightly
  attention, and retractions are handled by the accepted manual reset.

## Amendment (2026-08-06): the delta also replaces scheduled Fixtures

*Required by ADR 0040 (Match Previews), which reads upcoming Fixtures from the Published Store.*

As accepted, `delta_publish` computes `changed` from the ledger — which knows only **Finals**
— so a **scheduled** Fixture is never inserted, never re-dated and never removed. Its non-Final
rows therefore date from whenever a wholesale `publish()` was last run by hand. Today
Postgres and `football.db` happen to agree exactly on all 410 upcoming Fixtures, but nothing
in `scripts/nightly.sh` maintains that: the sequence runs `refresh`, `pg --heal-venues`, the
venue commit and `serving.publish`, and no wholesale publish at all. The gap has a date on it —
Leagues Cup's group stage ends 2026-08-14 and the knockout Fixtures drawn afterwards are new
non-Final rows that a delta would never carry across.

So each `delta_publish` now **replaces the current Season's non-Final Fixtures wholesale**:
delete them, reinsert from the staging parse. One statement handles re-dating, cancellation
and newly-drawn Fixtures alike, and it is idempotent.

This changes a word in this ADR's own description, which is why it is recorded rather than
slipped in: **the delta is no longer purely additive — it deletes.** The property this ADR
actually cared about is preserved absolutely, though. The deletion is scoped to non-Final
rows of the current Season, which is precisely the mutable frontier a **Refresh** already
re-fetches by design (CONTEXT.md), and it provably cannot touch a Final. It is also safe to
cascade nothing: across the entire Published Store, non-Final Fixtures carry 0 squad entries,
0 team match stats and 1 stray event.

The stated blind spot is unchanged and now narrower — a provider-retracted **Final** is still
removed only by a wholesale `publish()`. A retracted *scheduled* Fixture now disappears on the
next delta.
