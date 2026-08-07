/// <reference path="../pb_data/types.d.ts" />

// `fixture_row` — one Fixture as it appears in a list (alt_data ADR 0044).
//
// A **Fixture Row** is the narrow, denormalised card the blog draws when it is showing
// Fixtures rather than reading one: the persistent ribbon on every page, and the homepage's
// upcoming-week table. Crest, name, kickoff, status, score. Written by
// alt_data/football_blog/bundle.py --rows.
//
// ## Why this is separate from `match_bundle`
//
// These two collections answer two different questions and must not be merged.
//
//   match_bundle  — "render THIS Fixture"    keyed by Fixture id, ~30 KB, ~40 records
//   fixture_row   — "which Fixtures?"        keyed by date window, ~300 B, ~94 records
//
// Serving the ribbon out of `match_bundle` would mean a bundle for every Fixture in the
// Published Store — 9,514 of them, ~285 MB — to render a strip that shows two crests and a
// scoreline. And serving a post page out of `fixture_row` is impossible: there is no
// timeline here, no squad, no stats.
//
// ## A WINDOW, not an archive — the invariant that makes this collection cheap
//
// Records exist for Fixtures within roughly −3 to +14 days of the run. **The builder
// deletes records that have fallen out of the window**, and that delete is not tidiness:
// without it this collection grows without bound and slowly becomes the 9,514-row table the
// split above exists to avoid.
//
// The consequence to hold onto: absence here means "outside the window", never "no such
// Fixture". A page that needs a Fixture older than the window wants `match_bundle`, or it
// wants a widened window — it does not want a fallback that silently renders nothing.
//
// The window is wider than either consumer needs on its own. The ribbon wants one local
// day, the upcoming table wants Monday to Sunday; one window covering both is one job, and
// the rows are small enough that the slack costs nothing.
//
// ## No `live_minute`, on purpose
//
// The obvious field is missing because nothing could fill it. alt_data's Published Store
// has no live timeline — events, squad and stats are cache-first and only land once a
// Fixture is Final (ADR 0020) — and its live SCORE only advances when the nightly Refresh
// runs. The provisional Live Mirror that does hold in-progress data is never published.
//
// So a `live_minute` here would be null forever, and a null field reads as "not in play"
// rather than "never measured". When the Live Mirror gains a publish path, add the field
// with the job that populates it.
//
// Derived, like `match_bundle`: losing this collection costs one run of the builder.
//
// Field names are the wire contract with football_blog/bundle.py and with the
// `TodaysFixture` type in blog/la-cancha — PocketBase ignores unknown keys on write, so a
// rename here fails silently.
migrate((app) => {
  const row = new Collection({
    "id": "pbc_2001000006",
    "name": "fixture_row",
    "type": "base",
    "listRule": "publication.published = true",
    "viewRule": "publication.published = true",
    "createRule": null,
    "updateRule": null,
    "deleteRule": null,
    "indexes": [
      "CREATE UNIQUE INDEX `idx_fixture_row_fixture` ON `fixture_row` (`postgres_fixture_id`)",
      "CREATE INDEX `idx_fixture_row_kickoff` ON `fixture_row` (`kickoff_utc`)",
      "CREATE INDEX `idx_fixture_row_status` ON `fixture_row` (`status`)"
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
        // The identity, and the cross-reference: the site looks a Fixture id up in
        // `match_post` / `match_preview` to decide whether a ribbon card is clickable.
        "hidden": false,
        "id": "number1000000601",
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
        // Carries the accent colour and the display timezone the card is formatted in.
        // Also what the list rule gates on.
        "cascadeDelete": false,
        "collectionId": "pbc_2001000001",
        "hidden": false,
        "id": "relation1000000602",
        "maxSelect": 1,
        "minSelect": 0,
        "name": "publication",
        "presentable": false,
        "required": true,
        "system": false,
        "type": "relation"
      },
      {
        // Serialized with a trailing Z — see the same note on `match_bundle.kickoff_utc`.
        // This is the field the window is computed on and the ribbon sorts by.
        "hidden": false,
        "id": "date1000000603",
        "max": "",
        "min": "",
        "name": "kickoff_utc",
        "presentable": false,
        "required": true,
        "system": false,
        "type": "date"
      },
      {
        // The provider's competition id, duplicated alongside the relation so a caller can
        // group by Competition without expanding it.
        "hidden": false,
        "id": "number1000000604",
        "max": null,
        "min": null,
        "name": "league_id",
        "onlyInt": true,
        "presentable": false,
        "required": true,
        "system": false,
        "type": "number"
      },
      {
        // Display fallback only. The Publication's `display_name` is the name to render —
        // this is what the provider calls the Competition, which is not always the same
        // thing and is never the editorial choice.
        "autogeneratePattern": "",
        "hidden": false,
        "id": "text1000000605",
        "max": 0,
        "min": 0,
        "name": "league_name",
        "pattern": "",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "text"
      },
      {
        "hidden": false,
        "id": "number1000000606",
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
        "autogeneratePattern": "",
        "hidden": false,
        "id": "text1000000607",
        "max": 0,
        "min": 0,
        "name": "home_team_name",
        "pattern": "",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "text"
      },
      {
        // The crest URL from the Team Profile, denormalised. Optional: a Team with no
        // profile row draws a placeholder rather than blanking the card.
        "autogeneratePattern": "",
        "hidden": false,
        "id": "text1000000608",
        "max": 0,
        "min": 0,
        "name": "home_logo",
        "pattern": "",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "text"
      },
      {
        "hidden": false,
        "id": "number1000000609",
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
        "autogeneratePattern": "",
        "hidden": false,
        "id": "text1000000610",
        "max": 0,
        "min": 0,
        "name": "away_team_name",
        "pattern": "",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "text"
      },
      {
        "autogeneratePattern": "",
        "hidden": false,
        "id": "text1000000611",
        "max": 0,
        "min": 0,
        "name": "away_logo",
        "pattern": "",
        "presentable": false,
        "required": false,
        "system": false,
        "type": "text"
      },
      {
        // Drives the card's whole shape: kickoff time for NS, scoreline for a finished
        // match, a pill for anything else. The store spells cancelled BOTH `CANC` and
        // `Canc` — match on a whitelist, never on "not one of the finished ones".
        "autogeneratePattern": "",
        "hidden": false,
        "id": "text1000000612",
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
        // The ON-PITCH result — after extra time where there was some, never including a
        // shootout. Null until the match is played.
        "hidden": false,
        "id": "number1000000613",
        "max": null,
        "min": null,
        "name": "home_goals",
        "onlyInt": true,
        "presentable": false,
        "required": false,
        "system": false,
        "type": "number"
      },
      {
        "hidden": false,
        "id": "number1000000614",
        "max": null,
        "min": null,
        "name": "away_goals",
        "onlyInt": true,
        "presentable": false,
        "required": false,
        "system": false,
        "type": "number"
      },
      {
        // The shootout, kept apart from the goals it must never be added to. A `PEN` tie
        // is level on `home_goals`/`away_goals` and decided only here.
        "hidden": false,
        "id": "number1000000615",
        "max": null,
        "min": null,
        "name": "penalty_home",
        "onlyInt": true,
        "presentable": false,
        "required": false,
        "system": false,
        "type": "number"
      },
      {
        "hidden": false,
        "id": "number1000000616",
        "max": null,
        "min": null,
        "name": "penalty_away",
        "onlyInt": true,
        "presentable": false,
        "required": false,
        "system": false,
        "type": "number"
      },
      {
        // When this row last CHANGED — not when it was last checked. The builder compares
        // before it writes, so a run that finds nothing moved leaves this (and `updated`)
        // alone. That makes `updated` a real change signal to cache against.
        //
        // It does NOT tell you the job is alive: a `computed_at` from yesterday is the
        // normal state of a Fixture whose score has not moved. Liveness lives in the cron
        // logs. Nor does it date the scoreline itself — the Published Store underneath
        // only advances on the nightly Refresh.
        "hidden": false,
        "id": "date1000000617",
        "max": "",
        "min": "",
        "name": "computed_at",
        "presentable": false,
        "required": true,
        "system": false,
        "type": "date"
      },
      {
        "hidden": false,
        "id": "autodate1000000618",
        "name": "created",
        "onCreate": true,
        "onUpdate": false,
        "presentable": false,
        "system": false,
        "type": "autodate"
      },
      {
        "hidden": false,
        "id": "autodate1000000619",
        "name": "updated",
        "onCreate": true,
        "onUpdate": true,
        "presentable": false,
        "system": false,
        "type": "autodate"
      }
    ]
  });

  return app.save(row);
}, (app) => {
  const row = app.findCollectionByNameOrId("fixture_row");
  return app.delete(row);
});
