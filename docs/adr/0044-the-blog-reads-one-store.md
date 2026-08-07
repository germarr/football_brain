# The blog reads one store

---
Status: accepted — extends ADR 0029 (Editorial Store) and ADR 0040 (Match Previews,
derived records in an authored store). Reads the Published Store of ADR 0027/0028.
Bounded by ADR 0020 (the Live Poll and its unpublished Live Mirror). Amends the
**Editorial Store** entry in CONTEXT.md and adds **Match Bundle** and **Fixture Row**.
---

The blog reads from two stores. Editorial records — Publications, Match Posts, Match
Previews, team slugs — come from PocketBase. The facts it renders on a post page — the
timeline, the squads, the stats, the crests, the last-five strips — come straight out of
the **Published Store** over `postgres.js`.

That second connection is why the site cannot move to Cloudflare Workers: `postgres.js`
does not run there, which rules out the Cloudflare adapter and with it per-route on-demand
rendering. It is also why a wrong crest or a stale name needs a pipeline run rather than an
edit in an admin UI.

This copies everything the site reads into the Editorial Store, so the blog reads one store
and the `postgres` dependency comes out of its `package.json`. Nothing about what the
pipeline **writes** changes; this is one more derived read-path laid on top.

Four decisions were not obvious.

**Decisions:**

- **Two collections, split by the question they answer — not one blob per Fixture.** The
  obvious design, and the one the migration brief proposed, was a single `match_bundle`
  carrying the whole rendering payload for every Fixture, with the ribbon and the
  upcoming-week table filtering it by date and the last-five strip filtering it by team.
  One collection, one writer, every feed a filter away.

  It was rejected on arithmetic. A bundle measures ~30 KB. The Published Store holds
  **9,514 Fixtures** against **40 Match Posts**, so that design puts ~285 MB into a
  SQLite file currently under a megabyte — to serve pages that do not exist, since the
  only thing that renders a bundle is a post page. So there are two collections, and the
  split is by question rather than by entity:

      match_bundle  — "render THIS Fixture"    keyed by Fixture id     ~40 records, ~30 KB
      fixture_row   — "which Fixtures?"        keyed by a date window  ~94 records, ~300 B

  Neither can answer the other's question, and that is the invariant to protect. The
  moment a feed filters `match_bundle` by date or by team it has assumed a history that is
  deliberately absent, and it will get a shorter answer rather than an error. The
  `PocketBaseClient` therefore offers **no** date-filtered lookup over bundles at all —
  the only way in is by Fixture id.

- **The last-five strip is stored on the bundle, not queried.** Recent form is the one
  feed that reads *arbitrary past Fixtures*, and it is the specific reason the
  one-collection design would have needed full history: answering "this team's last five
  before this date" by query requires those Fixtures to be present as records.

  It is ten rows, and its cutoff — the subject Fixture's kickoff — is known while the
  bundle is being built. Precomputing it costs nothing and removes the reason to store the
  other 9,474. The derivation goes with it: `gf`/`ga` are written from the subject team's
  perspective and `outcome` derived from those, so nothing downstream has to know which
  side the team was on. That inversion is the only way a form strip can be wrong while
  still looking like a form strip.

- **`commentary` is stripped on the way out.** The Python `FullFixture` carries ESPN
  key-moment lines, which `loader.py` joins for the drafter. They are model input, the
  TypeScript `FullFixture` has no field for them, and they are ~30% of the payload. The
  size is the least of it: these collections are readable anonymously, so shipping them
  would publish third-party text as a side effect of a performance change. The strip is a
  property of the projection rather than of `FullFixture`, and the suite asserts both
  halves — that no ESPN line survives into a payload, and that the source bundle still
  carries them for the drafter.

- **Live scores are out of scope, and the cadence is set honestly because of it.** The
  brief assumed the frequent pass should run every one to two minutes during match
  windows, to keep the ribbon current. Nothing it could read moves that fast. The
  Published Store has **no live timeline** — events, squads and team stats are cache-first
  and only land once a Fixture is Final (ADR 0018/0020) — and its live *score* advances
  only when the nightly Refresh runs at 04:00. The **Live Mirror** that does hold
  in-progress data is read by the Viewer and published nowhere.

  So the row pass runs every fifteen minutes, which is already faster than anything it
  copies, and `fixture_row` has **no `live_minute` field**. A permanently-null column
  reads as "not in play" rather than "never measured", and the brief's own schema carried
  it as a placeholder. Publishing the Live Mirror is a real piece of work and gets its own
  ADR; the field arrives with the job that can fill it.

**Consequences:**

