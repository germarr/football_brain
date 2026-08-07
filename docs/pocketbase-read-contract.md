# The blog's PocketBase read contract

What the Astro site (`blog/la-cancha`) can read out of the **Editorial Store**, now that
ADR 0044 has moved every feed it used to pull from the **Published Store** over
`postgres.js`.

This is a **wire contract** between two repos that cannot see each other: the builder is
`football_blog/bundle.py` here, the consumer is `src/lib/*.ts` there, and PocketBase
silently ignores unknown keys on write. A field renamed on either side fails quietly, so
this file and the migrations are the only place the agreement is written down.

Verified against the live instance on **2026-08-07**. Every count, key list and filter
string below was read or executed, not recalled — see [Re-verifying](#re-verifying) to
regenerate them.

- Collections: `docs/adr/0044-the-blog-reads-one-store.md` for *why* this shape
- Glossary: **Match Bundle**, **Fixture Row**, **Editorial Store** in `CONTEXT.md`
- Schema: `football_blog/migrations/` — the two migrations defining these collections,
  with a README on why they are copies and `tests/test_pocketbase_schema.py` checking
  them against the live instance. PocketBase itself reads `personal_site`'s copy

---

## Contents

- [Read this first: the date-filter trap](#read-this-first-the-date-filter-trap)
- [The two collections](#the-two-collections)
- [`match_bundle`](#match_bundle)
- [What is inside each JSON field](#what-is-inside-each-json-field)
- [`fixture_row`](#fixture_row)
- [Where each feed now comes from](#where-each-feed-now-comes-from)
- [Query recipes](#query-recipes)
- [Access rules](#access-rules)
- [Freshness](#freshness)
- [What did not change](#what-did-not-change)
- [Re-verifying](#re-verifying)

---

## Read this first: the date-filter trap

**Never put a `T` in a PocketBase date filter.** It returns the wrong day and does not
error.

PocketBase stores its `date` columns space-separated — `2026-08-07 00:00:00.000Z`, not
`2026-08-07T00:00:00Z` — and compares them **lexically**. `T` is `0x54`, a space is
`0x20`, so an ISO-formatted bound sorts *after* every timestamp on its own date. The `>=`
bound therefore excludes the whole day it names and the `<` bound admits most of the next
one.

Asking for Friday 7 August, three ways:

```
✗  kickoff_utc >= "2026-08-07T00:00:00Z" && kickoff_utc < "2026-08-08T00:00:00Z"
   → 5 records, every one of them on 8 August. Wrong day, HTTP 200.

✓  kickoff_utc >= "2026-08-07 00:00:00"  && kickoff_utc < "2026-08-08 00:00:00"
✓  kickoff_utc >= "2026-08-07"           && kickoff_utc < "2026-08-08"
   → 7 records, all on 7 August. Both forms return an identical set.
```

The safe formats are `YYYY-MM-DD HH:mm:ss` (a trailing `.000Z` is accepted and changes
nothing) and bare `YYYY-MM-DD`.

**This is not new, and `match_preview` is already exposed to it.** It is a property of
every `date` column in the instance. On the previews the site reads *today*,
`kickoff_utc >= "2026-08-07T00:00:00Z"` returns **30** records where the space form
returns **37**. Worth grepping the Astro side for `toISOString()` feeding a filter string.

Python has never had to care — `datetime.fromisoformat` accepts the space separator, which
is why `preview.py:311` parses these records without incident.

### The same shape, reading back

`kickoff_utc` comes back as `"2026-08-15 23:00:00.000Z"`. `new Date(...)` on a
space-separated string is not spec-guaranteed and Safari has historically refused it, so
normalise before parsing:

```ts
const at = new Date(row.kickoff_utc.replace(' ', 'T'));
```

Timestamps **inside** the JSON fields are ours rather than PocketBase's and already carry
the `T…Z` form — `fixture.date`, `home_form[].date`. Only the top-level `date` columns
need this.

---

## The two collections

Not one blob per Fixture. A bundle measures ~30 KB and the Published Store holds 9,514
Fixtures against 40 Match Posts, so bundling everything would put ~285 MB into a SQLite
file that was under a megabyte — to serve pages that do not exist, since the only thing
that renders a bundle is a post page.

So the split is by **question**, not by entity:

| | `match_bundle` | `fixture_row` |
|---|---|---|
| Answers | "render *this* Fixture" | "*which* Fixtures?" |
| Keyed by | Fixture id | a date window |
| Exists for | only Fixtures with a Match Post | only −3d → +14d |
| Size | ~40 records, ~30 KB each | ~94 records, ~300 B each |
| Serves | Feed 1, Feed 4 | Feed 2, Feed 3 |

**Neither can answer the other's question, and that is the invariant to design against.**
A date or team filter over `match_bundle` returns a *short* answer rather than an error —
it holds ~40 of 9,514 Fixtures. Look bundles up by `postgres_fixture_id` and nothing else.

Symmetrically, absence from `fixture_row` means "outside the window", never "no such
Fixture". A page wanting an older Fixture wants `match_bundle`, or wants the window
widened — not a fallback that silently renders nothing.

---

## `match_bundle`

Unique index on `postgres_fixture_id`; secondary indexes on `kickoff_utc` and `status`.

| Field | Type | Req | Notes |
|---|---|:--:|---|
| `postgres_fixture_id` | number | ✓ | The identity, and the only sane lookup key |
| `publication` | relation | ✓ | → `publication`. `?expand=publication` works anonymously |
| `kickoff_utc` | date | ✓ | Denormalised from `fixture.date` for sort/filter |
| `status` | text | | `FT` `AET` `PEN` `NS` `CANC` `Canc` `PST` — see the note below |
| `home_team_id` | number | ✓ | For ribbon → post cross-reference |
| `away_team_id` | number | ✓ | |
| `fixture` | json | | `FixtureRow` |
| `events` | json | | `MatchEventRow[]`, pre-sorted by `event_index` |
| `squad` | json | | `SquadEntryRow[]` — both sides, ~43 entries |
| `team_stats` | json | | `TeamMatchStatRow[]` — 0, 1 or 2 rows |
| `home_profile` | json | | `TeamProfileRow` or null — the crest lives here |
| `away_profile` | json | | |
| `venue` | json | | `VenueRow` or null |
| `players` | json | | Object keyed by **stringified** player id |
| `home_form` | json | | Feed 4, precomputed. May legitimately be `[]` |
| `away_form` | json | | |
| `football_computed_at` | date | ✓ | When this bundle was built |

**Cancelled Fixtures are stored under two spellings** — `CANC` (78 rows) and `Canc` (5).
Any status test must whitelist. A blacklist ("not one of the finished ones") misses five
of them and reports nothing wrong.

**There is no `commentary` field, deliberately.** The Python `FullFixture` carries ESPN
key-moment lines; they are LLM drafting input, the TypeScript `FullFixture` has no slot
for them, and these collections are anonymously readable. They are stripped on write, and
`tests/test_match_bundles.py` asserts no ESPN line survives anywhere into a payload.

---

## What is inside each JSON field

These mirror `blog/la-cancha/src/lib/types.ts` **field for field**. That is by
declaration, not by coincidence: `football_blog/types.py` says its field names and
optionality exactly match the TS twin, and the serializer is `dataclasses.asdict()` with
no remapping. A field added to one side and not the other is a real divergence.

| Field | Keys |
|---|---|
| `fixture` | `id, date, season, league_id, league_name, tournament, phase, group_label, stage, matchday, round, status, venue_id, home_team_id, home_team_name, away_team_id, away_team_name, home_goals, away_goals, penalty_home, penalty_away` |
| `events[]` | `fixture_id, event_index, team_id, minute, extra, type, detail, player_id, assist_id, comments` |
| `squad[]` | `fixture_id, player_id, team_id, status, minutes, position, rating, captain, goals, assists, shots_total, shots_on, passes_total, passes_key, tackles_total, interceptions, duels_total, duels_won, dribbles_attempts, dribbles_success, fouls_drawn, fouls_committed, yellow, red, penalty_scored, penalty_missed` |
| `team_stats[]` | `fixture_id, team_id, possession, shots_total, shots_on, shots_off, shots_blocked, shots_inside, shots_outside, corners, offsides, fouls, yellow, red, saves, passes_total, passes_accurate, passes_pct, expected_goals, goals_prevented` |
| `home_profile` / `away_profile` | `id, name, code, country, founded, is_national, logo, league_id, league_name, league_country, continent` |
| `venue` | `id, name, city, provider_id` |
| `players{}` | `"<id>" → { id, name, firstname, lastname, nationality }` |
| `home_form[]` / `away_form[]` | `fixture_id, date, status, opponent_id, opponent_name, is_home, gf, ga, outcome` |

`players` keys are **strings** — JSON object keys always are. Stringify before lookup.

`fixture.home_goals` / `away_goals` are the **on-pitch** result (after extra time where
there was some) and never include a shootout. A `PEN` tie is level on those two and
decided only by `penalty_home` / `penalty_away`.

### Recent form is already derived

Up to five entries, newest first, from Fixtures **strictly before** this one's kickoff and
only in `FT`/`AET`/`PEN`. `gf`/`ga` are written from the subject team's perspective and
`outcome` derived from them, so nothing on the frontend needs to know which side the team
was on:

```json
{ "fixture_id": 1490347, "date": "2026-07-25T23:30:00Z", "status": "FT",
  "opponent_id": 1614, "opponent_name": "CF Montreal",
  "is_home": false, "gf": 1, "ga": 0, "outcome": "W" }
```

An away game, won 1–0, with `gf`/`ga` already inverted. Two consequences worth knowing:

- **An empty array is a real answer.** Atlante FC's earliest Fixture in the store *is* the
  one being rendered, so it has no prior five. One of the 40 bundles has an empty side
  today, correctly.
- **A shootout win is a `D`.** The strip reports what was played.

---

## `fixture_row`

Same shape as the old `TodaysFixture` type. Unique index on `postgres_fixture_id`;
secondary indexes on `kickoff_utc` and `status`. Currently 94 records — 82 `NS`, 10 `FT`,
2 `PEN`.

| Field | Type | Req | Notes |
|---|---|:--:|---|
| `postgres_fixture_id` | number | ✓ | Cross-reference into `match_post` / `match_preview` for clickability |
| `publication` | relation | ✓ | Accent colour and display timezone live here |
| `kickoff_utc` | date | ✓ | The window is computed on this |
| `league_id` | number | ✓ | Provider competition id |
| `league_name` | text | | Provider's name — **display fallback only** |
| `home_team_name` / `away_team_name` | text | | |
| `home_logo` / `away_logo` | text | | Crest URL. Nullable — draw a placeholder, do not drop the card |
| `status` | text | | Drives the NS / finished branching |
| `home_goals` / `away_goals` | number | | On-pitch result, null until played |
| `penalty_home` / `penalty_away` | number | | Shootout, kept apart from the goals |
| `computed_at` | date | ✓ | When the row last **changed** — see [Freshness](#freshness) |

Prefer `publication.display_name` over `league_name` for anything rendered: the latter is
what the provider calls the Competition, which is not always the same thing and is never
the editorial choice.

### There is no `live_minute`

The old `TodaysFixture` carried it as a permanent null. Nothing could fill it: the
Published Store has no live timeline — events, squads and stats are cache-first and only
land once a Fixture is Final (ADR 0018/0020) — and the **Live Mirror** that does hold
in-progress data is read by the Viewer and published nowhere.

A permanently-null column reads as "not in play" rather than "never measured", so the
field arrives with the job that can populate it. See [Freshness](#freshness).

---

## Where each feed now comes from

| Feed | Was | Now |
|---|---|---|
| **1** — match bundle<br>*post pages, league landing, homepage, RSS* | `loader.ts`, six queries joined in memory | `match_bundle` by Fixture id. One read. |
| **2** — today's fixtures<br>*the ribbon* | `todaysFixtures.ts` | `fixture_row`, day window on `kickoff_utc` |
| **3** — upcoming<br>*homepage weekly table* | `upcomingFixtures.ts` | `fixture_row`, `status = "NS"` + week window |
| **4** — recent form<br>*last-5 strips* | `recentForm.ts`, one query per team | **No query.** Read `home_form` / `away_form` off the bundle |

That retires `loader.ts`, `todaysFixtures.ts`, `upcomingFixtures.ts`, `recentForm.ts` and
`postgres.ts`, and the `postgres` npm dependency with them — which is the point, since it
is what blocks the Cloudflare adapter and per-route on-demand rendering.

---

## Query recipes

Every filter below was executed against the live instance.

### Feed 1 — a post page

```ts
const bundle = await pb.collection('match_bundle')
  .getFirstListItem(`postgres_fixture_id = ${fixtureId}`, { expand: 'publication' });

// Feed 4 comes free — no second call, no second round trip.
const { home_form, away_form } = bundle;
```

### Feed 2 — the ribbon

```ts
// PocketBase wants a space, not a T. See the date-filter trap.
const pbDate = (d: Date) => d.toISOString().slice(0, 19).replace('T', ' ');

const rows = await pb.collection('fixture_row').getFullList({
  filter: `kickoff_utc >= "${pbDate(dayStart)}" && kickoff_utc < "${pbDate(dayEnd)}"`,
  sort:   'kickoff_utc',
  expand: 'publication',
});
```

`dayStart` / `dayEnd` are the CDMX day boundaries converted to UTC, as before.

### Feed 3 — the upcoming week

```ts
filter: `status = "NS" && kickoff_utc >= "${pbDate(weekStart)}" && kickoff_utc < "${pbDate(weekEnd)}"`
```

### League landing / RSS — posts first, then their bundles

```ts
// Relation dereference works in filters, and works anonymously.
const posts = await pb.collection('match_post').getFullList({
  filter: `publication.slug = "liga-mx"`,
  sort:   '-published_at',
});
```

Then one batched lookup for the bundles — **chunked at 80**. The `||` filter chain travels
in the URL and PocketBase answers 400 once it grows too long, at ~87 ids in practice.
`football_blog/pocketbase.py::_by_fixture_ids` is the reference implementation; note that
it sets `perPage` to the chunk size, so a chunk can never silently return a partial page.
"Missing from the result" must mean "no record", never "page two".

---

## Access rules

| Collection | list / view rule | Visible anonymously |
|---|---|:--:|
| `match_bundle` | `publication.published = true` | 40 |
| `fixture_row` | `publication.published = true` | 94 |
| `match_preview` | `publication.published = true` | 38 |
| `match_post` | `status = 'published'` | 38 |
| `publication` | `published = true` | 4 |
| `team_slug` | *(none — fully public)* | 45 |

Reads need no auth. Both new collections are gated on the Publication, which is defence in
depth: the builders already decline to write for an unpublished Publication, and the rule
is the half that still holds if one is un-published later, leaving records behind.

Published Publications: `mundial-2026` (1), `mls` (253), `liga-mx` (262),
`leagues-cup-2026` (772).

### One asymmetry: 40 bundles, 38 readable posts

`match_post` gates on `status = 'published'` while `match_bundle` gates on the
Publication. Two posts are currently drafts, and **their bundles are anonymously
readable.** Only match facts are exposed — the authored `narrative_md` stays hidden with
the post — but the counts do not line up.

**Drive every page list from `match_post` and fetch bundles by the ids it returns.**
Listing bundles directly would surface two Fixtures with no readable post. Tightening the
bundle rule to also require a published post is a one-line migration if that is preferred;
it was left alone because changing an access rule is an editorial decision, not a
mechanical one.

---

## Freshness

| Collection | Cadence | Written by |
|---|---|---|
| `match_bundle` | checked nightly 04:00, plus immediately on draft | `bundle --bundles` in `scripts/nightly.sh`; `draft.py` also writes one per post |
| `fixture_row` | checked every 15 minutes | `bundle --rows`, its own crontab entry |
| `match_preview` | football half checked nightly; quotes rewritten hourly | `preview --full` / `--quotes` (ADR 0040) |

"Checked" rather than "written" — see below.

### Both passes write only what changed

Each run compares its payload against the stored record and skips the PATCH when they
match. A quiet quarter-hourly run therefore issues **two GETs and no writes**, where it
used to issue 94 PATCHes.

`computed_at`, `football_computed_at` and PocketBase's own `updated` consequently mark
when a record last **changed**, not when it was last checked.

| | Before | Now |
|---|---|---|
| `updated` as a change signal | useless — moved every run | **usable** — moves only on a real change |
| `computed_at` as a liveness signal | usable | **no** — use the cron logs |
| `fixture_row` writes/day | ~9,000 | ~0 on a quiet day |

**You can cache against `updated`.** That is the practical win: a `304`-style check or an
ISR revalidation key can key off it, which was not possible when every row's `updated`
moved every fifteen minutes.

**`match_preview` is the exception, on one half.** Its football half — `home`, `away`,
`football_computed_at` — is compared and skipped like the rest. Its **market half is
rewritten every hour on purpose**, so `match_preview.updated` still moves hourly and is
*not* a usable change signal.

Read the two stamps separately, which is what ADR 0040 built them for:

| Field | Moves when |
|---|---|
| `football_computed_at` | the table or a Team Leader actually changed |
| `quote_read_at_kalshi` / `_polymarket` | every hourly read, whether or not the price moved |

A `quote_read_at` is a genuine point-in-time claim and is safe to render as "price read
at …". A `football_computed_at` from two days ago is normal.

**A stale `computed_at` is normal, not a fault.** A Fixture whose score has not moved
keeps yesterday's stamp. Do not render it as "last updated" without that caveat, and do
not use it to decide whether the pipeline is alive — `refresh/logs/fixture-rows.out` is
where that question is answered.

**Neither timestamp ever dated the scoreline.** The store underneath moves once a night,
so raising the row frequency to fix a stale ribbon re-copies identical rows and changes
nothing. Live scores need the Live Mirror to gain a publish path first — its own piece of
work, and its own ADR.

---

## What did not change

- `publication`, `match_post`, `match_preview` and `team_slug` keep their exact schemas.
- The Python pipeline still writes Postgres exactly as before. This is one more **derived**
  read-path laid on top, not a change to any writer.
- Slug logic is untouched; `match_post.slug` is still the URL.
- The **Published Store** remains the store of record. A Match Bundle is a copy, and
  losing every one of them costs a rebuild.

The two collections are defined by migrations that now live in **both** repos:
`football_blog/migrations/` here (the copy, with the README explaining why) and
`personal_site/pocketbase/pb_migrations/` (the one PocketBase actually reads and applies).
Edit there, then copy back — `tests/test_pocketbase_schema.py` fails if they drift, or if
either drifts from the live instance.

---

## Re-verifying

Nothing above should be trusted after the schema changes. To regenerate it:

```bash
# Full schema, access rules and indexes for every collection we own
.venv/bin/python - <<'PY'
from football_blog.pocketbase import PocketBaseClient
pb = PocketBaseClient()
r = pb._client.get(f'{pb.base_url}/api/collections',
                   headers=pb._headers(), params={'perPage': 100})
for c in r.json()['items']:
    if c['name'] not in ('match_bundle', 'fixture_row', 'match_post',
                         'match_preview', 'publication', 'team_slug'):
        continue
    print(f"### {c['name']}  list={c.get('listRule')!r}")
    for f in c['fields']:
        print(f"    {f['name']:24} {f['type']:10} required={f.get('required')}")
PY

# What the site actually sees (anonymous, no auth header).
# POCKETBASE_URL lives in .env and is NOT exported to the shell — config.py loads it
# at import time — so read it out rather than expecting it in the environment.
PB=$(grep ^POCKETBASE_URL .env | cut -d= -f2-)
curl -s "${PB:-http://127.0.0.1:8090}/api/collections/match_bundle/records?perPage=1" \
  | head -c 400
```

The JSON sub-shapes come from `football_blog/types.py`, which is the mirror of
`src/lib/types.ts`. The date-filter behaviour is asserted nowhere in either repo — it is a
PocketBase property, so re-test it directly if you change how filters are built.
