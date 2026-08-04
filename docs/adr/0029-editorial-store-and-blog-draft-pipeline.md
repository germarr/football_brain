# The Editorial Store, and a one-command pipeline that fills it

---
Status: accepted — builds on ADR 0018 (Refresh), ADR 0026 (commentary store),
ADR 0027/0028 (Published Store and its delta publish). Amends the boundary
statement of ADR 0026.
---

We can now turn a played match into a publishable match report with one command:

    uv run python -m football_blog.pipeline --fixture-id 1550903 --espn-id 401877036

Every piece already existed — `refresh`, `commentary.ingest`, `publish_pg`,
`football_blog.draft`. What was missing was the order to run them in, the checks
between them, and a name for the thing that comes out. This ADR records the four
decisions that were not obvious.

**Decisions:**

- **PocketBase becomes the Editorial Store — the first store here that is
  *authored* rather than derived.** ADR 0002 made every store disposable: drop it,
  re-parse the raw cache, get it back. ADR 0026 broke that once, knowingly, because
  a Commentary Line's *inferred* Category costs money and will not reproduce
  exactly. The **Narrative** inside a **Match Post** breaks it much harder: it is
  written by a model and then **edited by hand** before publication, and no re-run
  recovers the edit. There is no raw cache behind it and no rebuild path.
  Two consequences follow directly. First, the pipeline **refuses to redraft a
  published Match Post** — `upsert_post` keys on the Fixture id alone, so without
  the guard an ordinary re-run (after a Coverage heal, or a `--reclassify`) would
  replace the edited prose with fresh model output and reset the record to draft,
  silently. Drafts stay freely overwritable; that is what a draft is for, and
  `--redraft` waives the guard explicitly. Second, this store is the only one here
  that genuinely needs backing up.
  It is also the only **co-tenanted** store: the same PocketBase instance serves a
  personal site whose `posts`, `pages`, `projects`, `profile` and `users`
  collections are nothing to do with football. We never touch them, but they share
  one `pb_data`.

- **Our collections are `match_post` and `publication` — both renamed away from the
  obvious word, for different reasons.** `posts` was taken: the personal site's own
  `posts` collection has live records, a different schema
  (title/slug/content/excerpt/cover/tags) and its own Astro pages, and a Narrative
  shares only the word "post" with it. Sharing the name would have meant sharing the
  schema. `league` was worse than taken — it was *wrong*. That collection is not a
  Competition; it is our editorial decision to cover one, plus how it renders (slug,
  language, timezone, brand colour, prompt overrides, and its own `published` gate).
  And CONTEXT.md bans the bare word as a canonical noun precisely because a
  Competition may be a **cup** — the World Cup is one of the three Competitions
  published here, so a record named `league` would have configured a cup.
  Both live in `personal_site/pocketbase/pb_migrations/`, so the schema is
  reproducible even though its contents are not.

- **The ESPN stage sits *between* the cache refresh and the delta publish.** This is
  the pipeline's only real ordering decision, and it rests on a property of ADR
  0028's delta: `_apply_delta` full-copies the commentary tables on **every** apply,
  including when zero Fixtures changed. So one publish carries both the new Final
  *and* its commentary. Ingesting after the publish instead — the arrangement most
  readers would reach for, since ESPN is "extra" data — would need a second publish
  to move the commentary across, for no gain. Verified in practice: a delta run
  reporting `applied 0 changed Final(s)` still moved 1,228 commentary lines.

- **`--fixture-id` can now be verified against either `football.db` or the Published
  Store, and is refused only if both disagree.** `football.db` rebuilds only at
  04:00, and the pipeline deliberately skips that rebuild, so a same-day match can
  be absent from it or carry a stale kickoff — while the Published Store was
  refreshed moments earlier by the pipeline's own step 3. `verify_fixture_any` tries
  the local store first and falls back. The fallback only ever *adds* a chance to
  confirm: neither source can waive the other's disagreement, because a refusal is
  raised unless some source agreed, and the comparison itself (exact kickoff, exact
  team names) lives in one shared function so a second source cannot weaken it.

**Consequences:**

- **ADR 0026's boundary statement is restated, not broken.** That ADR's module
  claimed `fixture_link.py` was the *only* contact between the commentary pipeline
  and the rest of the store. There are now two — and the second is networked, needing
  `psycopg` and credentials. The invariant worth keeping was never the count: it is
  that every such contact is **read-only, verification-only, and never opened unless
  `--fixture-id` is supplied**. Both sources honour it. `psycopg` and
  `football.config` are imported *inside* `verify_fixture_pg`, and the CLI flag
  (`--verify-fallback-pg`) defaults off, so a plain `commentary.ingest` keeps its
  stdlib-and-`requests` dependency profile.

- **`football_blog` reads Postgres via `FOOTBALL_DATABASE_URL`, never the bare `PG*`
  vars.** It was doing the latter and silently reaching the *YouTube* database:
  `.env` defines `PG*` twice, and while shell tools take last-wins (which is why the
  football block is last), `football_blog.config` is **first**-wins, so a real
  environment variable can always override the file. Every loader query failed with
  `relation "narrated_match" does not exist`. This is the same rule
  `football.config.load_pg_url` already documented for `publish_pg`; the connection
  now also asserts `public.fixture` exists on first connect, so pointing at the
  wrong database fails immediately and names what it reached.

- **The pipeline is operator-driven for exactly one reason: the ESPN game id has no
  lookup.** Fixture ids come from Postgres and `draft.py` already sweeps, so this is
  the only un-automated step. It is a gap, not a principle — it would be tempting to
  justify the typed id by `fixture_link.py`'s refuse-rather-than-guess stance, but
  that argument does not hold: ESPN's `scoreboard?dates=YYYYMMDD` lists a day's
  matches, and any candidate it proposed would still face the same exact-kickoff and
  exact-team-name check. Proposal by code is not assertion by hand, and the refusal
  contract would be unchanged. Unbuilt, deliberately, and recorded here so the next
  reader knows it was considered.

- **The absent Astro frontend must follow both renames.** `football_blog` carries
  "keep in sync with `blog/la-cancha/src/lib/*.ts`" comments throughout, and that
  repo is not on this machine, so its parity tests cannot run and its queries against
  `collections/posts` and `collections/league` cannot be updated from here.