- **Both passes compare before they PATCH, and that changes what the timestamps mean.**
  The quarter-hourly pass ran 94 unconditional PATCHes against a store that only advances
  once a night — ~9,000 writes a day, almost all of them rewriting identical bytes. It now
  writes only what moved, which on a quiet run is nothing.

  The comparison is against what PocketBase will have **coerced** the payload into, not
  against what was sent. A null number returns as `0` (PocketBase has no nullable number),
  a null text as `""`, and a timestamp comes back space-separated with milliseconds — each
  enough on its own to make every record compare unequal forever. That failure would have
  been invisible: the skip never firing looks exactly like the skip not existing.

  So `computed_at`, `football_computed_at` and PocketBase's own `updated` now mark when a
  record last **changed**, not when it was last rebuilt. Two consequences, one gained and
  one lost. Gained: `updated` becomes a genuine change signal a consumer can cache
  against, which it was not before. Lost: none of them answers "is the job still running"
  — a `computed_at` from yesterday is now the *normal* state of a Fixture whose score has
  not moved, and liveness belongs to the cron logs.

  Neither timestamp ever dated the scoreline itself. The store underneath moves once a
  night, so the natural fix for "the ribbon is stale" — raising the frequency — was always
  going to re-copy identical rows and change nothing. It now does so more cheaply.

- **`match_preview` takes the same treatment on one half only, and the asymmetry is the
  finding.** `--full` now compares its football half — table position, Team Leaders — and
  drops it from the payload when nothing moved. Its market half is always written.

  The obvious symmetry was rejected on measurement. Across one hourly interval all 33
  quoted markets had moved, because a Winner Market block carries `volume` and
  `open_interest` that tick with every trade — so a skip on `--quotes`, which is 792 of
  that collection's 825 daily writes, would have fired essentially never. And it would
  have cost something real: `quote_read_at` is rendered on the card, so freezing it would
  understate how current a forecast is, which is precisely the misdating ADR 0040 gave the
  record two timestamps to prevent.

  The result is that ADR 0040's two-timestamp design now shows through in the data rather
  than only in the code: `football_computed_at` sits at the last run that changed the
  football half while `quote_read_at_*` moves hourly. That is the intended reading of the
  record, and until now both stamps moved together and hid it.

- **`fixture_row` is a window, and the pass deletes what has left it.** This is the one
  failure mode here that looks *more* correct the longer it runs: every record left behind
  is individually valid, so a skipped delete produces no error and no wrong number — just
  a collection that grows without bound into the 9,514-row table the split exists to
  avoid. Absence from this collection therefore means "outside the window", never "no such
  Fixture".

- **A Match Bundle is written by the drafter as well as by the nightly pass.** A post whose
  bundle waits until 04:00 renders an empty page, and the operator who just drafted it
  would reasonably read that as the drafter having failed. The write happens *after* the
  Match Post — an orphan bundle is worse than a gap, because nothing cleans it up — and is
  wrapped, because the two halves are worth very different amounts. A Narrative cost a
  model call and, where an instruction was given, an operator's steer that nothing
  reconstructs; a bundle costs one rebuild.

- **CONTEXT.md's Editorial Store entry gains two more derived collections.** ADR 0040
  narrowed "cannot be regenerated" from the store to `match_post`; this widens the derived
  half again without touching that conclusion. The backup argument is unchanged and now
  rests on a smaller fraction of the instance.

- **PocketBase `date` fields come back space-separated.** A stored `kickoff_utc` reads back
  as `2026-08-06 02:30:00.000Z`, not `2026-08-06T02:30:00Z` — PocketBase normalises its own
  date columns. Python's `fromisoformat` accepts it (which is why `preview.py` has never
  had to care), but `new Date(...)` is not required to, and Safari has historically
  refused space-separated dates. The timestamps *inside* the JSON fields are ours and keep
  the `T…Z` form. The consumer that has to be careful is the ribbon, which renders
  `fixture_row.kickoff_utc` directly: it must normalise the separator before parsing. This
  is a property of every date field in this instance, `match_preview.kickoff_utc`
  included, not something these collections introduced.

- **The blog still cannot render a Fixture older than the window without a Match Post.**
  That is the intended shape — such a page does not exist — but it is the assumption to
  revisit first if a future feature wants, say, a club's full season archive. The answer
  then is a third narrow collection, not a wider `match_bundle`.

- **Four Publications are published today and all of them are gated twice**: the builders
  decline to write for an unpublished Publication, and both collections carry
  `publication.published = true` as their list and view rule. The second half is what
  still holds if a Publication is un-published later, leaving records behind.

- **The schema lives in two repos, and the copy here is guarded rather than trusted.**
  This ADR originally recorded that the migrations defining both collections sat in
  `personal_site`, a repo with no commits at all — so the contract had no history
  anywhere. That is now fixed at both ends: `personal_site` has an initial commit, and
  `football_blog/migrations/` holds the two files that define these collections, because
  they are this package's feature and a schema whose only definition lived in another repo
  was a contract with no home.

  That is a duplication, and this project's rule is that duplication which *agrees until
  it doesn't* is the dangerous kind. PocketBase reads `personal_site`'s copy, not this
  one, so editing the file here changes nothing — the README beside it says so, and
  `tests/test_pocketbase_schema.py` is what stops the copy quietly becoming false.

  It checks against the **live instance**, not against the other repo's file, and that is
  the sharper choice: a field renamed in the PocketBase admin UI touches no migration file
  anywhere, so a file-to-file comparison would pass while the schema had moved underneath
  the builder. It matters because **PocketBase ignores unknown keys on write** — a field
  that disappears does not fail the builder, the PATCH returns 200, the run reports
  `written`, and the value is simply absent from the page. The test skips when PocketBase
  is unreachable, so the rest of the suite stays runnable offline.
