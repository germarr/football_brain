/// <reference path="../pb_data/types.d.ts" />

// `match_bundle` — the rendering facts behind one Match Post (alt_data ADR 0044).
//
// A **Match Bundle** is everything the blog needs to render a played Fixture: the
// Fixture row itself, the event timeline, both squads, team match stats, both Team
// Profiles, the Venue, every Player named anywhere in it, and each side's last five
// results. Written by alt_data/football_blog/bundle.py.
//
// This collection exists for one reason: the Astro site used to read these facts from
// Postgres over `postgres.js`, which cannot run on Cloudflare Workers. Moving them here
// is what lets the site drop that dependency and render on demand.
//
// ## Scoped to Match Posts, deliberately, and this is the load-bearing decision
//
// There is one record per Fixture that HAS a Match Post — about 40 today — not one per
// Fixture. The Published Store holds 9,514 Fixtures and a bundle measures ~30 KB, so
// bundling everything would put ~285 MB into this SQLite file. It would also be pointless:
// the only page that renders a bundle is a post page, and a Fixture without a Match Post
// has no page.
//
// The rule this protects: **never make this collection the answer to "give me some
// Fixtures".** That question belongs to `fixture_row`. The moment a feed filters
// `match_bundle` by date or team rather than by Fixture id, it has quietly assumed full
// history is present here, and it is not.
//
// ## Why `home_form` / `away_form` are stored rather than queried
//
// The last-five strip is the one feed that reads *arbitrary past Fixtures*. Answering it
// by query is what would have forced full history into this collection. It is exactly ten
// rows, and its cutoff — the subject Fixture's kickoff — is known when the bundle is built,
// so precomputing it costs nothing and removes the reason to store the other 9,474.
//
// ## Derived, like `match_preview` and unlike `match_post`
//
// Every field here falls out of a rebuild from the Published Store; losing the whole
// collection costs one run of the builder. `match_post` remains the only collection in
// this instance with no rebuild path — that distinction is per-collection, not per-store,
// and the backup story rests on it.
//
// So there is no freeze and no lifecycle. A Match Preview needs one because a Quote is a
// point-in-time read that nothing reconstructs; a bundle is only history, and history is
// derivable forever. Corrections upstream SHOULD flow through on the next nightly run.
//
// ## What is deliberately absent: `commentary`
//
// The Python `FullFixture` carries ESPN key-moment lines. They are drafting input for the
// model, they are third-party text, and the TypeScript `FullFixture` has no field for
// them. They are stripped before write. Do not add the field here to "make the shapes
// match" — the shapes are meant to differ, and the list rule below is public.
//
// Field names are the wire contract with football_blog/bundle.py AND with
// blog/la-cancha/src/lib/types.ts — renaming one here breaks the builder silently, since
// PocketBase ignores unknown keys on write.
//
// On `required`: PocketBase treats required as *non-zero-value*, not merely *present*. The
// JSON fields are therefore optional — a Fixture may have no Venue, a coverage-light
// Competition may report no team stats, and a team's first ever match has no form.
migrate((app) => {
  const bundle = new Collection({
    "id": "pbc_2001000005",
    "name": "match_bundle",
    "type": "base",
    // Same gate as `match_preview`. The builder already declines to write for an
    // unpublished Publication, so this is defence in depth — and it is the half that
    // still holds if a Publication is un-published later, leaving bundles behind. The
    // pipeline authenticates as a superuser and bypasses these rules entirely.
    "listRule": "publication.published = true",
    "viewRule": "publication.published = true",
    "createRule": null,
    "updateRule": null,
    "deleteRule": null,
    "indexes": [
      "CREATE UNIQUE INDEX `idx_match_bundle_fixture` ON `match_bundle` (`postgres_fixture_id`)",
      "CREATE INDEX `idx_match_bundle_kickoff` ON `match_bundle` (`kickoff_utc`)",
      "CREATE INDEX `idx_match_bundle_status` ON `match_bundle` (`status`)"
    ],
    "fields": [
      {
        "autogeneratePattern": "[a-z0-9]{15}",
        "hidden": false,
        "id": "text3208210256",
        "max": 15,
        "min": 15,
        "name": "id",
        "pattern": "^[a-z0-9]+$",
        "presentable": false,
        "primaryKey": true,
        "required": true,
        "system": true,
        "type": "text"
      },
      {
        // The identity. A Match Bundle is keyed on its Fixture and on nothing else, the
        // same key `match_post` and `match_preview` are keyed on — hence the unique index.
        "hidden": false,
        "id": "number1000000501",
        "max": null,
        "min": null,
        "name": "postgres_fixture_id",
        "onlyInt": true,
        "presentable": false,
        "required": true,
        "system": false,
        "type": "number"
      },
      {
        "cascadeDelete": false,
        "collectionId": "pbc_2001000001",
        "hidden": false,
        "id": "relation1000000502",
        "maxSelect": 1,
        "minSelect": 0,
        "name": "publication",
        "presentable": false,
        "required": true,
        "system": false,
        "type": "relation"
      },
      {
        // Denormalised out of `fixture` so a list page can sort and filter without
        // unpacking a 30 KB blob. Same instant as `fixture.date`, serialized with a
        // trailing Z: the Postgres column is TIMESTAMP WITHOUT TIME ZONE holding naive
        // UTC (alt_data ADR 0005), and an offset-less string would be read as local time
        // by every `new Date(...)` on the site.
        "hidden": false,
        "id": "date1000000503",
        "max": "",
        "min": "",
        "name": "kickoff_utc",
        "presentable": false,
        "required": true,
        "system": false,
        "type": "date"
      },
      {
        // Denormalised out of `fixture` for the same reason. FT | AET | PEN | NS | CANC |
        // Canc | PST — note the store holds cancelled Fixtures under TWO spellings, so
        // any filter written against this field must whitelist, never blacklist.
        "autogeneratePattern": "",
        "hidden": false,
        "id": "text1000000504",
        "max": 0,
        "min": 0,
        "name": "status",
        "pattern": "",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "text"
      },
      {
        // Both denormalised so the ribbon can find a Fixture's post without unpacking
        // every bundle.
        "hidden": false,
        "id": "number1000000505",
        "max": null,
        "min": null,
        "name": "home_team_id",
        "onlyInt": true,
        "presentable": false,
        "required": true,
        "system": false,
        "type": "number"
      },
      {
        "hidden": false,
        "id": "number1000000506",
        "max": null,
        "min": null,
        "name": "away_team_id",
        "onlyInt": true,
        "presentable": false,
        "required": true,
        "system": false,
        "type": "number"
      },
      {
        // `FixtureRow` — kickoff, tournament metadata, final score. `home_goals`/
        // `away_goals` are the ON-PITCH result; a shootout lives in `penalty_home`/
        // `penalty_away` and a PEN tie cannot be decided from goals alone.
        "hidden": false,
        "id": "json1000000507",
        "maxSize": 0,
        "name": "fixture",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        // `MatchEventRow[]`, pre-sorted by `event_index`. Sorted on write because
        // `event_index` is a per-Fixture surrogate preserving provider order, and nothing
        // downstream re-derives that order.
        "hidden": false,
        "id": "json1000000508",
        "maxSize": 0,
        "name": "events",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        // `SquadEntryRow[]` — both squads. The site renders status, minutes, position,
        // captain, goals, yellow, red; the rest ride along for future expansion.
        "hidden": false,
        "id": "json1000000509",
        "maxSize": 0,
        "name": "squad",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        // `TeamMatchStatRow[]` — 0, 1 or 2 rows. A coverage-light Competition reports
        // none, and that is a fact about the Competition, not a failure.
        "hidden": false,
        "id": "json1000000510",
        "maxSize": 0,
        "name": "team_stats",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        "hidden": false,
        "id": "json1000000511",
        "maxSize": 0,
        "name": "home_profile",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        "hidden": false,
        "id": "json1000000512",
        "maxSize": 0,
        "name": "away_profile",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        // `VenueRow` or null. Venue ids come from alt_data's committed append-only Venue
        // registry, so the same stadium carries the same id in every store (ADR 0028/0042).
        "hidden": false,
        "id": "json1000000513",
        "maxSize": 0,
        "name": "venue",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        // Object keyed by player id — an OBJECT, not a Map and not an array, so it
        // survives the JSON round trip. Keys are strings because JSON object keys always
        // are; the site must stringify before lookup.
        "hidden": false,
        "id": "json1000000514",
        "maxSize": 0,
        "name": "players",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        // The last five finished Fixtures strictly BEFORE this one's kickoff, newest
        // first, from this team's perspective: opponent, is_home, gf, ga, outcome.
        // Stored rather than queried — see the header. `gf`/`ga` are already inverted for
        // an away result, so nothing downstream needs to know which side the team was.
        "hidden": false,
        "id": "json1000000515",
        "maxSize": 0,
        "name": "home_form",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        "hidden": false,
        "id": "json1000000516",
        "maxSize": 0,
        "name": "away_form",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "json"
      },
      {
        // Named to match `match_preview`'s convention. One timestamp is right here, where
        // `match_preview` needs two: a bundle has only a football half, so there is no
        // second cadence for a single field to misdate.
        //
        // It marks when the bundle last CHANGED, not when it was last rebuilt — the
        // builder compares before it writes, so an unchanged bundle keeps its old stamp
        // (and its old `updated`, which is therefore a usable change signal).
        "hidden": false,
        "id": "date1000000517",
        "max": "",
        "min": "",
        "name": "football_computed_at",
        "presentable": false,
        "required": true,
        "system": false,
        "type": "date"
      },
      {
        "hidden": false,
        "id": "autodate1000000518",
        "name": "created",
        "onCreate": true,
        "onUpdate": false,
        "presentable": false,
        "system": false,
        "type": "autodate"
      },
      {
        "hidden": false,
        "id": "autodate1000000519",
        "name": "updated",
        "onCreate": true,
        "onUpdate": true,
        "presentable": false,
        "system": false,
        "type": "autodate"
      }
    ]
  });

  return app.save(bundle);
}, (app) => {
  const bundle = app.findCollectionByNameOrId("match_bundle");
  return app.delete(bundle);
});
